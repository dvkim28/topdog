"""Two-tier game title normalization.

Tier 1 (deterministic, cheap): clean up the raw label the same way
`index.scraping.matching.normalize` always has, then fuzzy-match it against
every known `Game`/`GameAlias` with RapidFuzz. Titles that clear
`FUZZY_MATCH_THRESHOLD` are resolved immediately, no network call involved.

Tier 2 (Claude, batched): whatever Tier 1 couldn't confidently resolve is
batched into one structured request to Claude, alongside each title's
nearest RapidFuzz candidates, and asked to either pick a candidate's game id
or say the title is a new game. Anything Tier 2 also can't resolve - or that
Claude explicitly flags NEW_GAME - is written to `UnmatchedTileReview` for a
human to confirm.

`resolve_tiles()` is the single entry point tasks.py calls; it returns the
rows ready for `HomepagePlacement.objects.bulk_create` plus the
`UnmatchedTileReview` rows to persist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from django.conf import settings

from index.models import Game, GameAlias, UnmatchedTileReview
from index.scraping.matching import normalize

FUZZY_MATCH_THRESHOLD = 88.0  # RapidFuzz score, 0-100
FUZZY_CANDIDATE_CUTOFF = 60.0  # below this, don't even offer it to Claude
AI_CANDIDATES_PER_TITLE = 5
AI_BATCH_SIZE = 25

_BATCH_SYSTEM_PROMPT = """\
You match casino lobby tile labels to a canonical game catalogue.

You will receive a JSON array. Each element has:
{
  "id": integer,                          // index into this batch, echo it back unchanged
  "raw_label": string,                    // the label as scraped from the page
  "candidates": [{"game_id": int, "title": string, "score": number}, ...]  // nearest known titles
}

Return ONLY a JSON array, no prose, no markdown fences, one element per input element:
{
  "id": integer,               // matches the input id
  "game_id": integer or null,  // the correct candidate's game_id, or null if none of them are a match
  "confidence": number,        // 0-1, your confidence in this decision
  "new_game": boolean          // true if raw_label is clearly a real game not in the candidate list
}

Rules:
- Only pick a candidate's game_id if you are confident raw_label refers to that exact game (allow for
  marketing noise, abbreviations, and translated titles), not just a similar theme or provider.
- If none of the candidates match, set game_id to null.
- Set new_game true only when raw_label is unambiguously a specific slot/live-table/game title, not a
  category header, provider name, or promotional banner text.
- If you are unsure, set game_id to null and new_game to false; a human will review it.
"""


@dataclass
class ResolvedTile:
    game_id: int
    placement: str
    position: int
    raw_label: str
    row: int | None = None
    column: int | None = None


class FuzzyGameMatcher:
    """Tier 1: RapidFuzz against the alias table, built once per crawl run."""

    def __init__(self, alias_rows: list[tuple[str, int, str]]):
        # alias_rows: [(normalized_alias, game_id, display_title), ...]
        self._by_normalized: dict[str, tuple[int, str]] = {}
        for normalized, game_id, title in alias_rows:
            self._by_normalized.setdefault(normalized, (game_id, title))
        self._choices = list(self._by_normalized.keys())

    @classmethod
    def from_db(cls) -> "FuzzyGameMatcher":
        rows = [
            (normalized, game_id, title)
            for normalized, game_id, title in GameAlias.objects.select_related("game").values_list(
                "normalized", "game_id", "game__title"
            )
        ]
        rows += [
            (normalize(title), pk, title) for pk, title in Game.objects.values_list("id", "title")
        ]
        return cls(rows)

    def best_candidates(self, raw_label: str, limit: int = AI_CANDIDATES_PER_TITLE) -> list[dict]:
        from rapidfuzz import fuzz, process

        key = normalize(raw_label)
        if not key or not self._choices:
            return []
        matches = process.extract(key, self._choices, scorer=fuzz.WRatio, limit=limit)
        candidates = []
        for choice, score, _index in matches:
            if score < FUZZY_CANDIDATE_CUTOFF:
                continue
            game_id, title = self._by_normalized[choice]
            candidates.append({"game_id": game_id, "title": title, "score": round(score / 100, 3)})
        return candidates

    def match(self, raw_label: str) -> tuple[int | None, float, list[dict]]:
        """Returns (game_id_or_None, best_score_0_1, candidates)."""
        candidates = self.best_candidates(raw_label)
        if not candidates:
            return None, 0.0, []
        best = candidates[0]
        if best["score"] * 100 >= FUZZY_MATCH_THRESHOLD:
            return best["game_id"], best["score"], candidates
        return None, best["score"], candidates


def _ai_batch_match(batch: list[dict]) -> dict[int, dict]:
    """batch: [{"id", "raw_label", "candidates"}, ...] -> {id: {game_id, confidence, new_game}}"""
    from index.scraping.ai_client import call_for_json

    payload = json.dumps(
        [
            {
                "id": item["id"],
                "raw_label": item["raw_label"],
                "candidates": [
                    {"game_id": c["game_id"], "title": c["title"], "score": c["score"]}
                    for c in item["candidates"]
                ],
            }
            for item in batch
        ]
    )
    data = call_for_json(_BATCH_SYSTEM_PROMPT, payload)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array from the batch matcher, got {type(data).__name__}")

    results = {}
    for item in data:
        try:
            item_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        results[item_id] = {
            "game_id": item.get("game_id"),
            "confidence": float(item.get("confidence", 0) or 0),
            "new_game": bool(item.get("new_game", False)),
        }
    return results


def resolve_tiles(tiles: list, brand) -> tuple[list[ResolvedTile], list[UnmatchedTileReview]]:
    """Tier 1 then Tier 2. Returns (resolved rows, UnmatchedTileReview rows to save)."""
    matcher = FuzzyGameMatcher.from_db()
    resolved: list[ResolvedTile] = []
    tier2_queue: list[dict] = []  # {"tile": Tile, "candidates": [...]}

    for tile in tiles:
        game_id, score, candidates = matcher.match(tile.raw_label)
        if game_id:
            resolved.append(
                ResolvedTile(
                    game_id=game_id,
                    placement=tile.placement,
                    position=tile.position,
                    raw_label=tile.raw_label,
                    row=getattr(tile, "row", None),
                    column=getattr(tile, "column", None),
                )
            )
        else:
            tier2_queue.append({"tile": tile, "score": score, "candidates": candidates})

    reviews: list[UnmatchedTileReview] = []
    if tier2_queue and settings.AI["ENABLED"]:
        for start in range(0, len(tier2_queue), AI_BATCH_SIZE):
            chunk = tier2_queue[start : start + AI_BATCH_SIZE]
            batch = [
                {"id": i, "raw_label": entry["tile"].raw_label, "candidates": entry["candidates"]}
                for i, entry in enumerate(chunk)
            ]
            try:
                ai_results = _ai_batch_match(batch)
            except Exception as exc:  # noqa: BLE001 - Tier 2 failure falls through to human review
                logger_msg = f"Tier 2 AI batch match failed: {exc}"
                import logging

                logging.getLogger(__name__).warning(logger_msg)
                ai_results = {}

            for i, entry in enumerate(chunk):
                tile = entry["tile"]
                ai = ai_results.get(i, {})
                if ai.get("game_id") and ai.get("confidence", 0) >= 0.6:
                    resolved.append(
                        ResolvedTile(
                            game_id=ai["game_id"],
                            placement=tile.placement,
                            position=tile.position,
                            raw_label=tile.raw_label,
                            row=getattr(tile, "row", None),
                            column=getattr(tile, "column", None),
                        )
                    )
                    continue
                reviews.append(
                    UnmatchedTileReview(
                        brand=brand,
                        raw_label=tile.raw_label[:220],
                        normalized=normalize(tile.raw_label)[:220],
                        placement=tile.placement,
                        best_guess_id=entry["candidates"][0]["game_id"] if entry["candidates"] else None,
                        best_score=entry["score"],
                        ai_candidates=entry["candidates"],
                        ai_suggested_new=ai.get("new_game", False),
                    )
                )
    else:
        for entry in tier2_queue:
            tile = entry["tile"]
            reviews.append(
                UnmatchedTileReview(
                    brand=brand,
                    raw_label=tile.raw_label[:220],
                    normalized=normalize(tile.raw_label)[:220],
                    placement=tile.placement,
                    best_guess_id=entry["candidates"][0]["game_id"] if entry["candidates"] else None,
                    best_score=entry["score"],
                    ai_candidates=entry["candidates"],
                )
            )

    return resolved, reviews

"""Two-tier game title normalization.

Tier 1 (deterministic, cheap): clean up the raw label the same way
`index.scraping.matching.normalize` always has, then fuzzy-match it against
every known `Game`/`GameAlias` with RapidFuzz. Titles that clear
`FUZZY_MATCH_THRESHOLD` are resolved immediately, no network call involved.

Tier 2 (Claude, batched): whatever Tier 1 couldn't confidently resolve is
batched into one structured request to Claude, alongside each title's
nearest RapidFuzz candidates, and asked to either pick a candidate's game id
or say the title is a new game.

Claude auto-*matching* a tile to an existing candidate game is safe to do
without a human (it isn't adding anything to the catalogue, just recognizing
an alias of something already there). Claude calling a tile a brand new
game never is: every such tile lands in `UnmatchedTileReview` with status
PENDING regardless of Claude's confidence, and a human decides in the admin
panel whether to link it to an existing game (as an alias, no new rows) or
approve it as new (which creates the Game, and the Provider only if one
matching that name doesn't already exist). Claude's provider/category guess
is still attached to the review row - as `ai_suggested_provider` /
`ai_suggested_category` - purely to prefill the approval form; it never
creates anything by itself.

`resolve_tiles()` is the single entry point tasks.py calls; it returns the
rows ready for `HomepagePlacement.objects.bulk_create` plus the
`UnmatchedTileReview` rows to persist.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.utils.text import slugify

from index.models import Category, Game, GameAlias, Provider, UnmatchedTileReview
from index.scraping.matching import normalize

logger = logging.getLogger(__name__)

FUZZY_MATCH_THRESHOLD = 88.0  # RapidFuzz score, 0-100
FUZZY_CANDIDATE_CUTOFF = 60.0  # below this, don't even offer it to Claude - "not similar to anything"
AI_CANDIDATES_PER_TITLE = 5
AI_BATCH_SIZE = 25

# Matching an existing candidate only needs Tier 2 to be reasonably sure -
# it isn't adding anything to the catalogue. Calling a tile a new game never
# auto-creates anything (see module docstring), so there's no equivalent
# confidence gate for that path.
AI_MATCH_CONFIDENCE = 0.6

# Tier 2 decisions are cached by normalized title for a while so a title that
# lands on many brands the same night (a new release rolling out everywhere
# at once is the norm, not the exception) only ever costs one Claude call,
# not one per brand. Long enough to cover a full nightly run across every
# brand, short enough that a catalogue correction is picked up the next day.
AI_MATCH_CACHE_TTL = 60 * 60 * 20

_BATCH_SYSTEM_PROMPT = """\
You match casino lobby tile labels to a canonical game catalogue.

You will receive a JSON array. Each element has:
{
  "id": integer,                          // echo it back unchanged
  "raw_label": string,                    // the label as scraped from the page
  "placement": string,                    // "hero" | "grid" | "live_section" | "other"
  "candidates": [{"game_id": int, "title": string, "score": number}, ...]  // nearest known titles
}

Return ONLY a JSON array, no prose, no markdown fences, one element per input element:
{
  "id": integer,               // matches the input id
  "game_id": integer or null,  // the correct candidate's game_id, or null if none of them are a match
  "confidence": number,        // 0-1, your confidence in this decision
  "new_game": boolean,         // true if raw_label is clearly a real game not in the candidate list
  "provider": string,          // only when new_game is true and candidates is empty: your best guess at
                                // the studio/provider that makes this game, from naming style and branding
                                // conventions. "" if you can't make a reasonable guess.
  "category": string           // only when new_game is true and candidates is empty: one of
                                // live_roulette, live_blackjack, game_show, megaways, classic_slot, crash,
                                // table - inferred from placement ("live_section" -> a live_* value) and
                                // the title itself. "" if you can't make a reasonable guess.
}

Rules:
- Only pick a candidate's game_id if you are confident raw_label refers to that exact game (allow for
  marketing noise, abbreviations, and translated titles), not just a similar theme or provider.
- If none of the candidates match, set game_id to null.
- Set new_game true only when raw_label is unambiguously a specific slot/live-table/game title, not a
  category header, provider name, or promotional banner text. A human always reviews new_game tiles
  before anything is created - your provider/category guess only prefills that review form, so give your
  best guess whenever you're reasonably confident instead of leaving it blank.
- When candidates is non-empty, leave provider and category as "" even if new_game is true - raw_label
  being similar to an existing title is exactly the case a human should look at directly.
- If you are unsure whether raw_label is a real game at all, set game_id to null and new_game to false;
  a human will review it.
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


def _ai_cache_key(raw_label: str) -> str:
    # Hashed rather than the raw normalized text: cache keys with spaces
    # trigger Django's memcached-compatibility warning on every lookup
    # (noisy in logs even though this project's backend is Redis, which has
    # no such restriction), and a fixed-length key is a bit friendlier to
    # any backend regardless.
    digest = hashlib.sha1(normalize(raw_label).encode("utf-8")).hexdigest()
    return f"aimatch:{digest}"


def unique_slug(model, text: str, fallback: str = "item") -> str:
    """Slugify `text` and disambiguate against `model`'s existing slugs."""
    base = slugify(text) or fallback
    slug, n = base, 1
    while model.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


def get_or_create_provider(name: str) -> Provider:
    """Case-insensitive lookup so re-typing an existing provider's name (in
    the admin panel's new-game approval form, or here) links to it instead of
    minting a duplicate - one provider legitimately has many games.
    """
    provider = Provider.objects.filter(name__iexact=name).first()
    if provider:
        return provider
    name = name[:120]
    return Provider.objects.create(name=name, slug=unique_slug(Provider, name, fallback="provider"))


def _ai_batch_match(batch: list[dict]) -> dict[int, dict]:
    """batch: [{"id", "raw_label", "placement", "candidates"}, ...]
    -> {id: {game_id, confidence, new_game, provider, category}}
    """
    from index.scraping.ai_client import call_for_json

    payload = json.dumps(
        [
            {
                "id": item["id"],
                "raw_label": item["raw_label"],
                "placement": item["placement"],
                "candidates": [
                    {"game_id": c["game_id"], "title": c["title"], "score": c["score"]}
                    for c in item["candidates"]
                ],
            }
            for item in batch
        ]
    )
    # Tier 2 is a classification task, not open-ended extraction, so it runs
    # on the cheaper match model rather than the model used for HTML
    # extraction - the biggest lever on Anthropic spend here is call volume
    # (batching + the cache above), the next biggest is not paying
    # extraction-grade rates for a pick-a-candidate decision.
    data = call_for_json(_BATCH_SYSTEM_PROMPT, payload, model=settings.AI["MATCH_MODEL"])
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
            "provider": str(item.get("provider", "") or "").strip(),
            "category": str(item.get("category", "") or "").strip(),
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
        # Titles already resolved by Tier 2 earlier tonight (any brand) come
        # straight from cache - no repeat Claude call for the same title.
        ai_results: dict[int, dict] = {}
        cache_keys: dict[int, str] = {}
        to_call: list[int] = []
        for i, entry in enumerate(tier2_queue):
            key = _ai_cache_key(entry["tile"].raw_label)
            cached = cache.get(key)
            if cached is not None:
                ai_results[i] = cached
            else:
                cache_keys[i] = key
                to_call.append(i)

        for start in range(0, len(to_call), AI_BATCH_SIZE):
            idx_chunk = to_call[start : start + AI_BATCH_SIZE]
            batch = [
                {
                    "id": i,
                    "raw_label": tier2_queue[i]["tile"].raw_label,
                    "placement": tier2_queue[i]["tile"].placement,
                    "candidates": tier2_queue[i]["candidates"],
                }
                for i in idx_chunk
            ]
            try:
                batch_results = _ai_batch_match(batch)
            except Exception as exc:  # noqa: BLE001 - Tier 2 failure falls through to human review
                logger.warning("Tier 2 AI batch match failed: %s", exc)
                batch_results = {}

            for i in idx_chunk:
                result = batch_results.get(i, {})
                ai_results[i] = result
                cache.set(cache_keys[i], result, AI_MATCH_CACHE_TTL)

        for i, entry in enumerate(tier2_queue):
            tile = entry["tile"]
            ai = ai_results.get(i, {})

            if ai.get("game_id") and ai.get("confidence", 0) >= AI_MATCH_CONFIDENCE:
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

            # Never auto-created: every new-game candidate goes to a human,
            # with whatever provider/category is known attached purely to
            # prefill the approval form. A provider actually read off the
            # page (network JSON, or an HTML badge Claude spotted) is a fact,
            # not a guess - it wins over Tier 2's title-pattern inference
            # whenever both are available.
            category = ai.get("category", "")
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
                    ai_suggested_provider=(getattr(tile, "provider", "") or ai.get("provider", ""))[:120],
                    ai_suggested_category=category if category in Category.values else "",
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
                    ai_suggested_provider=getattr(tile, "provider", "")[:120],
                )
            )

    return resolved, reviews

"""Resolve a raw lobby tile label to a known Game."""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

# Noise that appears around tile labels in Spanish lobbies.
_STOPWORDS = {
    "jugar", "nuevo", "new", "exclusivo", "exclusiva", "en", "vivo", "directo",
    "demo", "bote", "jackpot", "hd", "es", "espanol",
}
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

MATCH_THRESHOLD = 0.88


def normalize(text: str) -> str:
    """Casefold, strip accents and punctuation, drop marketing noise."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", text.lower())
    tokens = [t for t in _SPACES.split(text) if t and t not in _STOPWORDS]
    return " ".join(tokens)


@dataclass
class MatchResult:
    game_id: int | None
    score: float
    exact: bool


class GameMatcher:
    """Built once per crawl run from the alias table, then reused per tile."""

    def __init__(self, alias_rows: list[tuple[str, int]]):
        # alias_rows: [(normalized_alias, game_id), ...]
        self._exact: dict[str, int] = {}
        for normalized, game_id in alias_rows:
            self._exact.setdefault(normalized, game_id)
        self._keys = list(self._exact.keys())

    @classmethod
    def from_db(cls) -> "GameMatcher":
        from index.models import Game, GameAlias

        rows = list(GameAlias.objects.values_list("normalized", "game_id"))
        rows += [(normalize(t), pk) for pk, t in Game.objects.values_list("id", "title")]
        return cls(rows)

    def match(self, raw_label: str) -> MatchResult:
        key = normalize(raw_label)
        if not key:
            return MatchResult(None, 0.0, False)
        if key in self._exact:
            return MatchResult(self._exact[key], 1.0, True)
        close = difflib.get_close_matches(key, self._keys, n=1, cutoff=0.75)
        if not close:
            return MatchResult(None, 0.0, False)
        score = difflib.SequenceMatcher(None, key, close[0]).ratio()
        game_id = self._exact[close[0]] if score >= MATCH_THRESHOLD else None
        return MatchResult(game_id, round(score, 3), False)

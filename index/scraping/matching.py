"""Deterministic string cleanup shared by every matching tier.

Tier 1 (RapidFuzz) and Tier 2 (Claude) both live in
`index.services.normalization`; this module only holds the normalization
step both tiers - and `GameAlias.save()` - build on.
"""

from __future__ import annotations

import re
import unicodedata

# Noise that appears around tile labels in Spanish lobbies.
_STOPWORDS = {
    "jugar", "nuevo", "new", "exclusivo", "exclusiva", "en", "vivo", "directo",
    "demo", "bote", "jackpot", "hd", "es", "espanol",
}
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Casefold, strip accents and punctuation, drop marketing noise."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", text.lower())
    tokens = [t for t in _SPACES.split(text) if t and t not in _STOPWORDS]
    return " ".join(tokens)

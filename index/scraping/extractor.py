"""Turn a main-page HTML document into a list of tiles with their section."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from index.models import Placement

DEFAULT_SELECTORS = {
    Placement.HERO: ".hero, .carousel, [class*='hero'] [class*='slide']",
    Placement.GRID: "[class*='top-games'] [class*='card'], [class*='popular'] [class*='tile']",
    Placement.LIVE_SECTION: "[class*='live'] [class*='card'], [id*='live'] [class*='tile']",
}

TITLE_ATTRS = ("data-game-name", "data-title", "aria-label", "title", "alt")
# Some sites' SPA components set aria-label to the tile's route path rather
# than its display name (seen live on leovegas.es: aria-label="/juegos/
# vegas-vault" on the <a> wrapping a tile whose nested <img alt="Vegas
# Vault"> has the real title). A value shaped like a path or URL is never a
# genuine game title, so it's rejected here and _label_from() falls through
# to the next attribute / the nested <img> / the node's own text instead.
_PATH_LIKE = re.compile(r"^/|://")


@dataclass
class Tile:
    raw_label: str
    placement: str
    position: int
    row: int | None = None
    column: int | None = None
    provider: str = ""


def _label_from(node) -> str:
    for attr in TITLE_ATTRS:
        value = node.get(attr)
        if value and value.strip() and not _PATH_LIKE.match(value.strip()):
            return value.strip()
    img = node.find("img")
    if img:
        for attr in ("alt", "title"):
            value = img.get(attr)
            if value and value.strip() and not _PATH_LIKE.match(value.strip()):
                return value.strip()
    text = node.get_text(" ", strip=True)
    return text[:200]


def extract_tiles(html: str, selectors: dict | None = None) -> list[Tile]:
    """selectors is Brand.selectors: {"hero": "...", "grid": "...", "live_section": "..."}"""
    soup = BeautifulSoup(html, "lxml")
    selectors = selectors or {}
    tiles: list[Tile] = []
    seen: set[tuple[str, str]] = set()

    for placement in (Placement.HERO, Placement.GRID, Placement.LIVE_SECTION):
        selector = selectors.get(placement.value) or DEFAULT_SELECTORS[placement]
        position = 0
        for node in soup.select(selector):
            label = _label_from(node)
            if not label or len(label) < 2:
                continue
            key = (placement.value, label.lower())
            if key in seen:
                continue
            seen.add(key)
            tiles.append(Tile(raw_label=label, placement=placement.value, position=position))
            position += 1

    return tiles

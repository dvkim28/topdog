"""Shared logic for turning an arbitrarily-shaped JSON payload into tiles.

Used by two different sources of JSON: a network response sniffed straight
off the wire (services/scraper.py) and structured data embedded in the page
itself - `<script type="application/ld+json">`, `__NEXT_DATA__`,
`window.__INITIAL_STATE__` (scraping/json_extract.py). Both reduce to the
same problem: find the array of game-like dicts inside a payload of unknown
shape, and read a title/position/provider off each one.
"""

from __future__ import annotations

from index.models import Placement
from index.scraping.extractor import Tile

# Keys a JSON object needs at least one of to plausibly be "a game", and keys
# that identify the array of such objects inside an arbitrarily-shaped payload.
TITLE_KEYS = ("title", "name", "gameName", "game_name", "label")
LIST_KEYS = ("games", "items", "tiles", "results", "data", "lobby", "list")
# Most lobby JSON APIs already carry the provider/studio name per game - when
# it's there, it's a fact off the wire, not a guess, so it's used as-is
# instead of asking a human (or Claude) to supply one later.
PROVIDER_KEYS = ("provider", "providerName", "provider_name", "vendor", "studio", "supplier", "gameProvider")


def find_tile_array(payload) -> list | None:
    """Depth-limited search for the array of game-like dicts inside a payload
    of unknown shape. Handles both a bare array and `{"data": {"games": [...]}}`
    style envelopes.
    """
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and any(k in payload[0] for k in TITLE_KEYS):
            return payload
        return None
    if not isinstance(payload, dict):
        return None
    for key in LIST_KEYS:
        value = payload.get(key)
        found = find_tile_array(value) if isinstance(value, (list, dict)) else None
        if found:
            return found
    # One level deeper, for envelopes like {"data": {"payload": {...}}}
    for value in payload.values():
        if isinstance(value, dict):
            found = find_tile_array(value)
            if found:
                return found
    return None


def guess_placement(url: str, payload_key_hint: str) -> str:
    haystack = f"{url} {payload_key_hint}".lower()
    if "hero" in haystack or "banner" in haystack or "carousel" in haystack:
        return Placement.HERO
    if "live" in haystack:
        return Placement.LIVE_SECTION
    if "top" in haystack or "popular" in haystack or "featured" in haystack or "grid" in haystack:
        return Placement.GRID
    return Placement.OTHER


def tiles_from_json(url: str, payload) -> list[Tile]:
    """url is only used as a placement-guessing hint - pass "" for payloads
    that didn't come from a URL (e.g. a script tag embedded in the page).
    """
    array = find_tile_array(payload)
    if not array:
        return []
    placement = guess_placement(url, "")
    tiles = []
    for position, item in enumerate(array):
        if not isinstance(item, dict):
            continue
        label = next((str(item[k]).strip() for k in TITLE_KEYS if item.get(k)), "")
        if not label:
            continue
        row = item.get("row") or item.get("rowIndex")
        column = item.get("column") or item.get("col") or item.get("columnIndex")
        item_placement = item.get("section") or item.get("placement")
        provider = next((str(item[k]).strip() for k in PROVIDER_KEYS if item.get(k)), "")
        tiles.append(
            Tile(
                raw_label=label[:200],
                placement=guess_placement(url, str(item_placement or "")) if item_placement else placement,
                position=position,
                row=int(row) if row else None,
                column=int(column) if column else None,
                provider=provider[:120],
            )
        )
    return tiles

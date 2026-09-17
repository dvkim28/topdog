"""Priority-1 DOM extraction: read tiles straight out of structured data the
page already embeds, before touching CSS selectors or the AI extractor.

A lot of modern lobbies hydrate from a JSON blob the server inlines into the
page itself rather than (or in addition to) a separate XHR - `<script
type="application/ld+json">`, Next.js's `__NEXT_DATA__`, or a plain
`window.__INITIAL_STATE__ = {...}` assignment. When one of these is present
and contains a tile array, reading it directly is free (a JSON parse, no
selectors to maintain, nothing for the AI fallback to ever see) and more
reliable than any CSS selector, since it can't be broken by a class-name
rename the way markup-based extraction can.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from index.scraping.extractor import Tile
from index.scraping.json_payload import find_tile_array, tiles_from_json

_INITIAL_STATE_ASSIGNMENT = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*(?:</script>|$|\n\s*(?:var|let|const|window))",
    re.DOTALL,
)


def _tiles_from_ld_json(soup: BeautifulSoup) -> list[Tile]:
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not node.string:
            continue
        try:
            payload = json.loads(node.string)
        except (json.JSONDecodeError, TypeError):
            continue
        tiles = tiles_from_json("", payload)
        if tiles:
            return tiles
    return []


def _tiles_from_next_data(soup: BeautifulSoup) -> list[Tile]:
    node = soup.find("script", id="__NEXT_DATA__")
    if not node or not node.string:
        return []
    try:
        payload = json.loads(node.string)
    except json.JSONDecodeError:
        return []
    # Next.js always nests page data under props.pageProps - check there
    # first so a tile array buried a level below find_tile_array's search
    # depth is still found, then fall back to a search of the whole payload.
    page_props = payload.get("props", {}).get("pageProps", {}) if isinstance(payload, dict) else {}
    return tiles_from_json("", page_props) or tiles_from_json("", payload)


def _tiles_from_initial_state(html: str) -> list[Tile]:
    match = _INITIAL_STATE_ASSIGNMENT.search(html)
    if not match:
        return []
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return tiles_from_json("", payload)


def extract_embedded_json_tiles(html: str) -> list[Tile]:
    """Tries each known embedding in order, returns the first one that
    actually yields tiles. Never raises - a malformed or absent blob just
    means "nothing found here", so the caller moves on to CSS selectors.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")

    for finder in (
        lambda: _tiles_from_ld_json(soup),
        lambda: _tiles_from_next_data(soup),
        lambda: _tiles_from_initial_state(html),
    ):
        tiles = finder()
        if tiles:
            return tiles
    return []

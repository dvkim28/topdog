"""Primary scraping strategy: sniff the lobby/games JSON off the network,
instead of parsing the rendered DOM.

Most modern casino lobbies are client-side apps that hydrate from a JSON API
(`/games`, `/lobby`, `/tiles`, ...). Listening for that response is cheaper
and more reliable than parsing the DOM it eventually produces, and doesn't
break every time a class name changes. `scrape_brand()` opens the homepage
(and live-casino page, if configured) in a headless browser, listens on
`page.on("response")` for a matching JSON payload for up to
`NETWORK_SNIFF_TIMEOUT` seconds, and only falls back to DOM extraction (AI or
CSS, on the same already-rendered page) if nothing usable showed up in time.

Every response checked against the URL patterns is recorded as a
`NetworkCaptureLogEntry`, matched or not, so the admin log viewer and
`ScrapeLog.extraction_mode` can both show what actually happened.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.conf import settings

from index.models import ExtractionMode, Placement
from index.scraping.extractor import Tile
from index.scraping.fetcher import RobotsDisallowed
from index.scraping.robots import crawl_allowed, crawl_delay

logger = logging.getLogger(__name__)

NETWORK_SNIFF_TIMEOUT = float(getattr(settings, "CRAWLER", {}).get("NETWORK_SNIFF_TIMEOUT", 5))
NETWORK_URL_PATTERNS = getattr(
    settings, "CRAWLER", {}
).get("NETWORK_URL_PATTERNS", ["/games", "/lobby", "/tiles", "/api/casino", "/casino/api"])

# Keys a JSON object needs at least one of to plausibly be "a game", and keys
# that identify the array of such objects inside an arbitrarily-shaped payload.
_TITLE_KEYS = ("title", "name", "gameName", "game_name", "label")
_LIST_KEYS = ("games", "items", "tiles", "results", "data", "lobby", "list")


@dataclass
class NetworkCaptureLogEntry:
    url: str
    matched_pattern: str = ""
    status_code: int | None = None
    tile_count: int = 0
    used: bool = False


@dataclass
class ScrapeOutcome:
    tiles: list[Tile]
    extraction_mode: str
    network_logs: list[NetworkCaptureLogEntry] = field(default_factory=list)


def _matched_pattern(url: str) -> str:
    for pattern in NETWORK_URL_PATTERNS:
        if pattern in url:
            return pattern
    return ""


def _find_tile_array(payload) -> list | None:
    """Depth-limited search for the array of game-like dicts inside a payload
    of unknown shape. Handles both a bare array and `{"data": {"games": [...]}}`
    style envelopes.
    """
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict) and any(k in payload[0] for k in _TITLE_KEYS):
            return payload
        return None
    if not isinstance(payload, dict):
        return None
    for key in _LIST_KEYS:
        value = payload.get(key)
        found = _find_tile_array(value) if isinstance(value, (list, dict)) else None
        if found:
            return found
    # One level deeper, for envelopes like {"data": {"payload": {...}}}
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_tile_array(value)
            if found:
                return found
    return None


def _guess_placement(url: str, payload_key_hint: str) -> str:
    haystack = f"{url} {payload_key_hint}".lower()
    if "hero" in haystack or "banner" in haystack or "carousel" in haystack:
        return Placement.HERO
    if "live" in haystack:
        return Placement.LIVE_SECTION
    if "top" in haystack or "popular" in haystack or "featured" in haystack or "grid" in haystack:
        return Placement.GRID
    return Placement.OTHER


def _tiles_from_json(url: str, payload) -> list[Tile]:
    array = _find_tile_array(payload)
    if not array:
        return []
    placement = _guess_placement(url, "")
    tiles = []
    for position, item in enumerate(array):
        if not isinstance(item, dict):
            continue
        label = next((str(item[k]).strip() for k in _TITLE_KEYS if item.get(k)), "")
        if not label:
            continue
        row = item.get("row") or item.get("rowIndex")
        column = item.get("column") or item.get("col") or item.get("columnIndex")
        item_placement = item.get("section") or item.get("placement")
        tiles.append(
            Tile(
                raw_label=label[:200],
                placement=_guess_placement(url, str(item_placement or "")) if item_placement else placement,
                position=position,
                row=int(row) if row else None,
                column=int(column) if column else None,
            )
        )
    return tiles


def _autoscroll(page, max_scrolls: int = 8, pause_ms: int = 400) -> None:
    """Scroll to the bottom repeatedly so lazy-loaded / virtualized tiles below
    the fold actually mount into the DOM (and any paginated network calls
    they trigger get a chance to fire) before the page is read.

    Most lobby grids only render what's on screen at load time and append
    more as you scroll; reading `page.content()` right after `networkidle`
    without this misses everything below the first viewport.
    """
    last_height = 0
    for _ in range(max_scrolls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(pause_ms)
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height:
            break
        last_height = height


def _sniff_network(page, urls: list[str], network_logs: list[NetworkCaptureLogEntry]) -> list[Tile]:
    """Navigate to each URL, listening for every matching JSON response, and
    merge tiles across all of them (deduped by placement + label).

    A lobby that paginates its API as you scroll fires several matching
    responses in one visit; taking only the first would silently drop
    everything after page one, so every usable payload is folded in.
    """
    tiles_by_key: dict[tuple[str, str], Tile] = {}

    def on_response(response):
        pattern = _matched_pattern(response.url)
        if not pattern or "json" not in (response.headers.get("content-type") or ""):
            return
        entry = NetworkCaptureLogEntry(url=response.url, matched_pattern=pattern, status_code=response.status)
        network_logs.append(entry)
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - not every matching URL is parseable JSON
            return
        tiles = _tiles_from_json(response.url, payload)
        entry.tile_count = len(tiles)
        if not tiles:
            return
        entry.used = True
        for tile in tiles:
            key = (tile.placement, tile.raw_label.lower())
            if key not in tiles_by_key:
                tile.position = len(tiles_by_key)
                tiles_by_key[key] = tile

    page.on("response", on_response)
    try:
        for url in urls:
            page.goto(url, wait_until="networkidle", timeout=int(NETWORK_SNIFF_TIMEOUT * 1000) + 5000)
            page.wait_for_timeout(NETWORK_SNIFF_TIMEOUT * 1000)
            # Scrolling both mounts lazy DOM content (used later if the sniff
            # comes up empty) and gives infinite-scroll lobbies a chance to
            # fire the next page of the API this loop is listening for.
            _autoscroll(page)
    finally:
        page.remove_listener("response", on_response)

    return list(tiles_by_key.values())


# Only the DOM/JSON matters here - stylesheets, images and fonts add load
# time and bandwidth without adding any extraction signal (labels come from
# attributes/text already present in the markup, not rendered pixels).
_BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}


def _block_heavy_assets(route):
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _dom_fallback(brand, page, html: str) -> tuple[list[Tile], str]:
    if brand.use_ai_extraction and settings.AI["ENABLED"]:
        from index.scraping.ai_extract import extract_tiles_ai

        return extract_tiles_ai({"lobby": html}), ExtractionMode.DOM_AI
    from index.scraping.extractor import extract_tiles

    return extract_tiles(html, brand.selectors), ExtractionMode.DOM_CSS


def scrape_brand(brand) -> ScrapeOutcome:
    """Sniff network JSON first; fall back to DOM extraction on the same
    rendered page if nothing usable was captured within the sniff window.
    """
    from playwright.sync_api import sync_playwright  # imported lazily: only needed in live mode

    urls = [brand.homepage_url]
    if brand.live_casino_url:
        urls.append(brand.live_casino_url)
    for url in urls:
        if not crawl_allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
    time.sleep(crawl_delay(brand.homepage_url, settings.CRAWLER["DELAY_SECONDS"]))

    network_logs: list[NetworkCaptureLogEntry] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=settings.CRAWLER["USER_AGENT"])
            page.route("**/*", _block_heavy_assets)
            tiles = _sniff_network(page, urls, network_logs)
            if tiles:
                return ScrapeOutcome(tiles=tiles, extraction_mode=ExtractionMode.NETWORK_API, network_logs=network_logs)

            logger.info("%s: no usable network JSON within %ss, falling back to DOM", brand, NETWORK_SNIFF_TIMEOUT)
            html = page.content()
            tiles, mode = _dom_fallback(brand, page, html)
            return ScrapeOutcome(tiles=tiles, extraction_mode=mode, network_logs=network_logs)
        finally:
            browser.close()

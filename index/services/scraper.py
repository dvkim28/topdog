"""Primary scraping strategy: sniff the lobby/games JSON off the network,
instead of parsing the rendered DOM.

Most modern casino lobbies are client-side apps that hydrate from a JSON API
(`/games`, `/lobby`, `/tiles`, ...). Listening for that response is cheaper
and more reliable than parsing the DOM it eventually produces, and doesn't
break every time a class name changes. `scrape_brand()` opens the homepage
(and live-casino page, if configured) in a headless browser, listens on
`page.on("response")` for a matching JSON payload for up to
`NETWORK_SNIFF_TIMEOUT` seconds, and only falls back to DOM extraction (AI or
CSS) if nothing usable showed up in time - using each URL's own hydrated
HTML, captured while it was open, so a homepage + separate live-casino page
both feed the fallback instead of only whichever page was visited last.

Every response checked against the URL patterns is recorded as a
`NetworkCaptureLogEntry`, matched or not, so the admin log viewer and
`ScrapeLog.extraction_mode` can both show what actually happened.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from django.conf import settings

from index.models import Brand, ExtractionMode
from index.scraping.ai_client import AIExtractionError, clean_html
from index.scraping.diffing import hash_content
from index.scraping.extractor import Tile
from index.scraping.fetcher import RobotsDisallowed
from index.scraping.json_extract import extract_embedded_json_tiles
from index.scraping.json_payload import tiles_from_json
from index.scraping.robots import crawl_allowed, crawl_delay

logger = logging.getLogger(__name__)

NETWORK_SNIFF_TIMEOUT = float(getattr(settings, "CRAWLER", {}).get("NETWORK_SNIFF_TIMEOUT", 5))
NETWORK_URL_PATTERNS = getattr(
    settings, "CRAWLER", {}
).get("NETWORK_URL_PATTERNS", ["/games", "/lobby", "/tiles", "/api/casino", "/casino/api"])


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
    content_hash: str = ""
    # True when this brand's content hashed identical to Brand.content_hash -
    # the caller should skip matching/AI/DB-write entirely and replay
    # Brand.latest_snapshot instead. `tiles` is always [] when this is True.
    unchanged: bool = False


def _matched_pattern(url: str) -> str:
    for pattern in NETWORK_URL_PATTERNS:
        if pattern in url:
            return pattern
    return ""


def _canonical_tiles(tiles: list[Tile]) -> str:
    """Stable string form of a tile list, for hashing. Sorted so that the
    network sniffer folding responses in from multiple in-flight requests
    doesn't produce a different hash purely from response-arrival order.
    """
    rows = sorted((t.placement, t.raw_label.lower(), t.position) for t in tiles)
    return json.dumps(rows)


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


def _sniff_network(
    page, urls: list[str], network_logs: list[NetworkCaptureLogEntry]
) -> tuple[list[Tile], dict[str, str]]:
    """Navigate to each URL, listening for every matching JSON response, and
    merge tiles across all of them (deduped by placement + label).

    A lobby that paginates its API as you scroll fires several matching
    responses in one visit; taking only the first would silently drop
    everything after page one, so every usable payload is folded in.

    Also returns each URL's fully-hydrated HTML (post-scroll), captured right
    after that URL is visited - not just whichever page happens to still be
    loaded once the whole loop finishes. The DOM fallback needs both the
    homepage and the live-casino page's markup when they're separate URLs;
    grabbing `page.content()` only once after the loop silently threw away
    the homepage's content in favor of whatever URL was visited last.
    """
    tiles_by_key: dict[tuple[str, str], Tile] = {}
    htmls: dict[str, str] = {}

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
        tiles = tiles_from_json(response.url, payload)
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
            # "networkidle" here would mean the goto itself blocks until the
            # page goes quiet - on any page with a chat widget, analytics
            # beacon, or websocket that keeps polling forever, that condition
            # never fires and the whole scrape times out (seen live on
            # betinia.es). The explicit wait_for_timeout below is what
            # actually listens for the lobby JSON, so goto only needs the DOM
            # parsed, not the network idle.
            page.goto(url, wait_until="domcontentloaded", timeout=int(NETWORK_SNIFF_TIMEOUT * 1000) + 5000)
            page.wait_for_timeout(NETWORK_SNIFF_TIMEOUT * 1000)
            # Scrolling both mounts lazy DOM content (used later if the sniff
            # comes up empty) and gives infinite-scroll lobbies a chance to
            # fire the next page of the API this loop is listening for.
            _autoscroll(page)
            htmls[url] = page.content()
    finally:
        page.remove_listener("response", on_response)

    return list(tiles_by_key.values()), htmls


# Only the DOM/JSON matters here - stylesheets, images and fonts add load
# time and bandwidth without adding any extraction signal (labels come from
# attributes/text already present in the markup, not rendered pixels).
_BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}


def _block_heavy_assets(route):
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _merge_unique(tiles: list[Tile], more: list[Tile]) -> list[Tile]:
    """Append `more` onto `tiles`, skipping anything already present under
    the same (placement, label) key - used to fold a separate live-casino
    page's tiles in after the lobby's, whichever extraction method produced
    both lists.
    """
    seen = {(t.placement, t.raw_label.lower()) for t in tiles}
    for tile in more:
        key = (tile.placement, tile.raw_label.lower())
        if key not in seen:
            seen.add(key)
            tiles.append(tile)
    return tiles


def _self_heal_selectors(brand, html: str) -> None:
    """After a full AI extraction succeeds where the stored CSS selectors
    found nothing, ask the cheap model once for updated selectors and save
    them - so the *next* run can go back to the free CSS path (priority 2)
    instead of paying for full-page AI extraction (priority 3) every time.

    Best-effort only: this run's extraction already succeeded via AI above,
    so a failure here just means the next run tries AI again too - it never
    turns a working scrape into a failed one.
    """
    from index.scraping.ai_extract import suggest_selectors_ai

    try:
        new_selectors = suggest_selectors_ai(html)
    except AIExtractionError as exc:
        logger.warning("%s: selector self-heal failed, will retry AI extraction next run: %s", brand, exc)
        return
    if new_selectors:
        Brand.objects.filter(pk=brand.pk).update(selectors=new_selectors)
        logger.info("%s: learned new CSS selectors %s", brand, new_selectors)


def _dom_fallback(brand, htmls: dict[str, str]) -> tuple[list[Tile], str]:
    """Three-stage hybrid, cheapest first:

    1. Embedded structured JSON (ld+json / __NEXT_DATA__ / __INITIAL_STATE__) -
       free, and needs no selectors at all.
    2. Stored CSS selectors, parsed locally - free, 0 AI tokens.
    3. Full AI extraction, only if 1 and 2 both found nothing and the brand
       allows it - and only ever a single one-off call, since a successful
       AI pass immediately triggers `_self_heal_selectors` so the *next* run
       can go back to step 2.
    """
    lobby_html = htmls.get(brand.homepage_url, "")
    live_html = htmls.get(brand.live_casino_url, "") if brand.live_casino_url else ""
    has_separate_live_page = bool(live_html and live_html != lobby_html)

    tiles = extract_embedded_json_tiles(lobby_html)
    if has_separate_live_page:
        tiles = _merge_unique(tiles, extract_embedded_json_tiles(live_html))
    if tiles:
        return tiles, ExtractionMode.DOM_JSON

    from index.scraping.extractor import extract_tiles

    tiles = extract_tiles(lobby_html, brand.selectors)
    if has_separate_live_page:
        # A separate live-casino page's markup might match the live_section
        # selector even though it wasn't present in the lobby page's DOM.
        tiles = _merge_unique(tiles, extract_tiles(live_html, brand.selectors))
    if tiles:
        return tiles, ExtractionMode.DOM_CSS

    if not (brand.use_ai_extraction and settings.AI["ENABLED"]):
        return [], ExtractionMode.DOM_CSS

    from index.scraping.ai_extract import extract_tiles_ai

    pages = {"lobby": lobby_html}
    if live_html:
        pages["live"] = live_html
    tiles = extract_tiles_ai(pages)
    if tiles:
        _self_heal_selectors(brand, lobby_html)
    return tiles, ExtractionMode.DOM_AI


def scrape_brand(brand) -> ScrapeOutcome:
    """Sniff network JSON first; fall back to DOM extraction (using every
    URL's own hydrated HTML, not just whichever page loaded last) if nothing
    usable was captured within the sniff window.
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
            tiles, htmls = _sniff_network(page, urls, network_logs)
            if tiles:
                # Step 1 (network path): hash the sniffed tiles themselves -
                # already free to compute, and a cleaner "did the merchandised
                # games actually change" signal than the raw JSON bytes would
                # be (session ids / cache-busting params churn every request).
                content_hash = hash_content(_canonical_tiles(tiles))
                if content_hash == brand.content_hash:
                    return ScrapeOutcome(
                        tiles=[], extraction_mode=ExtractionMode.UNCHANGED, network_logs=network_logs,
                        content_hash=content_hash, unchanged=True,
                    )
                return ScrapeOutcome(
                    tiles=tiles, extraction_mode=ExtractionMode.NETWORK_API, network_logs=network_logs,
                    content_hash=content_hash,
                )

            logger.info("%s: no usable network JSON within %ss, falling back to DOM", brand, NETWORK_SNIFF_TIMEOUT)

            # Step 1 (DOM path): hash the cleaned HTML *before* running any
            # extraction at all (embedded JSON, then CSS, then AI) - if it's
            # byte-for-byte the same shape as the last successful run, every
            # one of those stages (and the Tier 1/2 matching and DB write
            # that would follow) is skipped outright.
            lobby_html = htmls.get(brand.homepage_url, "")
            live_html = htmls.get(brand.live_casino_url, "") if brand.live_casino_url else ""
            content_hash = hash_content(clean_html(lobby_html + live_html))
            if content_hash == brand.content_hash:
                return ScrapeOutcome(
                    tiles=[], extraction_mode=ExtractionMode.UNCHANGED, network_logs=network_logs,
                    content_hash=content_hash, unchanged=True,
                )

            tiles, mode = _dom_fallback(brand, htmls)
            return ScrapeOutcome(tiles=tiles, extraction_mode=mode, network_logs=network_logs, content_hash=content_hash)
        finally:
            browser.close()

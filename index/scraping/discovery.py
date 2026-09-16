"""Find licensed operators that aren't in the Brand table yet.

Runs against a regulator's public licence registry (BrandDiscoverySource),
once per source, before the nightly game scrape. New operators are created as
Paused by default so someone sets up selectors and reviews the domain before
it's scraped for games — flip `Region.auto_activate_discovered_brands` if you
trust a source enough to skip that step.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings
from django.utils.text import slugify

from .fetcher import RobotsDisallowed
from .robots import crawl_allowed, crawl_delay

logger = logging.getLogger(__name__)

CANDIDATE_LIMIT_PER_SOURCE = 40

# Some registries paginate ("page 2, 3, ..."), some lazy-load on scroll or a
# "load more" button. Both are capped so a misbehaving or infinite-scroll page
# can't turn one discovery source into an unbounded crawl.
MAX_PAGES = int(getattr(settings, "CRAWLER", {}).get("DISCOVERY_MAX_PAGES", 5))
MAX_SCROLLS = int(getattr(settings, "CRAWLER", {}).get("DISCOVERY_MAX_SCROLLS", 8))

# Multilingual, since regulator sites span the tracked markets (ES/MX/IT/DK/SE/RO/GR/...).
_NEXT_PAGE_PATTERN = re.compile(
    r"^(next|siguiente|pr[oó]xima|avanti|seguinte|f[oö]lgende|»|›|>)\s*$", re.IGNORECASE
)
_LOAD_MORE_PATTERN = re.compile(
    r"(load more|show more|cargar m[aá]s|ver m[aá]s|mostrar m[aá]s|carica altro|mostra altro)",
    re.IGNORECASE,
)
_PAGE_NAV_TIMEOUT_MS = 20_000

# Only the DOM matters for finding operator rows - stylesheets/images/fonts
# just cost bandwidth and load time on registry pages we only read text from.
_BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}


def _block_heavy_assets(route):
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


@dataclass
class Candidate:
    name: str
    domain: str
    licence_number: str = ""


def _clean_domain(raw: str) -> str | None:
    raw = raw.strip().lower()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    host = urlparse(raw).netloc or urlparse(raw).path
    host = host.split("@")[-1].split(":")[0]
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host):
        return None
    return host


def _find_next_page_locator(page, selector: str | None):
    """A configured `selectors.next_page` CSS selector wins; otherwise try
    common pagination markup, then fall back to matching link text.
    """
    candidates = []
    if selector:
        candidates.append(selector)
    candidates += ["a[rel='next']", "a.next", ".pagination a.next", "[aria-label*='next' i]"]
    for css in candidates:
        locator = page.locator(css).first
        if locator.count():
            return locator
    text_locator = page.get_by_text(_NEXT_PAGE_PATTERN).first
    return text_locator if text_locator.count() else None


def _find_load_more_locator(page, selector: str | None):
    candidates = []
    if selector:
        candidates.append(selector)
    candidates += ["button.load-more", "[data-testid*='load-more' i]"]
    for css in candidates:
        locator = page.locator(css).first
        if locator.count():
            return locator
    text_locator = page.get_by_text(_LOAD_MORE_PATTERN).first
    return text_locator if text_locator.count() else None


def _collect_registry_html(source) -> list[str]:
    """Render the registry page and follow pagination or lazy-loading.

    Classic "next page" pagination (a `selectors.next_page` CSS selector, or
    auto-detected rel="next" / common markup / link text) yields one HTML
    snapshot per page, up to `DISCOVERY_MAX_PAGES`. A page with no next-page
    link is instead treated as possible infinite-scroll / "load more":
    `selectors.load_more` (or auto-detection) is clicked, and failing that the
    page is scrolled to the bottom, repeated up to `DISCOVERY_MAX_SCROLLS`
    times or until the page stops growing - then a single, fully-loaded HTML
    snapshot is returned.
    """
    from playwright.sync_api import sync_playwright  # imported lazily: only needed in live mode

    if not crawl_allowed(source.discovery_url):
        raise RobotsDisallowed(f"robots.txt disallows {source.discovery_url}")
    time.sleep(crawl_delay(source.discovery_url, settings.CRAWLER["DELAY_SECONDS"]))

    selectors = source.selectors or {}
    pages_html: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=settings.CRAWLER["USER_AGENT"])
            page.route("**/*", _block_heavy_assets)
            page.goto(source.discovery_url, wait_until="networkidle", timeout=_PAGE_NAV_TIMEOUT_MS)
            pages_html.append(page.content())

            next_locator = _find_next_page_locator(page, selectors.get("next_page"))
            if next_locator:
                while len(pages_html) < MAX_PAGES:
                    if not crawl_allowed(page.url):
                        break
                    time.sleep(crawl_delay(page.url, settings.CRAWLER["DELAY_SECONDS"]))
                    try:
                        next_locator.click(timeout=_PAGE_NAV_TIMEOUT_MS)
                        page.wait_for_load_state("networkidle", timeout=_PAGE_NAV_TIMEOUT_MS)
                    except Exception:  # noqa: BLE001 - pagination ended or link went stale
                        break
                    pages_html.append(page.content())
                    next_locator = _find_next_page_locator(page, selectors.get("next_page"))
                    if not next_locator:
                        break
            else:
                last_height = page.evaluate("document.body.scrollHeight")
                for _ in range(MAX_SCROLLS):
                    load_more = _find_load_more_locator(page, selectors.get("load_more"))
                    if load_more:
                        try:
                            load_more.click(timeout=5000)
                        except Exception:  # noqa: BLE001 - button became stale/hidden
                            pass
                    else:
                        page.mouse.wheel(0, 20_000)
                    page.wait_for_timeout(1200)
                    new_height = page.evaluate("document.body.scrollHeight")
                    if new_height <= last_height and not load_more:
                        break
                    last_height = new_height
                pages_html[0] = page.content()  # single snapshot, now fully grown
        finally:
            browser.close()

    return pages_html


def _discover_live(source) -> list[Candidate]:
    """Render the registry page(s) - following pagination/lazy-load - then
    extract via AI or CSS depending on the source. Candidates are deduped by
    domain across pages.
    """
    if source.use_ai_extraction and not settings.AI["ENABLED"]:
        raise RuntimeError(
            "Source has use_ai_extraction=True but AI is disabled "
            "(set ANTHROPIC_API_KEY, or turn off use_ai_extraction to use CSS selectors)"
        )

    pages_html = _collect_registry_html(source)
    logger.info("%s: collected %s page(s)/batch(es) of registry HTML", source, len(pages_html))

    seen_domains: set[str] = set()
    candidates: list[Candidate] = []
    for html in pages_html:
        page_candidates = (
            _discover_live_ai(html) if source.use_ai_extraction else _discover_live_css(source, html)
        )
        for candidate in page_candidates:
            if candidate.domain in seen_domains:
                continue
            seen_domains.add(candidate.domain)
            candidates.append(candidate)
        if len(candidates) >= CANDIDATE_LIMIT_PER_SOURCE:
            break

    return candidates[:CANDIDATE_LIMIT_PER_SOURCE]


def _discover_live_ai(html: str) -> list[Candidate]:
    from .ai_extract import extract_brand_candidates_ai

    return extract_brand_candidates_ai(html)


def _discover_live_css(source, html: str) -> list[Candidate]:
    """CSS-selector fallback for sources with use_ai_extraction=False."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    sel = source.selectors or {}
    row_sel = sel.get("row", "tr")
    name_sel = sel.get("name", "td:nth-of-type(1)")
    domain_sel = sel.get("domain", "a")
    licence_sel = sel.get("licence")

    candidates = []
    for row in soup.select(row_sel)[:CANDIDATE_LIMIT_PER_SOURCE]:
        name_node = row.select_one(name_sel)
        domain_node = row.select_one(domain_sel)
        if not name_node or not domain_node:
            continue
        name = name_node.get_text(" ", strip=True)
        href = domain_node.get("href") or domain_node.get_text(" ", strip=True)
        domain = _clean_domain(href)
        if not name or not domain:
            continue
        licence = ""
        if licence_sel:
            licence_node = row.select_one(licence_sel)
            licence = licence_node.get_text(" ", strip=True) if licence_node else ""
        candidates.append(Candidate(name=name, domain=domain, licence_number=licence))
    return candidates


# Deterministic name pool so mock discovery produces plausible, stable results
# per region+day rather than gibberish.
_MOCK_STEMS = [
    "Rocket", "Golden", "Star", "Royal", "Lucky", "Nova", "Prime", "Vega",
    "Atlas", "Crown", "Silver", "Neon", "Zenith", "Orbit", "Falcon",
]
_MOCK_SUFFIXES = ["Play", "Casino", "Bet", "Spins", "Games"]


def _discover_mock(source, day_seed: int) -> list[Candidate]:
    """Deterministic fake registry. Mostly returns brands already seeded (so
    discovery correctly reports 'no new candidates' most nights), and
    occasionally introduces one genuinely new operator to exercise the flow.
    """
    rng = random.Random(f"discover:{source.region.code}:{day_seed}")
    tld = source.region.tld or ".com"
    candidates = []

    if rng.random() < 0.22:  # a new operator shows up roughly one night in five
        stem = rng.choice(_MOCK_STEMS)
        suffix = rng.choice(_MOCK_SUFFIXES)
        name = f"{stem}{suffix}"
        domain = f"{slugify(name)}{tld}"
        candidates.append(
            Candidate(name=name, domain=domain, licence_number=f"{source.region.code}-{rng.randint(1000,9999)}")
        )
    return candidates


def discover_candidates(source, mock: bool, day_seed: int = 0) -> list[Candidate]:
    return _discover_mock(source, day_seed) if mock else _discover_live(source)

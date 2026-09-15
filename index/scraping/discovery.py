"""Find licensed operators that aren't in the Brand table yet.

Runs against a regulator's public licence registry (BrandDiscoverySource),
once per source, before the nightly game scrape. New operators are created as
Paused by default so someone sets up selectors and reviews the domain before
it's scraped for games — flip `Region.auto_activate_discovered_brands` if you
trust a source enough to skip that step.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from django.utils.text import slugify

CANDIDATE_LIMIT_PER_SOURCE = 40


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


def _discover_live(source) -> list[Candidate]:
    """Fetch the registry page, then extract via AI or CSS depending on the source."""
    from .fetcher import fetch_homepage

    result = fetch_homepage(source.discovery_url)

    if source.use_ai_extraction:
        from django.conf import settings

        if not settings.AI["ENABLED"]:
            raise RuntimeError(
                "Source has use_ai_extraction=True but AI is disabled "
                "(set ANTHROPIC_API_KEY, or turn off use_ai_extraction to use CSS selectors)"
            )
        from .ai_extract import extract_brand_candidates_ai

        return extract_brand_candidates_ai(result.html)

    return _discover_live_css(source, result.html)


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

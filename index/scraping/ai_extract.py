"""Extraction backed by the AI model instead of CSS selectors.

Both functions return the same dataclasses the CSS-selector path already
produces (`Tile` from .extractor, `Candidate` from .discovery), so nothing
downstream — matching, dedup, HomepagePlacement writes, Brand creation — needs
to know or care which extraction method ran.
"""

from __future__ import annotations

from .ai_client import call_for_json, clean_html
from .discovery import Candidate
from .extractor import Tile

_GAME_SYSTEM_PROMPT = """\
You read the HTML of an online casino's homepage and live-casino section and \
identify which games are being merchandised on the page right now.

Return ONLY a JSON array, no prose, no markdown fences. Each element:
{
  "title": string,           // the game's display name exactly as shown on the page
  "placement": "hero" | "grid" | "live_section",
  "position": integer,       // 0-based order within its placement, left-to-right / top-to-bottom
  "source": "lobby" | "live" // which of the two provided pages it came from
}

Rules:
- "hero" = large featured banner/carousel at the top of the page.
- "live_section" = a live-dealer table (roulette, blackjack, game show) or anything from the page
  labelled "live" (source="live").
- "grid" = any other promoted game tile (top picks, popular, new, recommended).
- Only include titles that are clearly individual game names, not category headers, ads, or
  navigation links.
- If the same title appears in more than one placement, include it once per placement.
- If you cannot find any game tiles at all, return an empty array [].
- Do not invent titles that are not present in the HTML.
"""

_BRAND_SYSTEM_PROMPT = """\
You read the HTML of a gambling regulator's public list of licensed operators and extract each \
operator's name and website domain.

Return ONLY a JSON array, no prose, no markdown fences. Each element:
{
  "name": string,            // the operator's trading name as shown in the registry
  "domain": string,          // bare domain, e.g. "example.com" - no scheme, no path
  "licence_number": string   // the licence/permit number if shown, else ""
}

Rules:
- Only include rows that clearly represent a distinct licensed operator with a real domain.
- Skip rows that are headers, footnotes, or the regulator's own site.
- Deduplicate by domain.
- If you cannot find a usable list, return an empty array [].
- Do not invent operators that are not present in the HTML.
"""


def extract_tiles_ai(pages: dict[str, str]) -> list[Tile]:
    """pages: {"lobby": html, "live": html}. Either key may be omitted."""
    parts = []
    for source, html in pages.items():
        if html:
            parts.append(f"--- PAGE: {source} ---\n{clean_html(html)}")
    if not parts:
        return []

    user = "\n\n".join(parts)
    data = call_for_json(_GAME_SYSTEM_PROMPT, user)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array of tiles, got {type(data).__name__}")

    tiles = []
    for item in data:
        title = str(item.get("title", "")).strip()
        placement = item.get("placement")
        if not title or placement not in ("hero", "grid", "live_section"):
            continue
        tiles.append(
            Tile(raw_label=title[:200], placement=placement, position=int(item.get("position", 0)))
        )
    return tiles


def extract_brand_candidates_ai(html: str) -> list[Candidate]:
    data = call_for_json(_BRAND_SYSTEM_PROMPT, clean_html(html))
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array of candidates, got {type(data).__name__}")

    from .discovery import _clean_domain  # reuse the same domain validation

    candidates = []
    for item in data:
        name = str(item.get("name", "")).strip()
        domain = _clean_domain(str(item.get("domain", "")))
        if not name or not domain:
            continue
        candidates.append(
            Candidate(name=name[:120], domain=domain, licence_number=str(item.get("licence_number", ""))[:80])
        )
    return candidates

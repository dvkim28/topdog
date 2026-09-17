"""Extraction backed by the AI model instead of CSS selectors.

Both functions return the same dataclasses the CSS-selector path already
produces (`Tile` from .extractor, `Candidate` from .discovery), so nothing
downstream — matching, dedup, HomepagePlacement writes, Brand creation — needs
to know or care which extraction method ran.
"""

from __future__ import annotations

from django.conf import settings

from .ai_client import AIExtractionError, call_for_json, clean_html
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
  "source": "lobby" | "live", // which of the two provided pages it came from
  "provider": string         // the studio/provider name if it's visibly attached to this specific
                              // tile (a badge, logo alt text, or label naming the studio) - "" if the
                              // page doesn't show one for this tile. Never guess or infer this from
                              // the title alone - only report what the page actually states.
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

_SELECTOR_SYSTEM_PROMPT = """\
You read the HTML of an online casino homepage and find CSS selectors that a scraper can reuse to keep \
extracting the games shown on it, without needing you again unless the layout changes.

Return ONLY a JSON object, no prose, no markdown fences:
{
  "hero": string,          // CSS selector for the hero/carousel tile cards, "" if that section isn't present
  "grid": string,          // CSS selector for the top-picks / popular / recommended grid tile cards, "" if absent
  "live_section": string   // CSS selector for live-dealer table cards, "" if absent
}

Rules:
- Each selector must match the repeating CARD container element for that section (one match per tile) -
  not the section's outer wrapper, and not an inner title/image/badge element within a card.
- Prefer a stable hook shared by every card of that kind (a distinctive class name or a data-* attribute)
  over a brittle one (nth-child position, a generated/hashed class, a single-use id).
- Leave a key as "" rather than guessing if that section genuinely isn't on the page.
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
    data = call_for_json(_GAME_SYSTEM_PROMPT, user, thinking=False)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array of tiles, got {type(data).__name__}")

    tiles = []
    for item in data:
        title = str(item.get("title", "")).strip()
        placement = item.get("placement")
        if not title or placement not in ("hero", "grid", "live_section"):
            continue
        tiles.append(
            Tile(
                raw_label=title[:200],
                placement=placement,
                position=int(item.get("position", 0)),
                provider=str(item.get("provider", "") or "").strip()[:120],
            )
        )
    return tiles


def suggest_selectors_ai(html: str) -> dict[str, str]:
    """One-off self-heal call after the brand's stored CSS selectors stopped
    matching anything (the site's markup changed). Runs on
    settings.AI["MATCH_MODEL"] - the cheap Haiku-tier model already used for
    Tier 2 title matching - because finding a handful of selectors is a small
    structured read, not open-ended extraction, so it doesn't need
    extraction-grade rates.

    The caller is expected to save the result onto Brand.selectors: every
    run after this one goes back to parsing locally for 0 AI tokens, until
    the layout drifts again and this fires once more.
    """
    data = call_for_json(_SELECTOR_SYSTEM_PROMPT, clean_html(html), model=settings.AI["MATCH_MODEL"], thinking=False)
    if not isinstance(data, dict):
        raise AIExtractionError(f"Expected a JSON object of selectors, got {type(data).__name__}")

    # extract_tiles() runs each placement's selector as an independent pass
    # over the whole DOM - if the model can't actually tell two sections
    # apart and returns the same selector for both (seen live on betinia.es:
    # "hero" and "grid" both came back as "stb-ui-thumbnail"), every tile
    # would otherwise be counted once per placement that shares it, doubling
    # (or worse) the real count. Keep only the first placement to claim a
    # given selector string.
    seen_values: set[str] = set()
    selectors: dict[str, str] = {}
    for key in ("grid", "live_section", "hero"):
        value = str(data.get(key, "") or "").strip()
        if value and value not in seen_values:
            selectors[key] = value
            seen_values.add(value)
    return selectors


def extract_brand_candidates_ai(html: str) -> list[Candidate]:
    data = call_for_json(_BRAND_SYSTEM_PROMPT, clean_html(html), thinking=False)
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

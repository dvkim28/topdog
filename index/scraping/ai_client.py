"""Thin wrapper around the Anthropic API for HTML-to-structured-data extraction.

Both discovery (find brands) and game extraction (find popular games) reduce to
the same shape: clean up a page's HTML, ask the model for JSON matching a
schema, parse it strictly. If the model doesn't return valid JSON, or returns
something that fails validation, this raises — callers treat that exactly like
a fetch failure. The AI is never allowed to write to the database directly;
it only ever produces candidate data that flows through the same matching and
validation code a CSS-selector extraction would.
"""

from __future__ import annotations

import json
import logging
import re

from django.conf import settings
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_SCRIPT_STYLE = re.compile(r"<(script|style|svg|noscript|template)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_HEAD = re.compile(r"<head\b.*?</head>", re.IGNORECASE | re.DOTALL)
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
_MANY_BLANKS = re.compile(r"\n\s*\n+")
_LEADING_WS = re.compile(r"^[ \t]+", re.MULTILINE)
# Plumbing attributes: pure styling hooks or image/link URL bytes, no
# game-name signal. On a real lobby these routinely dwarf the actual text -
# a modern component tree's utility-class soup (`class="..."`) alone can be
# most of a page's characters, and unlike `srcset`/`sizes` (already gone),
# a plain `src`/`href` URL with CDN query params adds up fast too. Stripping
# these before truncating is what keeps the actual game grid inside
# MAX_HTML_CHARS instead of being pushed past the cut. `alt`/`title`/
# `aria-label`/`data-game-name`/`data-title` (where labels actually live,
# per extractor.TITLE_ATTRS) are deliberately left off this list.
_NOISE_ATTRS = re.compile(
    r'\s+(?:class|style|srcset|sizes|src|href|poster|data-src|data-srcset|'
    r'loading|decoding|width|height|role|tabindex|data-testid|data-qa|'
    r'media|type|dir|palette)='
    r'"[^"]*"',
    re.IGNORECASE,
)
# `aria-*` accessibility plumbing (aria-hidden, aria-expanded, aria-current,
# ...) and analytics tracking hooks - neither ever carries a game name.
# `aria-label` is excluded (it's one of extractor.TITLE_ATTRS).
_NOISE_ATTR_PREFIXES = re.compile(r'\s+(?:aria-(?!label=)[\w-]+|data-analytics-[\w-]+)="[^"]*"', re.IGNORECASE)
# Framework-internal marker attributes (Angular's `_ngcontent-*`/`_nghost-*`
# view-encapsulation hashes, Vue's `data-v-*`, custom UI-kit directive flags,
# ...) are the single biggest source of bloat on a component-framework lobby
# page - seen live on betinia.es, where these alone were >1/3 of the cleaned
# HTML. Rather than enumerate every framework's naming scheme, this strips
# any attribute left with an empty value: a value-less marker can never hold
# a game name by definition, on any framework, so it's a safe general rule
# rather than a per-site hack.
_EMPTY_VALUED_ATTRS = re.compile(r'\s+[a-zA-Z_:][-a-zA-Z0-9_:.]*=""')


class AIExtractionError(Exception):
    """Raised on anything from a missing API key to invalid model output."""


def clean_html(html: str, max_chars: int | None = None) -> str:
    """Strip the parts of a page that add tokens without adding signal."""
    max_chars = max_chars or settings.AI["MAX_HTML_CHARS"]
    text = _HEAD.sub("", html)  # <head> never contains game tiles
    text = _SCRIPT_STYLE.sub("", text)
    text = _COMMENTS.sub("", text)
    text = _NOISE_ATTRS.sub("", text)
    text = _NOISE_ATTR_PREFIXES.sub("", text)
    text = _EMPTY_VALUED_ATTRS.sub("", text)
    text = _MANY_BLANKS.sub("\n", text)
    text = _LEADING_WS.sub("", text)  # deeply nested component trees indent every line
    if len(text) > max_chars:
        # Silent truncation here means silently missing games further down
        # the page (or with `use_ai_extraction` off, only spotted by an
        # operator noticing a suspiciously low games_found). Log it so a
        # truncated lobby shows up as a signal, not just a gap in the data.
        logger.warning(
            "clean_html: truncating %s -> %s chars; extraction may miss content past the cut",
            len(text), max_chars,
        )
        text = text[:max_chars] + "\n<!-- truncated -->"
    return text


def _client():
    if not settings.AI["ENABLED"]:
        raise AIExtractionError("AI extraction is disabled (AI_ENABLED=0 or no ANTHROPIC_API_KEY set)")
    import anthropic  # imported lazily so the package is only required when AI mode is on

    return anthropic.Anthropic(api_key=settings.AI["API_KEY"])


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=2, max=10))
def call_for_json(system: str, user: str, *, model: str | None = None, thinking: bool = True) -> list | dict:
    """Call Claude, require pure JSON back, parse it. Raises on anything else.

    `model` lets a caller opt into a cheaper model for simpler tasks (see
    services/normalization.py's Tier 2 matcher) instead of always paying
    extraction-grade rates; it defaults to settings.AI["MODEL"].

    `thinking=False` explicitly disables adaptive thinking (Sonnet-tier
    models default it on when the param is omitted). Every prompt in this
    module is "read the input, produce a JSON list matching a schema" -
    pattern extraction, not multi-step reasoning - so thinking buys nothing
    here, and it shares the same `max_tokens` budget as the actual output:
    on a large lobby page, adaptive thinking can burn the *entire* budget
    before writing a single character of JSON (seen live: a 4096-token
    response with stop_reason "max_tokens" and 0 output characters, all
    4096 spent on thinking), silently truncating the tile list. Disabling it
    guarantees the full budget goes to the JSON itself. Left as the default
    (omit the param) for callers on a model that doesn't take it - only
    extraction currently opts out.

    The system prompt is marked cacheable: every call site here reuses one of
    a handful of fixed prompts (game/brand extraction, Tier 2 matching)
    across every brand scraped in a night, so after the first call each
    subsequent one reads the prompt from cache at a fraction of its input
    cost instead of paying full price for the same tokens again.
    """
    client = _client()
    request = {
        "model": model or settings.AI["MODEL"],
        "max_tokens": settings.AI["MAX_TOKENS"],
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    if not thinking:
        request["thinking"] = {"type": "disabled"}
    try:
        response = client.messages.create(**request)
    except Exception as exc:  # noqa: BLE001 - surfaced as one clear error type to callers
        raise AIExtractionError(f"Anthropic API call failed: {exc}") from exc

    if response.stop_reason == "max_tokens":
        logger.warning(
            "Claude call (%s) hit max_tokens (%s) - output was truncated, results may be incomplete",
            response.model, settings.AI["MAX_TOKENS"],
        )

    usage = response.usage
    logger.debug(
        "Claude call (%s): %s input, %s cache read, %s cache write, %s output",
        response.model, usage.input_tokens, usage.cache_read_input_tokens,
        usage.cache_creation_input_tokens, usage.output_tokens,
    )

    text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    raw = "".join(text_blocks).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIExtractionError(f"Model did not return valid JSON: {exc}. Raw: {raw[:300]}") from exc

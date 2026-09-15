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
import re

from django.conf import settings
from tenacity import retry, stop_after_attempt, wait_exponential

_SCRIPT_STYLE = re.compile(r"<(script|style|svg|noscript)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
_MANY_BLANKS = re.compile(r"\n\s*\n+")


class AIExtractionError(Exception):
    """Raised on anything from a missing API key to invalid model output."""


def clean_html(html: str, max_chars: int | None = None) -> str:
    """Strip the parts of a page that add tokens without adding signal."""
    max_chars = max_chars or settings.AI["MAX_HTML_CHARS"]
    text = _SCRIPT_STYLE.sub("", html)
    text = _COMMENTS.sub("", text)
    text = _MANY_BLANKS.sub("\n", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n<!-- truncated -->"
    return text


def _client():
    if not settings.AI["ENABLED"]:
        raise AIExtractionError("AI extraction is disabled (AI_ENABLED=0 or no ANTHROPIC_API_KEY set)")
    import anthropic  # imported lazily so the package is only required when AI mode is on

    return anthropic.Anthropic(api_key=settings.AI["API_KEY"])


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=2, max=10))
def call_for_json(system: str, user: str) -> list | dict:
    """Call Claude, require pure JSON back, parse it. Raises on anything else."""
    client = _client()
    try:
        response = client.messages.create(
            model=settings.AI["MODEL"],
            max_tokens=settings.AI["MAX_TOKENS"],
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as one clear error type to callers
        raise AIExtractionError(f"Anthropic API call failed: {exc}") from exc

    text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    raw = "".join(text_blocks).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIExtractionError(f"Model did not return valid JSON: {exc}. Raw: {raw[:300]}") from exc

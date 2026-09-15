"""Polite HTTP fetch of a single brand main page."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from django.conf import settings
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .robots import crawl_allowed, crawl_delay

logger = logging.getLogger(__name__)


class RobotsDisallowed(Exception):
    pass


@dataclass
class FetchResult:
    html: str
    http_status: int
    duration_ms: int


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
def _get(url: str, headers: dict) -> httpx.Response:
    with httpx.Client(follow_redirects=True, timeout=settings.CRAWLER["TIMEOUT"]) as client:
        return client.get(url, headers=headers)


def fetch_homepage(url: str) -> FetchResult:
    if not crawl_allowed(url):
        raise RobotsDisallowed(f"robots.txt disallows {url}")

    # One page per brand per night, plus a courtesy pause before the request.
    time.sleep(crawl_delay(url, settings.CRAWLER["DELAY_SECONDS"]))

    headers = {
        "User-Agent": settings.CRAWLER["USER_AGENT"],
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "es-ES,es;q=0.9",
    }

    started = time.monotonic()
    response = _get(url, headers)
    duration_ms = int((time.monotonic() - started) * 1000)
    response.raise_for_status()
    return FetchResult(html=response.text, http_status=response.status_code, duration_ms=duration_ms)

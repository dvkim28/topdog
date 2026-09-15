"""robots.txt gate. Nothing is fetched unless the operator allows it."""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)
CACHE_TTL = 60 * 60 * 12


def _robots_url(page_url: str) -> str:
    parts = urlparse(page_url)
    return urljoin(f"{parts.scheme}://{parts.netloc}", "/robots.txt")


def crawl_allowed(page_url: str, user_agent: str | None = None) -> bool:
    if not settings.CRAWLER["RESPECT_ROBOTS"]:
        return True

    agent = user_agent or settings.CRAWLER["USER_AGENT"]
    robots_url = _robots_url(page_url)
    body = cache.get(robots_url)

    if body is None:
        try:
            response = httpx.get(
                robots_url,
                timeout=10,
                headers={"User-Agent": agent},
                follow_redirects=True,
            )
            body = response.text if response.status_code == 200 else ""
        except httpx.HTTPError as exc:
            logger.warning("robots.txt unreachable for %s: %s", robots_url, exc)
            # Unreachable robots.txt is treated as disallow: fail closed.
            return False
        cache.set(robots_url, body, CACHE_TTL)

    parser = RobotFileParser()
    parser.parse(body.splitlines())
    return parser.can_fetch(agent, page_url)


def crawl_delay(page_url: str, default: float) -> float:
    """Honour Crawl-delay when the operator publishes one."""
    body = cache.get(_robots_url(page_url))
    if not body:
        return default
    parser = RobotFileParser()
    parser.parse(body.splitlines())
    declared = parser.crawl_delay(settings.CRAWLER["USER_AGENT"])
    return max(float(declared), default) if declared else default

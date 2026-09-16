"""Shared crawl-etiquette exception.

Both live scraping paths (index/services/scraper.py for brand homepages,
index/scraping/discovery.py for registry pages) render pages with Playwright
rather than a plain HTTP fetch, but both check `robots.py`'s `crawl_allowed`
first and raise this when it says no.
"""

from __future__ import annotations


class RobotsDisallowed(Exception):
    pass

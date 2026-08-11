"""Runtime robots.txt compliance check.

Run before scraping each county. If robots.txt disallows the paths we need,
the county is skipped and the reason is logged — the run report surfaces it so
a human can decide what to do (e.g. request data another way).
"""

from __future__ import annotations

import logging
import urllib.robotparser
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

USER_AGENT = "MADDAssetsTaxDeedResearch/0.1 (public records research; contact site owner via county)"

# The paths the scraper actually requests.
CHECK_PATHS = [
    "/index.cgi?zaction=USER&zmethod=CALENDAR",
    "/index.cgi?zaction=AUCTION&Zmethod=PREVIEW",
    "/index.cgi",
    "/",
]


def check_robots(base_url: str, timeout: float = 15.0) -> dict:
    """Return {'allowed': bool, 'detail': str, 'crawl_delay': float|None}."""
    robots_url = urljoin(base_url, "/robots.txt")
    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = requests.get(robots_url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as e:
        return {"allowed": True, "detail": f"robots.txt unreachable ({e.__class__.__name__}); proceeding politely", "crawl_delay": None}
    if resp.status_code >= 400:
        return {"allowed": True, "detail": f"robots.txt HTTP {resp.status_code}; treating as no restrictions", "crawl_delay": None}

    rp.parse(resp.text.splitlines())
    blocked = [p for p in CHECK_PATHS if not rp.can_fetch(USER_AGENT, urljoin(base_url, p))]
    delay = None
    try:
        delay = rp.crawl_delay(USER_AGENT)
    except Exception:
        pass
    if blocked:
        return {
            "allowed": False,
            "detail": f"robots.txt disallows required paths: {blocked}",
            "crawl_delay": delay,
        }
    return {"allowed": True, "detail": "robots.txt permits required paths", "crawl_delay": delay}

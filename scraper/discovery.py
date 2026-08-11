"""Build the authoritative Florida taxdeed county list from the site's own
county selector (bottom-right dropdown on any RealAuction site).

We never trust a hardcoded county list: the selector is the source of truth,
and only 'FL ... Taxdeed' entries on *.realtaxdeed.com hosts are kept.
Foreclosure entries (realforeclose.com) and non-FL states are rejected and
logged so a human can audit the filtering.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .parsing import filter_fl_taxdeed, parse_county_selector

log = logging.getLogger(__name__)

SEED_URL = "https://www.volusia.realtaxdeed.com/"


def discover_counties(source, seed_slug: str = "volusia", seed_url: str = SEED_URL) -> dict:
    html = source.home_html(seed_slug, seed_url)
    if not html:
        raise RuntimeError(f"Could not load seed page {seed_url} for county discovery")
    entries = parse_county_selector(html, base_url=seed_url)
    if not entries:
        raise RuntimeError(
            "County selector not found on seed page — the site structure may have "
            "changed. Save the page HTML and inspect for the county dropdown."
        )
    counties, rejected = filter_fl_taxdeed(entries)
    log.info("Discovered %d FL taxdeed counties (%d selector entries rejected)", len(counties), len(rejected))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed_url": seed_url,
        "counties": counties,
        "rejected": rejected,
    }


def save_counties(result: dict, path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    log.info("Wrote %s (%d counties)", path, len(result["counties"]))


def load_counties(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["counties"]

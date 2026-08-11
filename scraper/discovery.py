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
import re
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
    # The splash page's jump menu omits the site you are already on, so the
    # seed county must be added explicitly.
    from urllib.parse import urlparse
    seed_host = urlparse(seed_url).netloc.lower()
    if seed_host.endswith("realtaxdeed.com") and not any(c["host"] == seed_host for c in counties):
        slug = seed_host.replace("www.", "").split(".")[0]
        counties.append({"slug": slug, "name": slug.title(), "url": seed_url.rstrip("/"),
                         "host": seed_host, "label": f"{slug.title()} Taxdeed (seed site)"})
        counties.sort(key=lambda c: c["slug"])
    log.info("Discovered %d FL taxdeed counties (%d selector entries rejected)", len(counties), len(rejected))

    # Cross-check against the client's authoritative county list, if present.
    client_missing: list[str] = []
    client_list = Path(__file__).resolve().parent.parent / "config" / "client_counties.txt"
    if client_list.exists():
        wanted = [ln.strip() for ln in client_list.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
        have = {c["slug"] for c in counties}
        client_missing = [w for w in wanted
                          if re.sub(r"[^a-z0-9]", "", w.lower()) not in have]
        if client_missing:
            log.warning("Client counties NOT found by discovery: %s", client_missing)
        else:
            log.info("All %d client counties present in discovery", len(wanted))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed_url": seed_url,
        "counties": counties,
        "rejected": rejected,
        "client_list_missing": client_missing,
    }


def save_counties(result: dict, path: str | Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    log.info("Wrote %s (%d counties)", path, len(result["counties"]))


def load_counties(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["counties"]

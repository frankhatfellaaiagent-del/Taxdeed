"""Address → lat/lng via the free US Census batch geocoder.

The feed's street addresses ("1230 CALDWELL DR, PANAMA CITY, FL- 32401") are
resolved to coordinates so the dashboard can deep-link parcel-centered maps
(wetlands, FEMA flood, satellite). Results are cached in data/geocache.json —
weekly runs only geocode addresses they have not seen before, and a network
failure degrades to whatever the cache already holds. No API key required.

Census batch endpoint docs: https://geocoding.geo.census.gov/geocoder/
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from pathlib import Path

import requests

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = ROOT / "data" / "geocache.json"

BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BATCH_SIZE = 500          # well under the 10k API limit; keeps retries cheap
TIMEOUT = 300             # the batch endpoint is slow on big files

# "STREET, CITY, FL- 32401" (sometimes "CITY FL, FL- 32405" or extra commas)
_ZIP_RE = re.compile(r"\bFL[-\s]*\s*(\d{5})\s*$", re.I)
_NO_SITUS_RE = re.compile(r"^\s*(NO\s+SITUS|0*\s*UNASSIGNED\s+LOC)", re.I)


def parse_address(addr: str) -> tuple[str, str, str] | None:
    """Split a feed address into (street, city, zip); None if not geocodable."""
    if not addr or _NO_SITUS_RE.search(addr):
        return None
    m = _ZIP_RE.search(addr)
    if not m:
        return None
    zip5 = m.group(1)
    head = addr[: m.start()].rstrip(" ,-")
    parts = [p.strip() for p in head.split(",") if p.strip()]
    if not parts:
        return None
    street = parts[0]
    # City is the last remaining part; strip a trailing state abbreviation
    # ("PANAMA CITY FL" → "PANAMA CITY").
    city = re.sub(r"\s+FL$", "", parts[-1], flags=re.I) if len(parts) > 1 else ""
    return street, city, zip5


def load_cache(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CACHE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("geocache unreadable, starting fresh: %s", p)
    return {}


def save_cache(cache: dict, path: str | Path | None = None) -> None:
    p = Path(path) if path else DEFAULT_CACHE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")


def _batch_lookup(items: list[tuple[str, tuple[str, str, str]]]) -> dict:
    """One Census batch call. items = [(cache_key, (street, city, zip)), ...].

    Returns {cache_key: [lat, lng] | None}."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, (_, (street, city, zip5)) in enumerate(items):
        w.writerow([i, street, city, "FL", zip5])
    resp = requests.post(
        BATCH_URL,
        data={"benchmark": "Public_AR_Current"},
        files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    out: dict = {}
    for row in csv.reader(io.StringIO(resp.text)):
        # id, input, match status, match type, matched addr, "lng,lat", ...
        if len(row) < 6 or row[2].strip().lower() != "match":
            if row and row[0].isdigit():
                out[items[int(row[0])][0]] = None
            continue
        try:
            lng, lat = (float(v) for v in row[5].split(","))
            out[items[int(row[0])][0]] = [round(lat, 6), round(lng, 6)]
        except (ValueError, IndexError):
            out[items[int(row[0])][0]] = None
    # Anything the response skipped entirely counts as a miss for this run
    # (left out of `out` so it is retried next run rather than cached as null).
    return out


def geocode_addresses(addresses: list[str], cache_path: str | Path | None = None) -> dict:
    """Return {address: [lat, lng] | None} for every input address.

    Cached results are reused; only new addresses hit the Census API. A network
    error logs a warning and returns cache-only results — never raises.
    """
    cache = load_cache(cache_path)
    todo: dict[str, tuple[str, str, str]] = {}
    for addr in addresses:
        if not addr or addr in cache or addr in todo:
            continue
        parsed = parse_address(addr)
        if parsed is None:
            cache[addr] = None      # structurally ungeocodable — remember that
        else:
            todo[addr] = parsed

    if todo:
        items = list(todo.items())
        log.info("Geocoding %d new addresses via Census batch API", len(items))
        resolved = 0
        for i in range(0, len(items), BATCH_SIZE):
            chunk = items[i : i + BATCH_SIZE]
            try:
                got = _batch_lookup(chunk)
            except requests.RequestException as exc:
                log.warning("Census geocoder unavailable (%s); using cache only", exc)
                break
            cache.update(got)
            resolved += sum(1 for v in got.values() if v)
        log.info("Geocoded %d/%d new addresses", resolved, len(items))
        save_cache(cache, cache_path)

    return {a: cache.get(a) for a in addresses}

"""ReportAll USA parcel API — the data behind LandGlide.

LandGlide has no public deep link, but its parent company sells the parcel
database directly, which is the part that matters: true parcel boundaries and
centroids, owner, component mailing address, acreage — for parcels our
appraiser scraping misses or where the county publishes no street address at
all (vacant land, the bulk of the client's buy box).

Ships dormant. Without REPORTALL_API_KEY in the environment every call is a
no-op, so the scrape and the feed behave exactly as before. To turn it on:
ReportAll's free 30-day trial includes 1,000 parcel lookups — enough to prove
it on real buy-box parcels — then add the key as the REPORTALL_API_KEY GitHub
Actions secret.

API (v9): https://reportallusa.com/api/parcels?client=<key>&v=9&...
  region=<County, ST>&parcel_id=<apn>   parcel by county + APN
  q=<free text address>                 address search
  spatial_intersect=POINT(lng lat)&si_srid=4326   parcel containing a point
Results are cached permanently in the enrichment store, so no parcel is ever
paid for twice.
"""

from __future__ import annotations

import logging
import os
import re

import requests

log = logging.getLogger(__name__)

API_URL = "https://reportallusa.com/api/parcels"
API_VERSION = "9"
TIMEOUT = 30
ENV_KEY = "REPORTALL_API_KEY"

# Field names vary across their schema versions; take the first that appears.
FIELD_ALIASES = {
    "owner": ["owner", "owner_name", "owner1"],
    "mailing_address": ["mail_address1", "mailing_address", "mail_addr1", "addr_street"],
    "mailing_city": ["mail_city", "mailing_city"],
    "mailing_state": ["mail_state2", "mail_state", "mailing_state"],
    "mailing_zip": ["mail_zip", "mailing_zip", "mail_zipcode"],
    "acreage": ["acreage_calc", "acreage", "gis_acres", "acres"],
    "land_use": ["land_use_class", "land_use_code", "usedesc", "std_land_use"],
    "parcel_address": ["physical_address", "site_address", "address"],
    "county": ["county_name", "county"],
    "lat": ["latitude", "lat", "centroid_lat"],
    "lng": ["longitude", "lng", "lon", "centroid_lon"],
}


def api_key() -> str | None:
    key = (os.environ.get(ENV_KEY) or "").strip()
    return key or None


def enabled() -> bool:
    return api_key() is not None


def _pick(row: dict, names: list[str]):
    for n in names:
        if row.get(n) not in (None, "", "null"):
            return row[n]
    return None


def normalize(row: dict) -> dict:
    """One API result row → the fields the feed cares about."""
    out: dict = {}
    for field, names in FIELD_ALIASES.items():
        val = _pick(row, names)
        if val is None:
            continue
        if field in ("lat", "lng"):
            try:
                out[field] = round(float(val), 6)
            except (TypeError, ValueError):
                continue
        else:
            out[field] = str(val).strip()[:200]
    mail = " ".join(str(out.pop(k, "")) for k in
                    ("mailing_address", "mailing_city", "mailing_state", "mailing_zip"))
    mail = re.sub(r"\s+", " ", mail).strip()
    if mail:
        out["mailing_address"] = mail
    return out


def _request(params: dict) -> list[dict]:
    key = api_key()
    if not key:
        return []
    query = {"client": key, "v": API_VERSION, **params}
    try:
        resp = requests.get(API_URL, params=query, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("ReportAll lookup failed (%s)", str(exc)[:160])
        return []
    if isinstance(data, dict):
        if data.get("status") and str(data["status"]).lower() not in ("ok", "success"):
            log.warning("ReportAll returned status=%s", data.get("status"))
        return data.get("results") or data.get("features") or []
    return data if isinstance(data, list) else []


def lookup(county: str = "", parcel_id: str = "", address: str = "",
           lat: float | None = None, lng: float | None = None) -> dict:
    """Best available parcel record for one property ({} when not found).

    Tries county+APN first (most precise), then a point-in-parcel lookup, then
    a free-text address search."""
    if not enabled():
        return {}
    attempts: list[dict] = []
    if county and parcel_id:
        attempts.append({"region": f"{county.title()} County, FL", "parcel_id": parcel_id})
    if lat is not None and lng is not None:
        attempts.append({"spatial_intersect": f"POINT({lng} {lat})", "si_srid": "4326"})
    if address:
        attempts.append({"q": f"{address}, Florida"})
    for params in attempts:
        rows = _request(params)
        if rows:
            row = rows[0]
            # ArcGIS-style responses nest the record under "attributes".
            record = row.get("attributes") if isinstance(row.get("attributes"), dict) else row
            norm = normalize(record)
            if norm:
                norm["source"] = "reportall"
                return norm
    return {}

"""Appraiser "quick look" enrichment — the first-wave scrub agreed with MADD.

For scheduled auctions that pass the buy-box (MATCH/REVIEW), fetch the parcel's
property appraiser page and pull the fields the auction site doesn't publish:
owner of record, mailing address, land use, acreage, and appraiser values.
Results land in data/enrichment.json (incremental across runs, keyed like the
dashboard: "county|parcel|case") and the exporter merges them into the feed.

County appraiser sites run on a handful of vendor platforms (qPublic/Beacon,
CamaDisplay, custom ASP.NET), so extraction is a generic label scanner over
tables/definition lists rather than per-county parsers; counties whose pages
defeat it simply stay unenriched (ok=false with the reason). Run with
--debug-dir to snapshot raw HTML for tuning new counties.

This module needs open egress to county sites — it runs in the weekly GitHub
Actions workflow (continue-on-error), never in the browser dashboard.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "enrichment.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 25
HOST_DELAY = 2.0          # seconds between requests to the same host

# Label → enrichment field. Matched case-insensitively against the *leading*
# text of a th/td/dt/label cell; the value is the adjacent cell's text.
LABELS = {
    "owner_name": ["owner name", "owner(s)", "owner of record", "owners", "owner"],
    "mailing_address": ["mailing address", "owner address", "mail address"],
    "property_use": ["property use code", "property use", "land use code",
                     "land use", "use code", "dor use code", "property class",
                     "use description", "dor code"],
    "acreage": ["total acreage", "acreage", "gross acres", "total acres",
                "land size", "acres"],
    "just_value": ["just (market) value", "just value", "market value",
                   "total market value"],
    "assessed_value": ["assessed value", "total assessed value"],
}


def record_key(r: dict) -> str:
    return f"{r.get('county', '')}|{r.get('parcel_id', '')}|{r.get('case_number', '')}"


def select_targets(records: list[dict], counties: list[str] | None = None,
                   limit: int | None = None) -> list[dict]:
    """Scheduled + buy-box MATCH/REVIEW + has an appraiser link, soonest first."""
    def date_key(r):
        d = r.get("sale_date") or ""
        return d[6:] + d[:2] + d[3:5] if len(d) == 10 else "99999999"

    out = [r for r in records
           if r.get("status") != "Redeemed"
           and r.get("buybox") in ("MATCH", "REVIEW")
           and r.get("appraiser_url")
           and (not counties or r.get("county") in counties)]
    out.sort(key=date_key)
    return out[:limit] if limit else out


def _cell_text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _value_after(el):
    """The natural 'value' cell for a label cell: next td/dd sibling, or the
    remainder of the row."""
    for sib in el.find_next_siblings(["td", "th", "dd", "div", "span"]):
        txt = _cell_text(sib)
        if txt:
            return txt
    return None


def parse_appraiser_page(html: str) -> dict:
    """Generic label→value scan. Returns whichever LABELS fields it finds."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    found: dict[str, str] = {}
    cells = soup.find_all(["th", "td", "dt", "label", "strong", "b", "span", "div"])
    for cell in cells:
        # Label cells are short; skip containers that hold whole tables.
        if len(cell.find_all(["td", "th", "tr", "div"])) > 2:
            continue
        text = _cell_text(cell).rstrip(":").lower()
        if not text or len(text) > 40:
            continue
        for field, names in LABELS.items():
            if field in found:
                continue
            if any(text == n or text.startswith(n + " ") for n in names):
                value = _value_after(cell)
                if value and value.rstrip(":").lower() != text:
                    found[field] = value[:200]
    # Normalize money fields to floats when they look like dollars.
    for f in ("just_value", "assessed_value"):
        if f in found:
            m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", found[f])
            found[f] = float(m.group(1).replace(",", "")) if m else found.pop(f, None)
            if found.get(f) is None:
                found.pop(f, None)
    return found


def load_enrichment(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_OUT
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("enrichment store unreadable, starting fresh: %s", p)
    return {}


def enrich_records(records: list[dict], counties: list[str] | None = None,
                   limit: int | None = 200, out_path: str | Path | None = None,
                   debug_dir: str | Path | None = None, refresh_days: int = 30) -> dict:
    """Fetch + parse appraiser pages for the selected records.

    Existing entries younger than refresh_days are kept as-is, so weekly runs
    spread coverage instead of refetching the same parcels. Returns summary
    counts."""
    out_p = Path(out_path) if out_path else DEFAULT_OUT
    store = load_enrichment(out_p)
    now = datetime.now(timezone.utc)

    def is_fresh(entry: dict) -> bool:
        try:
            age = now - datetime.fromisoformat(entry["fetched_at"])
            return entry.get("ok") and age.days < refresh_days
        except (KeyError, ValueError):
            return False

    targets = [r for r in select_targets(records, counties, None)
               if not is_fresh(store.get(record_key(r), {}))]
    if limit:
        targets = targets[:limit]
    log.info("Enriching %d parcels from appraiser sites", len(targets))

    dbg = Path(debug_dir) if debug_dir else None
    if dbg:
        dbg.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = UA
    last_hit: dict[str, float] = {}
    n_ok = n_fail = 0
    for i, r in enumerate(targets):
        url = r["appraiser_url"]
        host = url.split("/")[2] if "//" in url else ""
        wait = HOST_DELAY - (time.monotonic() - last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        entry = {"url": url, "fetched_at": now.isoformat(timespec="seconds"),
                 "ok": False}
        try:
            last_hit[host] = time.monotonic()
            resp = session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            fields = parse_appraiser_page(resp.text)
            if dbg and i < 25:
                (dbg / f"{r['county']}_{re.sub(r'[^A-Za-z0-9]', '_', r['parcel_id'])[:40]}.html") \
                    .write_text(resp.text, encoding="utf-8")
            if fields:
                entry.update(fields)
                entry["ok"] = True
                n_ok += 1
            else:
                entry["error"] = "no recognizable fields on page"
                n_fail += 1
        except requests.RequestException as exc:
            entry["error"] = str(exc)[:200]
            n_fail += 1
        store[record_key(r)] = entry
        if (i + 1) % 25 == 0:
            log.info("  %d/%d done (%d ok)", i + 1, len(targets), n_ok)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(json.dumps(store, indent=0, sort_keys=True),
                             encoding="utf-8")

    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(store, indent=0, sort_keys=True), encoding="utf-8")
    summary = {"attempted": len(targets), "ok": n_ok, "failed": n_fail,
               "store_total": len(store)}
    log.info("Enrichment done: %s", summary)
    return summary

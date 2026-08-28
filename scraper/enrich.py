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

from . import paperwork, reportall
from .clerk import ClerkResolver

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
    """Scheduled + buy-box MATCH/REVIEW + has an appraiser link, soonest first.

    The feed no longer ships per-record flags (each dashboard team computes its
    own), so targeting recomputes them here from config/buybox.yaml — the
    operator's config decides where enrichment effort goes first."""
    from . import judgment
    cfg = judgment.load_buybox(None)

    def date_key(r):
        d = r.get("sale_date") or ""
        return d[6:] + d[:2] + d[3:5] if len(d) == 10 else "99999999"

    def flag(r):
        if r.get("buybox"):                      # older feeds still carry it
            return r["buybox"]
        return judgment.buybox_flag(judgment.record_from_feed(r), cfg)[0]

    out = [r for r in records
           if r.get("status") != "Redeemed"
           and r.get("appraiser_url")
           and (not counties or r.get("county") in counties)
           and flag(r) in ("MATCH", "REVIEW")]
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


def _fetch_appraiser(rec: dict, session, dbg: Path | None, idx: int) -> dict:
    """Appraiser page → owner / mailing / use / acreage / values."""
    url = rec.get("appraiser_url")
    if not url:
        return {}
    try:
        resp = session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return {"appraiser_error": str(exc)[:200]}
    if dbg and idx < 25:
        name = re.sub(r"[^A-Za-z0-9]", "_", rec.get("parcel_id", ""))[:40]
        (dbg / f"{rec['county']}_{name}.html").write_text(resp.text, encoding="utf-8")
    fields = parse_appraiser_page(resp.text)
    return fields or {"appraiser_error": "no recognizable fields on page"}


def enrich_records(records: list[dict], counties: list[str] | None = None,
                   limit: int | None = 200, out_path: str | Path | None = None,
                   debug_dir: str | Path | None = None, refresh_days: int = 30,
                   read_docs: bool = True, use_browser: bool = True) -> dict:
    """Run the quick-look scrub over the selected parcels.

    Per parcel, in order: the county appraiser record, the clerk's case file
    (scraper/clerk.py), the paperwork inside that file (scraper/paperwork.py),
    and — only when an API key is configured — the ReportAll parcel record.
    Every source is optional; whatever answers gets stored.

    Entries younger than refresh_days are left alone, so weekly runs widen
    coverage instead of refetching the same parcels."""
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

    dbg = Path(debug_dir) if debug_dir else None
    if dbg:
        dbg.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = UA

    clerk_resolver = ClerkResolver(session=requests.Session())
    browser_ctx = _BrowserPortals() if use_browser else None
    log.info("Enriching %d parcels (appraiser + clerk case files%s%s)",
             len(targets),
             ", paperwork" if read_docs else "",
             ", ReportAll" if reportall.enabled() else "")

    last_hit: dict[str, float] = {}
    n_ok = n_fail = n_cases = n_docs = n_parcels = 0
    try:
        for i, r in enumerate(targets):
            entry: dict = {"fetched_at": now.isoformat(timespec="seconds"), "ok": False}
            if r.get("appraiser_url"):
                entry["url"] = r["appraiser_url"]
                host = r["appraiser_url"].split("/")[2] if "//" in r["appraiser_url"] else ""
                wait = HOST_DELAY - (time.monotonic() - last_hit.get(host, 0))
                if wait > 0:
                    time.sleep(wait)
                last_hit[host] = time.monotonic()
                entry.update(_fetch_appraiser(r, session, dbg, i))

            # Clerk case file — this parcel's own record, not the county page.
            case: dict = {}
            try:
                case = clerk_resolver.resolve(r)
                if not case and browser_ctx is not None:
                    case = browser_ctx.resolve(r, clerk_resolver)
            except Exception as exc:                      # noqa: BLE001
                log.debug("clerk resolve failed for %s: %s", record_key(r), exc)
            if case:
                n_cases += 1
                entry.update(case)
                # Read the paperwork the case file links to.
                if read_docs and case.get("case_docs"):
                    try:
                        read = paperwork.read_case_docs(case["case_docs"], session)
                        entry.update(read)
                        n_docs += read.get("docs_read", 0)
                    except Exception as exc:              # noqa: BLE001
                        log.debug("paperwork read failed: %s", exc)

            # ReportAll parcel record (skipped entirely without an API key).
            if reportall.enabled():
                try:
                    parcel = reportall.lookup(
                        county=r.get("county", ""), parcel_id=r.get("parcel_id", ""),
                        address=r.get("property_address", ""),
                        lat=r.get("lat"), lng=r.get("lng"))
                    if parcel:
                        n_parcels += 1
                        entry["parcel"] = parcel
                except Exception as exc:                  # noqa: BLE001
                    log.debug("ReportAll lookup failed: %s", exc)

            # "ok" means at least one source answered with something usable.
            useful = [k for k in entry
                      if k not in ("fetched_at", "ok", "url", "appraiser_error")]
            if useful:
                entry["ok"] = True
                n_ok += 1
            else:
                entry["error"] = entry.pop("appraiser_error", "nothing resolved")
                n_fail += 1

            store[record_key(r)] = entry
            if (i + 1) % 25 == 0:
                log.info("  %d/%d done (%d ok, %d case files, %d docs read)",
                         i + 1, len(targets), n_ok, n_cases, n_docs)
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_text(json.dumps(store, indent=0, sort_keys=True),
                                 encoding="utf-8")
    finally:
        if browser_ctx is not None:
            browser_ctx.close()

    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(store, indent=0, sort_keys=True), encoding="utf-8")
    summary = {"attempted": len(targets), "ok": n_ok, "failed": n_fail,
               "case_files": n_cases, "docs_read": n_docs,
               "reportall_parcels": n_parcels, "store_total": len(store)}
    log.info("Enrichment done: %s", summary)
    return summary


def backfill_geometry(records: list[dict], counties: list[str] | None = None,
                      limit: int | None = None, out_path: str | Path | None = None,
                      include_redeemed: bool = False, include_past: bool = False) -> dict:
    """Resolve the parcel boundary (and centroid) for feed records by APN.

    A lookup by parcel number, standing on its own — it doesn't touch the
    appraiser/clerk pipeline and never marks a record "enriched". For every
    record whose stored ReportAll parcel is missing (or lacks `geometry_wkt`),
    call ReportAll by county+APN and merge the parcel record into the store, so
    the per-card map can outline the exact lot. Cheap relative to a full
    enrichment (one API call per parcel), and safe to repeat: a record that
    already has geometry is skipped, so re-runs only fill what's still missing.

    Only auctions still to come and not redeemed are looked up — a past sale's
    parcel isn't worth an API call. Soonest sales first; pass include_redeemed
    (and include_past, mainly for tests) to widen the net. A no-op without a
    key."""
    out_p = Path(out_path) if out_path else DEFAULT_OUT
    store = load_enrichment(out_p)
    if not reportall.enabled():
        log.info("ReportAll not configured — geometry backfill skipped")
        return {"attempted": 0, "filled": 0, "store_total": len(store)}
    now = datetime.now(timezone.utc)
    county_set = {c.lower() for c in counties} if counties else None
    today_key = now.strftime("%Y%m%d")

    def wants(r: dict) -> bool:
        if not r.get("parcel_id"):
            return False
        if county_set and str(r.get("county", "")).lower() not in county_set:
            return False
        if not include_redeemed and str(r.get("status", "")).lower() == "redeemed":
            return False
        # Future sales only: don't spend a lookup on an auction that already
        # happened (or on a record with no usable sale date).
        if not include_past:
            key = _sale_sort_key(r.get("sale_date", ""))
            if key == "99999999" or key < today_key:
                return False
        parcel = (store.get(_feed_key(r), {}) or {}).get("parcel") or {}
        return not parcel.get("geometry_wkt")

    # Soonest upcoming sales first, so a capped run covers what matters most.
    targets = [r for r in records if wants(r)]
    targets.sort(key=lambda r: _sale_sort_key(r.get("sale_date", "")))
    if limit:
        targets = targets[:limit]
    log.info("Resolving parcel geometry for %d feed records", len(targets))

    n_filled = n_try = 0
    for i, r in enumerate(targets):
        n_try += 1
        try:
            parcel = reportall.lookup(county=r.get("county", ""),
                                      parcel_id=r.get("parcel_id", ""),
                                      address=r.get("property_address", ""),
                                      lat=r.get("lat"), lng=r.get("lng"))
        except Exception as exc:                          # noqa: BLE001
            log.debug("ReportAll geometry lookup failed: %s", exc)
            continue
        if parcel and parcel.get("geometry_wkt"):
            key = _feed_key(r)
            entry = store.get(key, {})
            entry.setdefault("fetched_at", now.isoformat(timespec="seconds"))
            entry["parcel"] = {**(entry.get("parcel") or {}), **parcel}
            entry["geom_backfilled_at"] = now.isoformat(timespec="seconds")
            store[key] = entry
            n_filled += 1
        time.sleep(0.3)   # be polite to the API
        if (i + 1) % 50 == 0:
            log.info("  %d/%d done (%d filled)", i + 1, len(targets), n_filled)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(json.dumps(store, indent=0, sort_keys=True),
                             encoding="utf-8")
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(store, indent=0, sort_keys=True), encoding="utf-8")
    summary = {"attempted": n_try, "filled": n_filled, "store_total": len(store)}
    log.info("Geometry backfill done: %s", summary)
    return summary


def _feed_key(r: dict) -> str:
    """Store key for a FEED record (fields are flat, not the scraper's attrs)."""
    return f"{r.get('county', '')}|{r.get('parcel_id', '')}|{r.get('case_number', '')}"


def _sale_sort_key(d: str) -> str:
    # MM/DD/YYYY -> YYYYMMDD so soonest sorts first; blanks sort last.
    return (d[6:] + d[:2] + d[3:5]) if len(d) == 10 else "99999999"


class _BrowserPortals:
    """Lazily-started browser for portals that can't be resolved over HTTP.

    Playwright only starts if a record actually needs it (today: Marion's
    NewVision portal), and any failure downgrades to no browser at all."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._resolver = None
        self._broken = False

    def _ensure(self):
        if self._resolver is not None or self._broken:
            return self._resolver
        try:
            from playwright.sync_api import sync_playwright

            from .clerk_browser import NewVisionResolver
            self._pw = sync_playwright().start()
            # Marion's portal errors on its search navigation over HTTP/2
            # (net::ERR_HTTP2_PROTOCOL_ERROR, reproducible every time) —
            # force HTTP/1.1 so that request goes through.
            self._browser = self._pw.chromium.launch(args=["--disable-http2"])
            page = self._browser.new_page()
            self._resolver = NewVisionResolver(page)
        except Exception as exc:                          # noqa: BLE001
            log.info("browser portals unavailable (%s)", str(exc)[:120])
            self._broken = True
        return self._resolver

    def resolve(self, rec: dict, clerk_resolver) -> dict:
        cfg = clerk_resolver._county_cfg(rec.get("county", ""))
        if cfg.get("platform") != "newvision":
            return {}
        resolver = self._ensure()
        if resolver is None:
            return {}
        try:
            return resolver.resolve(rec, cfg)
        except Exception as exc:                          # noqa: BLE001
            log.debug("newvision resolve failed: %s", exc)
            return {}

    def close(self):
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:                             # noqa: BLE001
                pass

"""Collectors for the Florida counties RealAuction doesn't cover.

The main pipeline (scraper/scrape.py) speaks one platform: RealAuction. Seven
counties sell tax deeds elsewhere and were registered in
config/florida_counties.json with a real sale_list_url but no way to pull the
listings into the feed. This module holds a per-platform collector for each and
a dispatcher that runs them into a run directory the exporter already
understands (one <slug>.json list of records per county), so the carry-forward
export merges these counties in beside the RealAuction 45.

Public pages only; robots-checked; rate limited. Each collector returns
AuctionRecords; a collector that fails is logged and skipped — one broken
county never blocks the others or the feed.

    python -m scraper scrape-clerk --out output/runs/<ts> [--counties okaloosa,...]

Platforms, by evidence capture (scraper/capture_sale_lists.py):
  Okaloosa   Bid4Assets — the listings page server-binds the whole sale into
             an inline Kendo grid (dataSource.data.Data = the auctions), so a
             plain GET carries every parcel; no login, no JS.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .models import AuctionRecord
from .robots import check_robots
from .scrape import write_county_outputs
from .sources import RateLimiter

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "config" / "florida_counties.json"

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Slug -> collector function name; the dispatcher routes by this map so a county
# not listed here is simply reported as "no collector yet" rather than guessed.
BID4ASSETS_SLUGS = {"okaloosa"}


def _load_registry() -> dict[str, dict]:
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("counties", list(data.values()))
    return {r["slug"]: r for r in rows if isinstance(r, dict) and r.get("slug")}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract_json_array(text: str, key: str) -> str | None:
    """Return the substring of `text` that is the JSON array following `"key":`,
    matching brackets while respecting strings/escapes. The Bid4Assets grid
    inlines its rows as `"Data":[ ... ]` inside a much larger script, so a plain
    regex can't carve it out — brackets nest and strings contain `]`."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[', text)
    if not m:
        return None
    i = m.end() - 1  # position of the opening '['
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return None


def _num(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _saledate_to_mdy(s: str) -> str:
    """'20260908' -> '09/08/2026' (the MM/DD/YYYY the rest of the app uses)."""
    s = (s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[4:6]}/{s[6:8]}/{s[:4]}"
    # Also accept an ISO datetime like '2026-09-08T00:00:00'.
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
    return ""


_MARKER_RE = re.compile(r"\*{2,}\s*([^*]+?)\s*\*{2,}")


def _clean_title(title: str) -> tuple[str, str]:
    """Bid4Assets prefixes withdrawn/cancelled lots in the asset title, e.g.
    '***Withdrawn***6287 BETHANY DR ...'. Return (clean_address, marker)."""
    title = title or ""
    marker = ""
    m = _MARKER_RE.search(title)
    if m:
        marker = m.group(1).strip()
    clean = _MARKER_RE.sub("", title).strip()
    return clean, marker


def _b4a_status(row: dict, marker: str) -> str:
    """A human status for the parcel. Bid4Assets reuses foreclosure labels
    ('Sold to Plaintiff') for tax sales, so trust the explicit flags first: a
    withdrawn / stayed / postponed lot is off the sale (for tax deeds, usually a
    redemption or cancellation); otherwise report the auction's own status."""
    if marker:
        return marker  # e.g. "Withdrawn", "Cancelled"
    if row.get("IsStayed"):
        return "Stayed"
    if row.get("IsPostponed") or row.get("IsPostponedOrStayed"):
        return "Postponed"
    return (row.get("AuctionStatusString") or "").strip()


def collect_bid4assets(entry: dict, session: requests.Session, rate: RateLimiter) -> list[AuctionRecord]:
    """Okaloosa on Bid4Assets. The county tax-sale landing page server-binds the
    whole sale into an inline Kendo grid (dataSource.data.Data), so each sale
    date's parcels arrive in one GET. Iterate every sale date in the dropdown."""
    slug = entry["slug"]
    url = entry["sale_list_url"]
    host = re.sub(r"^https?://", "", url).split("/")[0]

    def _fetch(u: str) -> str:
        rate.wait()
        r = session.get(u, timeout=30)
        r.raise_for_status()
        return r.text

    html = _fetch(url)
    soup = BeautifulSoup(html, "lxml")

    # Every sale date the county has scheduled; each reloads the grid via
    # ?salesdate=<id>. Fall back to whatever the default page already shows.
    sel = soup.find("select", id="SelectedSaleDateId") or soup.find("select", attrs={"name": "SelectedSaleDateId"})
    sale_ids: list[str] = []
    if sel:
        sale_ids = [(o.get("value") or "").strip() for o in sel.find_all("option") if (o.get("value") or "").strip()]
    if not sale_ids:
        sd = soup.find(attrs={"name": "SelectedSaleDateId"}) or soup.find(id="SelectedSaleDateId")
        if sd and sd.get("value"):
            sale_ids = [sd["value"].strip()]

    records: list[AuctionRecord] = []
    seen: set[tuple] = set()
    pages = {sale_ids[0] if sale_ids else "": html}
    for sid in sale_ids[1:]:
        try:
            pages[sid] = _fetch(f"{url.rstrip('/')}?salesdate={sid}")
        except requests.RequestException as exc:
            log.warning("[%s] sale date %s fetch failed: %s", slug, sid, exc)

    for sid, page in pages.items():
        arr_text = _extract_json_array(page, "Data")
        if not arr_text:
            continue
        try:
            rows = json.loads(arr_text)
        except json.JSONDecodeError as exc:
            log.warning("[%s] sale %s: grid data did not parse: %s", slug, sid, exc)
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            apn = (row.get("Apn") or "").strip()
            addr_raw = row.get("Address") or row.get("Asset_Title") or ""
            clean_addr, marker = _clean_title(addr_raw)
            sale_date = _saledate_to_mdy(row.get("SaleDateString") or row.get("SaleDate") or sid)
            key = (apn, sale_date)
            if not apn or key in seen:
                continue
            seen.add(key)
            aid = row.get("AuctionID")
            rec = AuctionRecord(
                county=slug,
                sale_date=sale_date,
                sale_time=(row.get("CloseTimeAsString") or "").strip() + (" CT" if row.get("CloseTimeAsString") else ""),
                auction_type="TAXDEED",
                parcel_id=apn,
                property_address=clean_addr,
                opening_bid=_num(row.get("MinimumBid")),
                auction_status=_b4a_status(row, marker),
                auction_url=f"https://{host}/auction/{aid}" if aid else url,
                source_host=host,
                scraped_at=_now_iso(),
                raw_fields={k: row.get(k) for k in
                            ("AuctionID", "Asset_Title", "Address", "Apn", "MinimumBid",
                             "CurrentBidString", "AuctionStatusString", "SaleDateString",
                             "IsStayed", "IsPostponed", "IsPostponedOrStayed") if k in row},
            )
            records.append(rec)
    records.sort(key=lambda r: (r.sale_date[6:], r.sale_date[:2], r.sale_date[3:5], r.parcel_id))
    return records


# Platform dispatch: slug -> (collector, needs_session).
def _collector_for(slug: str):
    if slug in BID4ASSETS_SLUGS:
        return collect_bid4assets
    return None


def scrape_clerk_counties(out_dir: str | Path, counties: list[str] | None = None,
                          delay: float = 3.0, skip_robots: bool = False) -> dict:
    """Run the non-RealAuction collectors into `out_dir`, writing one
    <slug>.json/.csv per county so the exporter's carry-forward merges them in.
    Records the outcome per county in clerk_meta.json for the run log."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    reg = _load_registry()

    # Default target set: every registry county we have a collector for.
    if counties:
        targets = counties
    else:
        targets = [s for s in reg if _collector_for(s)]

    rate = RateLimiter(base_delay=delay)
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    meta: dict = {"started_at": _now_iso(), "counties": {}}
    for slug in targets:
        entry = reg.get(slug)
        centry: dict = {"status": "ok", "records": 0}
        if not entry:
            centry["status"] = "error"
            centry["error"] = "not in registry"
            meta["counties"][slug] = centry
            log.warning("[%s] not in florida_counties.json", slug)
            continue
        collector = _collector_for(slug)
        if not collector:
            centry["status"] = "skipped"
            centry["error"] = "no collector for this county's platform yet"
            meta["counties"][slug] = centry
            log.info("[%s] no collector yet — skipped", slug)
            continue
        url = entry.get("sale_list_url")
        centry["url"] = url
        if not url:
            centry["status"] = "error"
            centry["error"] = "no sale_list_url in registry"
            meta["counties"][slug] = centry
            continue
        try:
            if not skip_robots:
                robots = check_robots(url)
                centry["robots"] = robots["detail"]
                if not robots["allowed"]:
                    centry["status"] = "skipped_robots"
                    meta["counties"][slug] = centry
                    log.warning("[%s] skipped (robots): %s", slug, robots["detail"])
                    continue
            recs = collector(entry, session, rate)
            write_county_outputs(out, slug, {"records": recs})
            centry["records"] = len(recs)
            centry["sale_dates"] = sorted({r.sale_date for r in recs})
            log.info("[%s] %d records via %s", slug, len(recs), collector.__name__)
        except Exception as exc:  # noqa: BLE001 — one county never blocks the rest
            centry["status"] = "error"
            centry["error"] = f"{exc.__class__.__name__}: {exc}"
            centry["traceback"] = traceback.format_exc(limit=4)
            log.error("[%s] FAILED: %s", slug, exc)
        centry["finished_at"] = _now_iso()
        meta["counties"][slug] = centry

    meta["finished_at"] = _now_iso()
    (out / "clerk_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta

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
  Okaloosa            Bid4Assets — the listings page server-binds the whole sale
                      into an inline Kendo grid (dataSource.data.Data), so a
                      plain GET carries every parcel; no login, no JS.
  St. Johns, Levy     TaxSmart — pick a sale-date range, POST it (stashed in the
                      session), then a jqGrid fetches the rows from
                      Home/GridSearchData as JSON.
  Hardee, Sumter,     JS-rendered clerk sites — the sale list is drawn client
  Columbia            side as repeated <label>/<strong> field pairs (no table,
                      no clean JSON endpoint; Columbia's WAF 403s plain requests
                      but a real browser gets through). Render in Chromium and
                      parse the labelled fields. One generic collector serves
                      all three.
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
# St. Johns + Levy run the same "TaxSmart" clerk software; one collector serves
# both. The upcoming-sale search stashes its date range in the session, then a
# jqGrid fetches the rows from Home/GridSearchData as JSON.
TAXSMART_SLUGS = {"stjohns", "levy"}
# Hardee, Sumter, Columbia render their sale list client-side as repeated
# <label>/<strong> field pairs — one generic Chromium-render collector reads all
# three (Columbia's WAF 403s plain requests but a real browser gets through).
RENDERED_SLUGS = {"hardee", "sumter", "columbia"}


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


# --- TaxSmart (St. Johns + Levy) --------------------------------------------
# The upcoming-sale list is behind a search: pick a sale-date range, POST it
# (the range is stashed in the session), then a jqGrid fetches the rows as JSON
# from Home/GridSearchData. Evidence (capture-sale-lists rounds) pinned the flow
# and the fixed cell order the server returns for a Sale-Date search:
#   cell = [Applicant, Case#, Certificate#, ParcelId, SaleDate,
#           Status, Amount, LandsAvailable, Surplus, Owner]
# (The jqGrid display colModel lists columns in a different order — the JSON
# cell order is the server's, confirmed from a real row, and is shared by both
# counties since they run the same TaxSmart software.)
TAXSMART_CELL = {
    "applicant": 0, "case_number": 1, "certificate_number": 2, "parcel_id": 3,
    "sale_date": 4, "status": 5, "amount": 6, "lands_available": 7,
    "surplus": 8, "owner": 9,
}


def _money(s) -> float | None:
    if not s:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s))
    return float(m.group(0).replace(",", "")) if m else None


def _mdy(s: str) -> str:
    """'9/16/2026' -> '09/16/2026'. Leaves already-padded or odd values alone."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", (s or "").strip())
    return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}" if m else (s or "").strip()


def collect_taxsmart(entry: dict, session: requests.Session, rate: RateLimiter) -> list[AuctionRecord]:
    """St. Johns / Levy on the TaxSmart clerk platform."""
    from datetime import datetime
    from urllib.parse import urljoin
    slug = entry["slug"]
    url = entry["sale_list_url"]
    host = re.sub(r"^https?://", "", url).split("/")[0]

    rate.wait()
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    form = None
    for f in soup.find_all("form"):
        if f.find(attrs={"name": "SearchSaleDateFrom"}) or f.find(id="SearchSaleDateFrom"):
            form = f
            break
    if not form:
        raise ValueError("TaxSmart sale-date form not found")
    action = urljoin(r.url, form.get("action") or r.url)

    # Every field the form submits, with defaults.
    fields: dict[str, str] = {}
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue
        if el.name == "select":
            opts = el.find_all("option")
            sel = next((o for o in opts if o.get("selected")), opts[0] if opts else None)
            fields[name] = sel.get("value", "") if sel else ""
        elif el.get("type") in ("checkbox", "radio"):
            if el.get("checked"):
                fields[name] = el.get("value", "on")
        else:
            fields[name] = el.get("value", "")

    # Build a valid ascending range covering every upcoming sale date. The
    # <option>s are human strings ("Wednesday, December 15, 2027 12:00 PM"),
    # listed newest-first.
    sel = soup.find("select", attrs={"name": "SearchSaleDateFrom"}) or soup.find("select", id="SearchSaleDateFrom")
    opt_vals = [o.get("value", "") for o in sel.find_all("option") if o.get("value")] if sel else []

    def _parse_dt(s):
        for fmt in ("%A, %B %d, %Y %I:%M %p", "%A, %B %d, %Y %I:%M%p", "%A, %B %d, %Y"):
            try:
                return datetime.strptime(s.strip(), fmt)
            except ValueError:
                continue
        return None
    now = datetime.utcnow()
    upcoming = sorted([(o, d) for o, d in ((o, _parse_dt(o)) for o in opt_vals) if d and d >= now],
                      key=lambda x: x[1])
    if upcoming:
        fields["SearchSaleDateFrom"] = upcoming[0][0]
        fields["SearchSaleDateTo"] = upcoming[-1][0]
    elif opt_vals:
        fields["SearchSaleDateFrom"], fields["SearchSaleDateTo"] = opt_vals[-1], opt_vals[0]
    else:
        return []  # nothing scheduled

    # This one multi-tab form dispatches on which submit button is present.
    for k in [k for k in fields if k.lower().startswith("buttonsubmit")]:
        del fields[k]
    fields["buttonSubmitSaleDate"] = "Search"

    rate.wait()
    session.post(action, data=fields, timeout=45)  # stashes the range in session

    # The grid reads its rows from Home/GridSearchData; ask for a large page so
    # a single request returns every matching sale.
    grid_url = urljoin(action if action.endswith("/") else action + "/", "Home/GridSearchData")
    rate.wait()
    gr = session.get(grid_url, params={"SearchType": "Sale Date", "rows": 10000, "page": 1},
                     headers={"X-Requested-With": "XMLHttpRequest"}, timeout=45)
    gr.raise_for_status()
    payload = gr.json()
    rows = payload.get("rows", []) if isinstance(payload, dict) else []

    records: list[AuctionRecord] = []
    seen: set[tuple] = set()
    C = TAXSMART_CELL
    for row in rows:
        cell = row.get("cell") if isinstance(row, dict) else None
        if not cell or len(cell) <= C["owner"]:
            continue
        parcel = (cell[C["parcel_id"]] or "").strip()
        sale_date = _mdy(cell[C["sale_date"]])
        # Only upcoming sales (past ones may come back from the same grid).
        try:
            if datetime.strptime(sale_date, "%m/%d/%Y") < now.replace(hour=0, minute=0, second=0, microsecond=0):
                continue
        except ValueError:
            pass
        key = (parcel, sale_date)
        if not parcel or key in seen:
            continue
        seen.add(key)
        case_no = (cell[C["case_number"]] or "").strip()
        records.append(AuctionRecord(
            county=slug,
            sale_date=sale_date,
            auction_type="TAXDEED",
            case_number=case_no,
            certificate_number=(cell[C["certificate_number"]] or "").strip(),
            parcel_id=parcel,
            owner_name=(cell[C["owner"]] or "").strip(),
            opening_bid=_money(cell[C["amount"]]),
            auction_status=(cell[C["status"]] or "").strip(),
            auction_url=url,
            source_host=host,
            scraped_at=_now_iso(),
            raw_fields={"applicant": (cell[C["applicant"]] or "").strip(),
                        "lands_available": cell[C["lands_available"]],
                        "surplus": cell[C["surplus"]],
                        "cell": cell, "taxsmart_id": row.get("id")},
        ))
    records.sort(key=lambda r: (r.sale_date[6:], r.sale_date[:2], r.sale_date[3:5], r.parcel_id))
    return records


# --- Rendered clerk lists (Hardee + Sumter + Columbia) ----------------------
# All three draw their sale list client-side as repeated rows of
#   <div class="w-full md:w-auto"><label>KEY</label><strong>VALUE</strong></div>
# so one render-and-read-labels collector serves them. Label wording varies a
# little between counties (Parcel ID vs Parcel Number, Cert vs Cert #, File #
# vs File Number); these normalized-key sets absorb that.
_LABEL_MAP = {
    "parcel_id": {"parcel id", "parcel number", "parcel"},
    "sale_date": {"sale date", "sale date/time", "date"},
    "opening_bid": {"opening bid", "minimum bid", "bid"},
    "auction_status": {"status"},
    "certificate_number": {"cert", "cert #", "certificate", "certificate #", "certificate number"},
    "case_number": {"file", "file #", "file number", "case", "case #", "case number"},
    "owner_name": {"owner", "owner name"},
}
# Extra labels worth keeping verbatim in raw_fields.
_RAW_LABELS = {"cert holder", "applicant", "notes", "amount due", "location"}


def _norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("#", "").strip().lower()).strip()


def _render_html(url: str, rate: RateLimiter) -> str:
    """Load a JS clerk page in Chromium (a real browser gets past the WAF that
    403s plain requests) and return the rendered HTML."""
    from playwright.sync_api import sync_playwright
    rate.wait()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-http2"])
        try:
            page = browser.new_page(user_agent=BROWSER_HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(1500)
            return page.content()
        finally:
            browser.close()


def collect_rendered_labels(entry: dict, session: requests.Session, rate: RateLimiter) -> list[AuctionRecord]:
    """Hardee / Sumter / Columbia. Render the page and read each sale row's
    <label>/<strong> field pairs."""
    from datetime import datetime
    from urllib.parse import urljoin
    slug = entry["slug"]
    url = entry["sale_list_url"]
    host = re.sub(r"^https?://", "", url).split("/")[0]

    html = _render_html(url, rate)
    soup = BeautifulSoup(html, "lxml")

    # Each field is a div carrying both "w-full" and "w-auto" classes with a
    # <label>/<strong> inside; group consecutive fields by their shared parent
    # (the row's flex wrapper), preserving document order.
    def _cls(el):
        return " ".join(el.get("class") or [])
    field_divs = [d for d in soup.find_all("div")
                  if "w-full" in _cls(d) and "w-auto" in _cls(d) and d.find("label") and d.find("strong")]
    rows: list[tuple] = []            # (wrapper_id, {label: value}, wrapper_el)
    by_wrapper: dict[int, dict] = {}
    order: list[int] = []
    wrapper_el: dict[int, object] = {}
    for fd in field_divs:
        label = _norm_label(fd.find("label").get_text(" ", strip=True))
        value = fd.find("strong").get_text(" ", strip=True)
        wid = id(fd.parent)
        if wid not in by_wrapper:
            by_wrapper[wid] = {}
            order.append(wid)
            wrapper_el[wid] = fd.parent
        by_wrapper[wid].setdefault(label, value)

    records: list[AuctionRecord] = []
    seen: set[tuple] = set()
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for wid in order:
        fields = by_wrapper[wid]
        if "sale date" not in fields:      # not a sale row
            continue
        picked: dict[str, str] = {}
        for target, names in _LABEL_MAP.items():
            for lab, val in fields.items():
                if lab in names:
                    picked[target] = val
                    break
        parcel = (picked.get("parcel_id") or "").strip()
        sale_date = _mdy(picked.get("sale_date", ""))
        if not parcel or not sale_date:
            continue
        # Upcoming only.
        try:
            if datetime.strptime(sale_date, "%m/%d/%Y") < today:
                continue
        except ValueError:
            pass
        key = (parcel, sale_date)
        if key in seen:
            continue
        seen.add(key)
        # An appraiser/record link sometimes sits in the row container.
        appraiser = ""
        container = wrapper_el[wid].parent if wrapper_el[wid] else None
        if container:
            a = container.find("a", href=True)
            if a:
                appraiser = urljoin(url, a["href"])
        raw = {"cell_labels": fields}
        for lab, val in fields.items():
            if lab in _RAW_LABELS:
                raw[lab.replace(" ", "_")] = val
        records.append(AuctionRecord(
            county=slug,
            sale_date=sale_date,
            auction_type="TAXDEED",
            case_number=(picked.get("case_number") or "").strip(),
            certificate_number=(picked.get("certificate_number") or "").strip(),
            parcel_id=parcel,
            owner_name=(picked.get("owner_name") or "").strip(),
            opening_bid=_money(picked.get("opening_bid")),
            auction_status=(picked.get("auction_status") or "").strip(),
            auction_url=url,
            appraiser_url=appraiser,
            source_host=host,
            scraped_at=_now_iso(),
            raw_fields=raw,
        ))
    records.sort(key=lambda r: (r.sale_date[6:], r.sale_date[:2], r.sale_date[3:5], r.parcel_id))
    return records


# Platform dispatch: slug -> collector.
def _collector_for(slug: str):
    if slug in BID4ASSETS_SLUGS:
        return collect_bid4assets
    if slug in TAXSMART_SLUGS:
        return collect_taxsmart
    if slug in RENDERED_SLUGS:
        return collect_rendered_labels
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

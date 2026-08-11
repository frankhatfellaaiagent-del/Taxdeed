"""Per-county scrape orchestration: calendar -> auction pages -> records.

Design rules:
- A county that errors is logged and skipped; the run continues.
- Every record passes a taxdeed sanity check; foreclosure-smelling records are
  excluded and written to excluded_foreclosure.json for audit.
- Output is deterministic: sorted records, stable columns, one CSV + one JSON
  per county, plus run_meta.json describing the run.
"""

from __future__ import annotations

import csv
import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from .models import CSV_COLUMNS, AuctionRecord
from .parsing import looks_like_foreclosure, parse_auction_items, parse_calendar_dates
from .robots import check_robots

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scrape_county(source, county: dict, months: int = 2) -> dict:
    """Scrape one county. Returns {records, excluded, dates, warnings}."""
    slug, base_url = county["slug"], county["url"]
    if "realforeclose.com" in base_url:
        raise ValueError(f"Refusing to scrape foreclosure host: {base_url}")

    warnings: list[str] = []
    records: list[AuctionRecord] = []
    excluded: list[dict] = []

    cal_pages = source.calendar_pages(slug, base_url, months=months)
    dates: list[str] = []
    for page in cal_pages:
        for d in parse_calendar_dates(page):
            if d["date"] not in dates:
                dates.append(d["date"])
    if not cal_pages:
        warnings.append("calendar: no pages loaded")
    elif not dates:
        warnings.append("calendar: no tax deed sale dates found (may be none scheduled, or page structure changed)")

    today = datetime.now().strftime("%Y%m%d")
    for date in dates:
        yyyymmdd = date[6:] + date[:2] + date[3:5]
        if yyyymmdd < today:
            continue  # only upcoming sales
        pages = source.auction_pages(slug, base_url, date)
        if not pages or all(not p.strip() for p in pages):
            warnings.append(f"auction {date}: no page content")
            continue
        page_url = urljoin(base_url, f"/index.cgi?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE={date}")
        found_any = False
        for html in pages:
            items = parse_auction_items(html, page_url, county=slug, sale_date=date)
            for rec in items:
                found_any = True
                rec.scraped_at = _now_iso()
                if looks_like_foreclosure(rec):
                    excluded.append({"reason": "failed taxdeed sanity check (looks like foreclosure)", **rec.to_dict()})
                else:
                    records.append(rec)
        if not found_any:
            warnings.append(f"auction {date}: page loaded but no auction items parsed (structure change?)")

    # Deterministic order + in-run dedupe (pagination overlap protection).
    uniq: dict[tuple, AuctionRecord] = {}
    for rec in records:
        uniq.setdefault(rec.key(), rec)
    records = sorted(uniq.values(), key=lambda r: (r.sale_date[6:], r.sale_date[:2], r.sale_date[3:5], r.parcel_id, r.case_number))
    return {"records": records, "excluded": excluded, "dates": dates, "warnings": warnings}


def write_county_outputs(out_dir: Path, slug: str, result: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [r.to_dict() for r in result["records"]]
    (out_dir / f"{slug}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    with (out_dir / f"{slug}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _write_status(out_dir: Path, meta: dict, current: str | None, total: int):
    """Incremental heartbeat for the dashboard: rewritten after every county."""
    status = {
        "run_dir": str(out_dir),
        "started_at": meta["started_at"],
        "finished_at": meta.get("finished_at"),
        "live": meta["live"],
        "total_counties": total,
        "current_county": current,
        "counties": meta["counties"],
    }
    try:
        (out_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        pointer = out_dir.parent.parent / "current_run.json"  # output/current_run.json
        pointer.write_text(json.dumps(status), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write status heartbeat: %s", e)


def run_scrape(source, counties: list[dict], out_dir: str | Path, months: int = 2,
               skip_robots: bool = False) -> dict:
    """Scrape a list of counties sequentially. Never raises for a single county."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "started_at": _now_iso(),
        "months": months,
        "live": getattr(source, "is_live", False),
        "counties": {},
    }
    all_excluded: list[dict] = []
    _write_status(out_dir, meta, current=counties[0]["slug"] if counties else None, total=len(counties))

    for county in counties:
        slug = county["slug"]
        centry: dict = {"url": county["url"], "status": "ok", "auctions": 0, "warnings": []}
        try:
            if getattr(source, "is_live", False) and not skip_robots:
                robots = check_robots(county["url"])
                centry["robots"] = robots["detail"]
                if not robots["allowed"]:
                    centry["status"] = "skipped_robots"
                    log.warning("[%s] skipped: %s", slug, robots["detail"])
                    meta["counties"][slug] = centry
                    continue
                if robots.get("crawl_delay") and hasattr(source, "rate"):
                    source.rate.base_delay = max(source.rate.base_delay, float(robots["crawl_delay"]))

            log.info("[%s] scraping %s", slug, county["url"])
            result = scrape_county(source, county, months=months)
            write_county_outputs(out_dir, slug, result)
            centry["auctions"] = len(result["records"])
            centry["sale_dates"] = result["dates"]
            centry["warnings"] = result["warnings"]
            centry["excluded_foreclosure"] = len(result["excluded"])
            all_excluded.extend(result["excluded"])
            log.info("[%s] %d records, %d excluded, %d warnings", slug,
                     len(result["records"]), len(result["excluded"]), len(result["warnings"]))
        except Exception as e:
            centry["status"] = "error"
            centry["error"] = f"{e.__class__.__name__}: {e}"
            centry["traceback"] = traceback.format_exc(limit=5)
            log.error("[%s] FAILED: %s (county skipped, run continues)", slug, e)
        centry["finished_at"] = _now_iso()
        meta["counties"][slug] = centry
        done = len(meta["counties"])
        nxt = counties[done]["slug"] if done < len(counties) else None
        _write_status(out_dir, meta, current=nxt, total=len(counties))

    meta["finished_at"] = _now_iso()
    _write_status(out_dir, meta, current=None, total=len(counties))
    if all_excluded:
        (out_dir / "excluded_foreclosure.json").write_text(json.dumps(all_excluded, indent=2), encoding="utf-8")
    (out_dir / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta

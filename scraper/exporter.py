"""Stable data feeds for downstream consumers (Google Sheet, dashboard UI).

Writes, from a run directory:
  data/exports/master_list.tsv   — imported live by the client's Google Sheet
  data/exports/master_list.json  — fetched by the dashboard frontend

Both live at fixed paths so their raw.githubusercontent.com URLs never change;
every scraper run that commits data refreshes the same URLs. The feed contract
is documented in docs/DATA_FEED.md — keep the two in sync.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import diffing, geocode, judgment
from .enrich import load_enrichment

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
EXPORTS_DIR = ROOT / "data" / "exports"

TSV_COLUMNS = ["County", "Sale Date", "Sale Time", "Parcel ID", "Case #",
               "Certificate #", "Owner", "Mailing Address", "Property Address",
               "Property Use", "Acres", "Opening Bid", "Assessed Value",
               "Bid/Value %", "Buy-Box", "Buy-Box Notes", "Status", "Auction Page",
               "Appraiser Record", "Latitude", "Longitude"]


def _clean(v) -> str:
    return str(v if v is not None else "").replace("\t", " ").replace("\n", " ").strip()


def _load_clerk_sites() -> dict:
    """config/clerk_sites.yaml → {county_slug: {url, search?}} for the feed."""
    p = ROOT / "config" / "clerk_sites.yaml"
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.warning("clerk_sites.yaml unreadable (%s); feed ships without it", exc)
        return {}
    return {slug: {k: v for k, v in (entry or {}).items() if k in ("url", "search")}
            for slug, entry in raw.items() if (entry or {}).get("url")}


def export_run(run_dir: str | Path, out_dir: str | Path | None = None) -> dict:
    run_dir = Path(run_dir)
    out = Path(out_dir) if out_dir else EXPORTS_DIR
    out.mkdir(parents=True, exist_ok=True)

    records = diffing.load_run_records(run_dir)
    records, _ = judgment.dedupe(records)
    cfg = judgment.load_buybox(None)
    records.sort(key=lambda r: (r.sale_date[6:] + r.sale_date[:2] + r.sale_date[3:5],
                                r.county, r.parcel_id))

    # Parcel coordinates (cached; only new addresses hit the Census API).
    coords = geocode.geocode_addresses([r.property_address for r in records])

    # Appraiser quick-look results (python -m scraper enrich), keyed like the
    # dashboard ids. Merged into each record BEFORE buy-box flagging so an
    # enriched land use can upgrade REVIEW rows to MATCH/NO.
    enrichment = load_enrichment()

    tsv_lines = ["\t".join(TSV_COLUMNS)]
    json_records = []
    by_county: dict[str, dict] = {}
    n_redeemed = 0
    for r in records:
        enr = enrichment.get(f"{r.county}|{r.parcel_id}|{r.case_number}", {})
        mailing = ""
        if enr.get("ok"):
            r.owner_name = r.owner_name or enr.get("owner_name", "")
            r.property_use = r.property_use or enr.get("property_use", "")
            r.acreage = r.acreage or enr.get("acreage", "")
            mailing = enr.get("mailing_address", "")
        flag, reasons = judgment.buybox_flag(r, cfg)
        redeemed = "redeem" in (r.auction_status or "").lower()
        n_redeemed += redeemed
        ratio = round(100 * r.opening_bid / r.assessed_value) \
            if r.opening_bid and r.assessed_value else None
        status = "Redeemed" if redeemed else "Scheduled"
        latlng = coords.get(r.property_address) or [None, None]
        tsv_lines.append("\t".join(_clean(x) for x in [
            r.county, r.sale_date, r.sale_time, r.parcel_id, r.case_number,
            r.certificate_number, r.owner_name, mailing, r.property_address,
            r.property_use, r.acreage, r.opening_bid or "",
            r.assessed_value or "", ratio if ratio is not None else "", flag, reasons,
            status, r.auction_url, r.appraiser_url,
            latlng[0] if latlng[0] is not None else "",
            latlng[1] if latlng[1] is not None else ""]))
        json_records.append({
            "county": r.county, "sale_date": r.sale_date, "sale_time": r.sale_time,
            "parcel_id": r.parcel_id, "case_number": r.case_number,
            "certificate_number": r.certificate_number,
            "owner_name": r.owner_name, "mailing_address": mailing,
            "property_address": r.property_address,
            "property_use": r.property_use, "acreage": r.acreage,
            "enriched": bool(enr.get("ok")),
            "opening_bid": r.opening_bid, "assessed_value": r.assessed_value,
            "bid_to_value_pct": ratio, "buybox": flag, "buybox_notes": reasons,
            "anomalies": judgment.find_anomalies(r), "status": status,
            "auction_url": r.auction_url, "appraiser_url": r.appraiser_url,
            "lat": latlng[0], "lng": latlng[1],
        })
        c = by_county.setdefault(r.county, {"total": 0, "scheduled": 0, "redeemed": 0})
        c["total"] += 1
        c["redeemed" if redeemed else "scheduled"] += 1

    (out / "master_list.tsv").write_text("\n".join(tsv_lines) + "\n", encoding="utf-8")
    feed = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_run": run_dir.name,
        "counts": {
            "total": len(json_records),
            "scheduled": len(json_records) - n_redeemed,
            "redeemed": n_redeemed,
            "counties": len(by_county),
            "by_county": dict(sorted(by_county.items())),
        },
        # Client-set per-county limits from config/buybox.yaml (may be empty).
        "county_caps": cfg.get("county_caps") or {},
        # Clerk of Court pages per county from config/clerk_sites.yaml.
        "clerk_sites": _load_clerk_sites(),
        "records": json_records,
    }
    (out / "master_list.json").write_text(json.dumps(feed, indent=1), encoding="utf-8")
    log.info("Exported %d records to %s (tsv + json)", len(json_records), out)
    return feed["counts"]

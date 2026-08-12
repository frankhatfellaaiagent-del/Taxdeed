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

from . import diffing, judgment

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
EXPORTS_DIR = ROOT / "data" / "exports"

TSV_COLUMNS = ["County", "Sale Date", "Sale Time", "Parcel ID", "Case #",
               "Certificate #", "Property Address", "Opening Bid", "Assessed Value",
               "Bid/Value %", "Buy-Box", "Buy-Box Notes", "Status", "Auction Page",
               "Appraiser Record"]


def _clean(v) -> str:
    return str(v if v is not None else "").replace("\t", " ").replace("\n", " ").strip()


def export_run(run_dir: str | Path, out_dir: str | Path | None = None) -> dict:
    run_dir = Path(run_dir)
    out = Path(out_dir) if out_dir else EXPORTS_DIR
    out.mkdir(parents=True, exist_ok=True)

    records = diffing.load_run_records(run_dir)
    records, _ = judgment.dedupe(records)
    cfg = judgment.load_buybox(None)
    records.sort(key=lambda r: (r.sale_date[6:] + r.sale_date[:2] + r.sale_date[3:5],
                                r.county, r.parcel_id))

    tsv_lines = ["\t".join(TSV_COLUMNS)]
    json_records = []
    by_county: dict[str, dict] = {}
    n_redeemed = 0
    for r in records:
        flag, reasons = judgment.buybox_flag(r, cfg)
        redeemed = "redeem" in (r.auction_status or "").lower()
        n_redeemed += redeemed
        ratio = round(100 * r.opening_bid / r.assessed_value) \
            if r.opening_bid and r.assessed_value else None
        status = "Redeemed" if redeemed else "Scheduled"
        tsv_lines.append("\t".join(_clean(x) for x in [
            r.county, r.sale_date, r.sale_time, r.parcel_id, r.case_number,
            r.certificate_number, r.property_address, r.opening_bid or "",
            r.assessed_value or "", ratio if ratio is not None else "", flag, reasons,
            status, r.auction_url, r.appraiser_url]))
        json_records.append({
            "county": r.county, "sale_date": r.sale_date, "sale_time": r.sale_time,
            "parcel_id": r.parcel_id, "case_number": r.case_number,
            "certificate_number": r.certificate_number,
            "property_address": r.property_address,
            "opening_bid": r.opening_bid, "assessed_value": r.assessed_value,
            "bid_to_value_pct": ratio, "buybox": flag, "buybox_notes": reasons,
            "anomalies": judgment.find_anomalies(r), "status": status,
            "auction_url": r.auction_url, "appraiser_url": r.appraiser_url,
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
        "records": json_records,
    }
    (out / "master_list.json").write_text(json.dumps(feed, indent=1), encoding="utf-8")
    log.info("Exported %d records to %s (tsv + json)", len(json_records), out)
    return feed["counts"]

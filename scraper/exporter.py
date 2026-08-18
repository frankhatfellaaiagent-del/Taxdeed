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
               "Appraiser Record", "Clerk Case File", "Deed Status", "Case Flags",
               "Latitude", "Longitude"]


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

    json_records = []
    for r in records:
        enr = enrichment.get(f"{r.county}|{r.parcel_id}|{r.case_number}", {})
        mailing = ""
        case: dict = {}
        parcel_latlng = None
        if enr.get("ok"):
            parcel = enr.get("parcel") or {}          # ReportAll, when enabled
            r.owner_name = r.owner_name or enr.get("owner_name", "") or parcel.get("owner", "")
            r.property_use = (r.property_use or enr.get("property_use", "")
                              or parcel.get("land_use", ""))
            r.acreage = r.acreage or enr.get("acreage", "") or str(parcel.get("acreage", "") or "")
            mailing = enr.get("mailing_address", "") or parcel.get("mailing_address", "")
            # This parcel's own clerk case file + what the paperwork says.
            case = {k: enr[k] for k in
                    ("clerk_case_url", "deed_status", "applicant", "applicant_address",
                     "case_docs", "case_flags", "docs_read")
                    if enr.get(k)}
            # A true parcel centroid beats a rooftop geocode (and works for
            # the vacant lots that have no street address at all).
            if parcel.get("lat") is not None and parcel.get("lng") is not None:
                parcel_latlng = [parcel["lat"], parcel["lng"]]
        flag, reasons = judgment.buybox_flag(r, cfg)
        redeemed = "redeem" in (r.auction_status or "").lower()
        ratio = round(100 * r.opening_bid / r.assessed_value) \
            if r.opening_bid and r.assessed_value else None
        status = "Redeemed" if redeemed else "Scheduled"
        latlng = parcel_latlng or coords.get(r.property_address) or [None, None]
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
            **case,      # clerk_case_url, deed_status, applicant, case_docs, case_flags
        })

    # A run only covers the counties it scraped. Carry the previous feed's
    # records forward for every county NOT in this run, so a partial run
    # (a pilot, a validation dispatch, one county erroring out) refreshes its
    # counties without wiping the rest of the board. county_runs records which
    # run each county's rows came from, so staleness is visible, not silent.
    run_counties = {rec["county"] for rec in json_records}
    county_runs = {c: run_dir.name for c in sorted(run_counties)}
    prior_path = out / "master_list.json"
    if prior_path.exists():
        try:
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
            carried = [rec for rec in prior.get("records", [])
                       if rec.get("county") not in run_counties]
            for rec in carried:
                county_runs.setdefault(rec["county"],
                                       (prior.get("county_runs") or {}).get(rec["county"],
                                                                            prior.get("source_run", "")))
            if carried:
                log.info("Carrying forward %d records from %d counties not in this run",
                         len(carried), len({r['county'] for r in carried}))
            json_records += carried
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("previous feed unreadable (%s); exporting this run only", exc)

    def _date_key(rec):
        d = rec.get("sale_date") or ""
        return (d[6:] + d[:2] + d[3:5] if len(d) == 10 else "99999999",
                rec.get("county", ""), rec.get("parcel_id", ""))
    json_records.sort(key=_date_key)

    by_county: dict[str, dict] = {}
    n_redeemed = 0
    tsv_lines = ["\t".join(TSV_COLUMNS)]
    for rec in json_records:
        redeemed = rec.get("status") == "Redeemed"
        n_redeemed += redeemed
        c = by_county.setdefault(rec["county"], {"total": 0, "scheduled": 0, "redeemed": 0})
        c["total"] += 1
        c["redeemed" if redeemed else "scheduled"] += 1
        tsv_lines.append("\t".join(_clean(x) for x in [
            rec["county"], rec["sale_date"], rec["sale_time"], rec["parcel_id"],
            rec["case_number"], rec["certificate_number"], rec.get("owner_name", ""),
            rec.get("mailing_address", ""), rec["property_address"],
            rec.get("property_use", ""), rec.get("acreage", ""),
            rec["opening_bid"] or "", rec["assessed_value"] or "",
            rec["bid_to_value_pct"] if rec["bid_to_value_pct"] is not None else "",
            rec["buybox"], rec["buybox_notes"], rec["status"],
            rec["auction_url"], rec["appraiser_url"],
            rec.get("clerk_case_url", ""), rec.get("deed_status", ""),
            "; ".join(rec.get("case_flags", [])),
            rec["lat"] if rec.get("lat") is not None else "",
            rec["lng"] if rec.get("lng") is not None else ""]))

    (out / "master_list.tsv").write_text("\n".join(tsv_lines) + "\n", encoding="utf-8")
    feed = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_run": run_dir.name,
        "county_runs": county_runs,
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
        # The full buy-box (config/buybox.yaml) as this feed's DEFAULT. Each
        # dashboard team gets its own editable copy seeded from this the first
        # time they open Settings; buy-box flags are computed client-side from
        # whichever config is active, not baked into the record here — this is
        # only the starting point every team customizes for themselves.
        "default_buybox": {
            "target_counties": cfg.get("target_counties") or [],
            "excluded_counties": cfg.get("excluded_counties") or [],
            "land_use_keywords": cfg.get("land_use_keywords") or [],
            "non_land_keywords": cfg.get("non_land_keywords") or [],
            "max_opening_bid": cfg.get("max_opening_bid"),
            "county_caps": cfg.get("county_caps") or {},
        },
        "records": json_records,
    }
    (out / "master_list.json").write_text(json.dumps(feed, indent=1), encoding="utf-8")
    log.info("Exported %d records to %s (tsv + json)", len(json_records), out)
    return feed["counts"]

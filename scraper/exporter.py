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


def _load_auction_sites() -> dict:
    """config/counties.json → {slug: auction_url} for the discovered online sites.

    So the app can link EVERY online county straight to its own RealAuction
    site — even one carrying no sales this refresh — instead of leaving it a
    dead end that reads as if we were behind.
    """
    p = ROOT / "config" / "counties.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("counties.json unreadable (%s); online counties ship without an auction link", exc)
        return {}
    return {c["slug"]: c["url"] for c in data.get("counties", []) if c.get("slug") and c.get("url")}


def _load_counties_registry() -> list[dict]:
    """config/florida_counties.json → all 67 counties with coverage status.

    The registry is what lets the app show EVERY Florida county — the ones with
    no online auction included — with each county's sale method and clerk links.
    Online counties are given an `auction_url` (their own RealAuction site) so
    the app can always link out, even when the county has no current sales.
    """
    p = ROOT / "config" / "florida_counties.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("florida_counties.json unreadable (%s); feed ships without the registry", exc)
        return []
    counties = data.get("counties", [])
    auction_sites = _load_auction_sites()
    for c in counties:
        if c.get("coverage") == "online" and not c.get("auction_url"):
            url = auction_sites.get(c.get("slug"))
            if url:
                c["auction_url"] = url
    return counties


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
        parcel_geom = ""
        # The county appraiser "quick look" (only present when it resolved).
        if enr.get("ok"):
            r.owner_name = r.owner_name or enr.get("owner_name", "")
            r.property_use = r.property_use or enr.get("property_use", "")
            r.acreage = r.acreage or enr.get("acreage", "")
            mailing = enr.get("mailing_address", "") or mailing
            # This parcel's own clerk case file + what the paperwork says.
            case = {k: enr[k] for k in
                    ("clerk_case_url", "deed_status", "applicant", "applicant_address",
                     "case_docs", "case_flags", "docs_read")
                    if enr.get(k)}
        # The ReportAll parcel record (centroid, boundary, owner, mailing) stands
        # on its own — a lookup by parcel number, independent of whether the
        # appraiser resolved. Its centroid and boundary always win (a true parcel
        # geometry beats a rooftop geocode); its text fields only backfill what
        # the appraiser didn't provide. "enriched" below stays tied to the
        # appraiser verification specifically, never to a geometry-only lookup.
        parcel = enr.get("parcel") or {}
        if parcel:
            r.owner_name = r.owner_name or parcel.get("owner", "")
            r.property_use = r.property_use or parcel.get("land_use", "")
            r.acreage = r.acreage or str(parcel.get("acreage", "") or "")
            mailing = mailing or parcel.get("mailing_address", "")
            if parcel.get("lat") is not None and parcel.get("lng") is not None:
                parcel_latlng = [parcel["lat"], parcel["lng"]]
            # The true parcel boundary (WKT MULTIPOLYGON) — the dashboard
            # outlines it on the per-card map so you see the exact lot.
            parcel_geom = parcel.get("geometry_wkt", "") or ""
        # Some counties hyperlink the case number on the auction page straight
        # to the clerk's tax deed record — the only case-file link available
        # where no clerk-portal resolver exists. Surface it as the case link
        # plus one document row; a resolved case file (richer: real doc list,
        # applicant, deed status) overrides it below via **case.
        scraped_case = {}
        if r.clerk_case_url:
            scraped_case = {"clerk_case_url": r.clerk_case_url,
                            "case_docs": [{"name": "Tax deed record (clerk)",
                                           "date": "", "url": r.clerk_case_url}]}
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
            "bid_to_value_pct": ratio,
            "anomalies": judgment.find_anomalies(r), "status": status,
            "auction_url": r.auction_url, "appraiser_url": r.appraiser_url,
            "lat": latlng[0], "lng": latlng[1],
            "parcel_geometry": parcel_geom,
            **scraped_case,   # case link published on the auction page itself
            **case,      # clerk_case_url, deed_status, applicant, case_docs, case_flags
            **({"land_check": enr["land_check"]} if enr.get("land_check") else {}),
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
                # Older feeds baked per-record flags in; the JSON no longer
                # carries them (the dashboard computes per-team flags itself).
                rec.pop("buybox", None)
                rec.pop("buybox_notes", None)
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
        # The TSV (the operator's Google Sheet mirror) still carries buy-box
        # columns, computed here from config/buybox.yaml. The public JSON does
        # not — each dashboard team computes its own flags client-side.
        flag, reasons = judgment.buybox_flag(judgment.record_from_feed(rec), cfg)
        tsv_lines.append("\t".join(_clean(x) for x in [
            rec["county"], rec["sale_date"], rec["sale_time"], rec["parcel_id"],
            rec["case_number"], rec["certificate_number"], rec.get("owner_name", ""),
            rec.get("mailing_address", ""), rec["property_address"],
            rec.get("property_use", ""), rec.get("acreage", ""),
            rec["opening_bid"] or "", rec["assessed_value"] or "",
            rec["bid_to_value_pct"] if rec["bid_to_value_pct"] is not None else "",
            flag, reasons, rec["status"],
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
            "counties_total": 67,
            "by_county": dict(sorted(by_county.items())),
        },
        # Clerk of Court pages per county from config/clerk_sites.yaml.
        "clerk_sites": _load_clerk_sites(),
        # ALL 67 Florida counties with how each sells tax deeds
        # (config/florida_counties.json) — so the app can show every county,
        # including the ones that only sell in person at the courthouse.
        "counties_registry": _load_counties_registry(),
        # A NEUTRAL buy-box template — the starting point every new team's
        # editable buy-box is seeded from (flags are computed client-side).
        # Deliberately generic: all counties targeted, common land vocabulary,
        # no caps. No customer's actual criteria ever ships in the public feed;
        # each team's real buy-box lives in their private Firestore doc.
        "default_buybox": {
            "target_counties": sorted(by_county),
            "excluded_counties": [],
            "land_use_keywords": ["vacant", "land", "acreage", "agricultur", "timber",
                                   "pasture", "grove", "ranch", "farm", "rural",
                                   "grazing", "orchard", "nursery"],
            "non_land_keywords": ["condo", "townhouse", "townhome", "mobile home park",
                                   "commercial", "industrial", "warehouse", "office", "retail"],
            "max_opening_bid": None,
            "county_caps": {},
        },
        "records": json_records,
    }
    (out / "master_list.json").write_text(json.dumps(feed, indent=1), encoding="utf-8")
    log.info("Exported %d records to %s (tsv + json)", len(json_records), out)
    return feed["counts"]

"""Deterministic parts of the judgment layer: buy-box flagging, anomaly
detection, and cleanup. The tax-deed-scrub skill reviews these outputs and
applies human-style judgment on top (REVIEW rows, weird anomalies, drift)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import AuctionRecord

DEFAULT_BUYBOX_PATH = Path(__file__).resolve().parent.parent / "config" / "buybox.yaml"


def _norm_county(name: str) -> str:
    """'St. Lucie' / 'st-lucie' / 'stlucie' all compare equal."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


def load_buybox(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_BUYBOX_PATH
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    for key in ("land_use_keywords", "non_land_keywords"):
        cfg[key] = [str(x).lower() for x in (cfg.get(key) or [])]
    for key in ("target_counties", "excluded_counties"):
        cfg[key] = [_norm_county(str(x)) for x in (cfg.get(key) or [])]
    return cfg


def buybox_flag(rec: AuctionRecord, cfg: dict) -> tuple[str, str]:
    """Return (flag, reasons). Flag is MATCH / REVIEW / NO.

    MATCH  = target county AND property use looks like land/rural.
    REVIEW = target county but property use is missing/ambiguous — worth a look.
    NO     = excluded county, or use clearly not land.
    """
    county = _norm_county(rec.county)
    reasons = []
    if county in cfg["excluded_counties"]:
        return "NO", "excluded county (coastal/metro)"
    if county not in cfg["target_counties"]:
        return "NO", "county not in target list"
    reasons.append("target county")

    def kw_hits(keywords: list[str], text: str) -> list[str]:
        # Word-boundary prefix match: "timber" hits "TIMBERLAND" but "land"
        # does not hit "LAKELAND".
        return [k for k in keywords if re.search(r"\b" + re.escape(k), text)]

    use = (rec.property_use or "").lower()
    address = (rec.property_address or "").lower()

    max_bid = cfg.get("max_opening_bid")
    if max_bid and rec.opening_bid and rec.opening_bid > float(max_bid):
        return "NO", f"opening bid over cap (${rec.opening_bid:,.0f} > ${float(max_bid):,.0f})"

    if use:
        if kw_hits(cfg["non_land_keywords"], use):
            return "NO", f"target county but non-land use ({rec.property_use})"
        land = kw_hits(cfg["land_use_keywords"], use)
        if land:
            reasons.append("land/rural use: " + ", ".join(land))
            return "MATCH", "; ".join(reasons)
        return "NO", f"target county but property use not land-like ({rec.property_use})"

    # No property use on the auction page: fall back to address hints, but
    # always send to REVIEW rather than auto-matching.
    addr_hints = kw_hits(cfg["land_use_keywords"], address)
    if addr_hints:
        reasons.append("no property use; address suggests land: " + ", ".join(addr_hints))
    else:
        reasons.append("property use unknown — verify on appraiser site")
    return "REVIEW", "; ".join(reasons)


def find_anomalies(rec: AuctionRecord) -> list[str]:
    out = []
    if not rec.parcel_id:
        out.append("missing parcel ID")
    if rec.opening_bid is None:
        out.append("missing opening bid")
    elif rec.opening_bid <= 0:
        out.append("opening bid <= 0")
    if rec.assessed_value is None:
        out.append("missing assessed value")
    elif rec.opening_bid and rec.assessed_value and rec.opening_bid > rec.assessed_value:
        out.append("opening bid exceeds assessed value")
    if rec.auction_status and re.search(r"cancel|redeem|remov", rec.auction_status, re.I):
        out.append(f"status: {rec.auction_status}")
    if not rec.property_address:
        out.append("missing property address")
    return out


def dedupe(records: list[AuctionRecord]) -> tuple[list[AuctionRecord], int]:
    seen: dict[tuple, AuctionRecord] = {}
    for r in records:
        k = r.key()
        if k in seen:
            # Keep the row with more filled fields.
            old = seen[k]
            if sum(1 for v in r.to_dict().values() if v) > sum(1 for v in old.to_dict().values() if v):
                seen[k] = r
        else:
            seen[k] = r
    return list(seen.values()), len(records) - len(seen)

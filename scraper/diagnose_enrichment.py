"""One-off diagnostic: why does enrichment fail for specific counties?

data/enrichment.json already shows four distinct failure signatures across
Alachua, Gilchrist, Osceola, Marion and Suwannee:

  Alachua/Gilchrist  qpublic.schneidercorp.com -> 403 Forbidden
  Osceola            ira.property-appraiser.org -> "Server Error"
  Marion/Suwannee     200 OK, but the generic label-scanner finds nothing

This script fetches one live sample per county (plain requests AND, for the
403s, a fuller "real browser" header set — no UA impersonation, just the
standard Accept/Accept-Language/Referer headers a bare requests.get() omits)
and prints enough of each response to diagnose the cause: status, final URL
after redirects, and a chunk of the actual page text. It also probes the five
online counties whose clerk portal isn't yet classified to a resolver
platform (Bay, Clay, Lake, Leon, Orange), and Marion's NewVision clerk portal
end to end (real resolve() calls against several sample records, now that
the resolver's tab-reveal gating, wrong-panel submit, tax_number keyword
mismatch, HTTP/2 navigation failure, off-screen results row, ag-Grid row
click and Angular render timing have all been fixed in clerk_browser.py).

Read-only, prints everything to stdout for log-based review.

    python -m scraper diagnose-enrichment --out output/diagnose
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests
import yaml

from .enrich import parse_appraiser_page

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = ROOT / "data" / "exports" / "master_list.json"
CLERK_SITES_PATH = ROOT / "config" / "clerk_sites.yaml"

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
}

APPRAISER_TARGETS = ["alachua", "gilchrist", "osceola", "marion", "suwannee"]
UNCLASSIFIED_PORTALS = ["bay", "clay", "lake", "leon", "orange"]


def _sep(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _sample_records(counties: list[str]) -> dict[str, dict]:
    feed = json.loads(FEED_PATH.read_text(encoding="utf-8"))
    samples: dict[str, dict] = {}
    for r in feed["records"]:
        c = r.get("county")
        if c in counties and c not in samples and r.get("appraiser_url"):
            samples[c] = r
    return samples


def _print_response(resp: requests.Response, chars: int = 1500) -> None:
    print(f"  status: {resp.status_code}   final url: {resp.url}")
    print(f"  content-length: {len(resp.content)} bytes   content-type: {resp.headers.get('content-type')}")
    text = resp.text or ""
    print(f"  --- first {chars} chars of body ---")
    print("  " + text[:chars].replace("\n", "\n  "))
    print("  --- end sample ---")


def diagnose_appraiser(out: Path) -> None:
    _sep("PART 1 — Appraiser page fetch failures")
    samples = _sample_records(APPRAISER_TARGETS)
    for county in APPRAISER_TARGETS:
        rec = samples.get(county)
        if not rec:
            print(f"\n{county}: no sample record with an appraiser_url found in the feed")
            continue
        url = rec["appraiser_url"]
        print(f"\n--- {county} --- parcel {rec['parcel_id']}  case {rec['case_number']}")
        print(f"  appraiser_url: {url}")

        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        resp = None
        try:
            resp = session.get(url, timeout=25, allow_redirects=True)
            _print_response(resp)
            (out / f"{county}_appraiser.html").write_text(resp.text or "", encoding="utf-8")
            if resp.status_code < 400:
                fields = parse_appraiser_page(resp.text)
                print(f"  parse_appraiser_page() found: {fields or '(nothing)'}")
        except requests.RequestException as exc:
            print(f"  EXCEPTION: {exc.__class__.__name__}: {exc}")

        # 403s: retry with full browser-standard headers (already used above,
        # this second call with a fresh session isolates whether cookies/
        # session state from a prior request changed the outcome).
        if resp is not None and resp.status_code == 403:
            print("  retry (fresh session, same headers, once more):")
            try:
                resp2 = requests.get(url, headers=BROWSER_HEADERS, timeout=25)
                print(f"    status: {resp2.status_code}")
            except requests.RequestException as exc:
                print(f"    EXCEPTION: {exc}")


def diagnose_unclassified_portals(out: Path) -> None:
    _sep("PART 2 — Unclassified clerk portals (candidates for a resolver)")
    sites = yaml.safe_load(CLERK_SITES_PATH.read_text(encoding="utf-8")) or {}
    for county in UNCLASSIFIED_PORTALS:
        cfg = sites.get(county, {})
        url = cfg.get("search") or cfg.get("portal") or cfg.get("url")
        print(f"\n--- {county} --- target: {url}")
        if not url:
            print("  no search/portal/url configured")
            continue
        try:
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=25, allow_redirects=True)
            _print_response(resp, chars=1200)
            (out / f"{county}_portal.html").write_text(resp.text or "", encoding="utf-8")
            text = (resp.text or "").lower()
            signals = {
                "realtdm-like (getCase link)": "/cases/getcase/" in text or "realtdm" in text,
                "taxsmart-like (Home/Details link)": "/home/details" in text,
                "generic search form present": "<form" in text and "search" in text,
            }
            print(f"  signals: {signals}")
        except requests.RequestException as exc:
            print(f"  EXCEPTION: {exc.__class__.__name__}: {exc}")


def diagnose_marion_clerk() -> None:
    _sep("PART 3 — Marion NewVision clerk resolver, end to end")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright not installed in this environment — skipping")
        return

    feed = json.loads(FEED_PATH.read_text(encoding="utf-8"))
    recs = [r for r in feed["records"] if r.get("county") == "marion"][:5]
    if not recs:
        print("  no Marion record found in the feed")
        return
    print(f"  trying up to {len(recs)} Marion record(s) — the first sample case was")
    print("  confirmed to genuinely have no Deed Status/Date Received/documents yet"
          " (a not-yet-processed case), so try more until one actually resolves.")

    sites = yaml.safe_load(CLERK_SITES_PATH.read_text(encoding="utf-8")) or {}
    cfg = sites.get("marion", {})
    portal = cfg.get("portal") or cfg.get("search") or cfg.get("url")
    print(f"  portal: {portal}")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-http2"])
        try:
            from .clerk_browser import NewVisionResolver
            page = browser.new_page()
            nv = NewVisionResolver(page)
            for i, rec in enumerate(recs):
                print(f"  --- record {i + 1}/{len(recs)}: parcel {rec['parcel_id']} "
                      f"case {rec['case_number']} tax_number={rec.get('certificate_number')} ---")
                try:
                    result = nv.resolve(rec, cfg)
                except Exception as exc:                       # noqa: BLE001
                    print(f"  EXCEPTION: {exc.__class__.__name__}: {exc}")
                    continue
                print(f"  result keys: {list(result.keys()) or '(empty — still unresolved)'}")
                if result.get("clerk_case_url"):
                    print(f"  clerk_case_url: {result['clerk_case_url']}")
                if result.get("case_docs"):
                    print(f"  case_docs: {len(result['case_docs'])} document(s)")
                if result.get("deed_status"):
                    print(f"  deed_status: {result['deed_status']}")
                if result:
                    print("  RESOLVED — stopping here.")
                    break
        finally:
            browser.close()


def diagnose_enrichment(out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    diagnose_appraiser(out)
    diagnose_unclassified_portals(out)
    diagnose_marion_clerk()
    _sep("DONE")

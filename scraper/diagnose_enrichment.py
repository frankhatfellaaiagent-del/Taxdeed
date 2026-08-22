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
step by step (the browser resolver swallows exceptions silently in normal
operation — this narrates each stage instead).

Read-only, prints everything to stdout for log-based review.

    python -m scraper diagnose-enrichment --out output/diagnose
"""

from __future__ import annotations

import json
import logging
import re
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
    _sep("PART 3 — Marion NewVision clerk resolver, step by step")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright not installed in this environment — skipping")
        return

    feed = json.loads(FEED_PATH.read_text(encoding="utf-8"))
    rec = next((r for r in feed["records"] if r.get("county") == "marion"), None)
    if not rec:
        print("  no Marion record found in the feed")
        return
    print(f"  sample: parcel {rec['parcel_id']}  case {rec['case_number']}  tax_number={rec.get('certificate_number')}")

    sites = yaml.safe_load(CLERK_SITES_PATH.read_text(encoding="utf-8")) or {}
    cfg = sites.get("marion", {})
    portal = cfg.get("portal") or cfg.get("search") or cfg.get("url")
    print(f"  portal: {portal}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            print("  step: goto portal...")
            page.goto(portal, wait_until="domcontentloaded", timeout=20000)
            print(f"    landed at: {page.url}")
            print(f"    title: {page.title()!r}")

            # Dump every visible input's name/id/placeholder — this is the
            # actual state NewVisionResolver._find_input searches over.
            inputs = page.locator("input").all()
            print(f"  step: found {len(inputs)} <input> elements on the page")
            for i, inp in enumerate(inputs[:25]):
                try:
                    print(f"    [{i}] name={inp.get_attribute('name')!r} "
                          f"id={inp.get_attribute('id')!r} "
                          f"placeholder={inp.get_attribute('placeholder')!r} "
                          f"type={inp.get_attribute('type')!r} "
                          f"visible={inp.is_visible()}")
                except Exception as exc:                  # noqa: BLE001
                    print(f"    [{i}] (error reading attributes: {exc})")

            body_text = page.locator("body").inner_text()[:800]
            print(f"  page body text (first 800 chars):\n    {body_text}")

            # End-to-end via the real (now-fixed) methods, narrated — resolve()
            # itself swallows every exception by design, so a plain call gives
            # no signal beyond "empty or not". Calling the same private
            # methods it uses, with prints between them, shows exactly which
            # stage stops working without duplicating any of their logic.
            print("  step: NewVisionResolver real methods, narrated (fresh page)")
            from .clerk_browser import NewVisionResolver, SEARCH_FIELDS
            from .clerk import parse_case_page
            import re as _re
            page2 = browser.new_page()
            try:
                nv = NewVisionResolver(page2)
                print(f"    portal opened: {nv._open_portal(portal)}")
                for field, keywords in SEARCH_FIELDS:
                    value = str(rec.get(field) or rec.get("case_number") or "").strip()
                    if not value:
                        continue
                    nv._select_search_tab(field)
                    box = nv._find_input(keywords)
                    print(f"    field={field} value={value!r} input_found={box is not None}")
                    if box is None:
                        continue
                    box.fill("")
                    box.fill(value)
                    try:
                        near_result = nv._submit_near(box)
                        print(f"      _submit_near(box) returned: {near_result}")
                    except Exception as exc:                  # noqa: BLE001
                        print(f"      _submit_near(box) raised: {exc.__class__.__name__}: {exc}")
                        near_result = None
                    if not near_result:
                        nv._submit(box)
                    try:
                        page2.wait_for_load_state("networkidle", timeout=nv.timeout)
                    except Exception as exc:                  # noqa: BLE001
                        print(f"      wait_for_load_state failed: {exc.__class__.__name__}: {exc}")
                    print(f"      url after submit: {page2.url}")
                    row = page2.locator('table tr:has(a), tr[onclick], a:has-text("View")').first
                    print(f"      result row present: {row.count() > 0}")
                    if row.count():
                        row.click(timeout=5000)
                        page2.wait_for_load_state("networkidle", timeout=nv.timeout)
                        print(f"      url after row click: {page2.url}")
                    html = page2.content()
                    hay = _re.sub(r"[^A-Za-z0-9]", "", html).upper()
                    print(f"      value found on final page: {_re.sub(r'[^A-Za-z0-9]', '', value).upper() in hay}")
                    print(f"      parsed fields: {parse_case_page(html, page2.url)}")
            except Exception as exc:                          # noqa: BLE001
                print(f"    EXCEPTION: {exc.__class__.__name__}: {exc}")
            finally:
                page2.close()
        except Exception as exc:                          # noqa: BLE001
            print(f"  EXCEPTION during portal load: {exc.__class__.__name__}: {exc}")
        finally:
            browser.close()


def diagnose_enrichment(out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    diagnose_appraiser(out)
    diagnose_unclassified_portals(out)
    diagnose_marion_clerk()
    _sep("DONE")

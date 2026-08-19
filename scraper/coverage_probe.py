"""Coverage audit: which Florida counties sell tax deeds where?

Probes the counties NOT covered by RealAuction discovery (plus Broward, which
appears on both platforms) against Grant Street's DeedAuction platform
(https://<slug>.deedauction.net/), saves the HTML evidence, and emits a
classification the COVERAGE.md doc and the 67-county registry are built from.

Read-only and polite: robots.txt is fetched first per host, requests are
spaced by the shared RateLimiter, and only the targeted hosts are probed —
no brute-forcing name variants across the whole state.

    python -m scraper coverage --out output/coverage
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from .discovery import load_counties
from .robots import USER_AGENT
from .sources import RateLimiter

_COUNTIES_JSON = Path(__file__).resolve().parent.parent / "config" / "counties.json"

log = logging.getLogger(__name__)

# All 67 Florida counties (static list — Florida hasn't added a county since
# 1925). Slugs match the scraper's convention: lowercase, punctuation stripped.
FLORIDA_COUNTIES = {
    "alachua": "Alachua", "baker": "Baker", "bay": "Bay", "bradford": "Bradford",
    "brevard": "Brevard", "broward": "Broward", "calhoun": "Calhoun",
    "charlotte": "Charlotte", "citrus": "Citrus", "clay": "Clay",
    "collier": "Collier", "columbia": "Columbia", "desoto": "DeSoto",
    "dixie": "Dixie", "duval": "Duval", "escambia": "Escambia",
    "flagler": "Flagler", "franklin": "Franklin", "gadsden": "Gadsden",
    "gilchrist": "Gilchrist", "glades": "Glades", "gulf": "Gulf",
    "hamilton": "Hamilton", "hardee": "Hardee", "hendry": "Hendry",
    "hernando": "Hernando", "highlands": "Highlands",
    "hillsborough": "Hillsborough", "holmes": "Holmes",
    "indianriver": "Indian River", "jackson": "Jackson",
    "jefferson": "Jefferson", "lafayette": "Lafayette", "lake": "Lake",
    "lee": "Lee", "leon": "Leon", "levy": "Levy", "liberty": "Liberty",
    "madison": "Madison", "manatee": "Manatee", "marion": "Marion",
    "martin": "Martin", "miamidade": "Miami-Dade", "monroe": "Monroe",
    "nassau": "Nassau", "okaloosa": "Okaloosa", "okeechobee": "Okeechobee",
    "orange": "Orange", "osceola": "Osceola", "palmbeach": "Palm Beach",
    "pasco": "Pasco", "pinellas": "Pinellas", "polk": "Polk",
    "putnam": "Putnam", "santarosa": "Santa Rosa", "sarasota": "Sarasota",
    "seminole": "Seminole", "stjohns": "St. Johns", "stlucie": "St. Lucie",
    "sumter": "Sumter", "suwannee": "Suwannee", "taylor": "Taylor",
    "union": "Union", "volusia": "Volusia", "wakulla": "Wakulla",
    "walton": "Walton", "washington": "Washington",
}

# Markers that say "this is a live Grant Street DeedAuction site" vs. a parked
# domain or an error skin.
_DEEDAUCTION_MARKERS = ("deedauction", "tax deed", "auction")


def _fetch(session: requests.Session, url: str, timeout: float = 20.0) -> tuple[int | None, str]:
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        return resp.status_code, resp.text or ""
    except requests.RequestException as e:
        return None, f"{e.__class__.__name__}: {e}"


def _robots_allows(session: requests.Session, base: str) -> tuple[bool, str]:
    import urllib.robotparser
    code, text = _fetch(session, base + "/robots.txt", timeout=15.0)
    if code is None or code >= 400:
        return True, f"robots.txt {'unreachable' if code is None else f'HTTP {code}'}; proceeding politely"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    for path in ("/", "/auctions"):
        if not rp.can_fetch(USER_AGENT, base + path):
            return False, f"robots.txt disallows {path}"
    return True, "robots.txt allows"


def probe_coverage(out_dir: str | Path, delay: float = 3.0) -> dict:
    """Probe DeedAuction for every county RealAuction discovery doesn't cover
    (plus Broward), save HTML evidence, and write coverage.json."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    realauction = {c["slug"] for c in load_counties(_COUNTIES_JSON)}
    # Broward is on RealAuction but also runs broward.deedauction.net — probe it
    # too so the platform-transition question is answered with evidence.
    targets = sorted((set(FLORIDA_COUNTIES) - realauction) | {"broward"})
    log.info("Probing %d counties against deedauction.net: %s", len(targets), ", ".join(targets))

    rate = RateLimiter(base_delay=delay)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    results: dict[str, dict] = {}
    for slug in targets:
        base = f"https://{slug}.deedauction.net"
        entry: dict = {"name": FLORIDA_COUNTIES[slug], "realauction": slug in realauction,
                       "deedauction_host": base}
        rate.wait()
        allowed, robots_note = _robots_allows(session, base)
        entry["robots"] = robots_note
        if not allowed:
            entry["deedauction"] = "robots-blocked"
            results[slug] = entry
            log.info("%s: robots-blocked", slug)
            continue

        rate.wait()
        code, home = _fetch(session, base + "/")
        entry["home_status"] = code
        if code is None:
            entry["deedauction"] = "none"
            entry["error"] = home[:200]
            results[slug] = entry
            log.info("%s: unreachable (%s)", slug, home[:80])
            continue
        if code >= 400:
            entry["deedauction"] = "none"
            results[slug] = entry
            log.info("%s: HTTP %s", slug, code)
            continue

        (out / f"{slug}_home.html").write_text(home, encoding="utf-8")
        rate.wait()
        a_code, auctions = _fetch(session, base + "/auctions")
        entry["auctions_status"] = a_code
        if a_code and a_code < 400 and auctions:
            (out / f"{slug}_auctions.html").write_text(auctions, encoding="utf-8")

        haystack = (home + " " + (auctions if isinstance(auctions, str) else "")).lower()
        live = any(m in haystack for m in _DEEDAUCTION_MARKERS) and len(home) > 2000
        entry["deedauction"] = "live" if live else "unknown-manual-check"
        entry["home_bytes"] = len(home)
        results[slug] = entry
        log.info("%s: %s (home %s bytes, /auctions HTTP %s)", slug, entry["deedauction"], len(home), a_code)

    # Full-state classification: every county, one row.
    coverage = {}
    for slug, name in sorted(FLORIDA_COUNTIES.items()):
        row = {"name": name,
               "realauction": slug in realauction,
               "deedauction": results.get(slug, {}).get("deedauction", "not-probed")}
        if slug in results:
            row["probe"] = results[slug]
        coverage[slug] = row

    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "realauction_count": len(realauction),
               "probed": targets,
               "coverage": coverage}
    (out / "coverage.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("Wrote %s", out / "coverage.json")

    live = [s for s, r in coverage.items() if r["deedauction"] == "live"]
    print(f"\nRealAuction counties: {len(realauction)}")
    print(f"DeedAuction live: {len(live)} -> {', '.join(live) or '(none)'}")
    print(f"Neither (in-person / county-run / none): "
          f"{len([s for s, r in coverage.items() if not r['realauction'] and r['deedauction'] not in ('live',)])}")
    return payload

"""Verify the clerk links in config/florida_counties.json actually resolve.

The registry's clerk_url/sale_list_url for the 21 in-person counties (plus
Okaloosa's Bid4Assets link) were compiled from research, not fetched — the
sandbox that authors this repo has no egress to county/clerk sites. This is
the CI-side check that closes the loop: fetch every link, record its status,
and flag anything that doesn't look like a live page (404s, redirects to a
domain's homepage, parked-domain markers).

    python -m scraper verify-links --out output/link-check
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from .robots import USER_AGENT
from .sources import RateLimiter

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "config" / "florida_counties.json"

_PARKED_MARKERS = ("domain is for sale", "buy this domain", "this domain may be for sale",
                   "godaddy.com/domains", "future home of something quite cool")


def _base_domain(host: str) -> str:
    """Naive eTLD+1: last two dot-labels. Good enough to tell a same-org
    subdomain move (app.x.com <- www.x.com) from a genuine domain change
    (x.com -> unrelated-y.com) for the .com/.org/.gov/.net hosts clerks use."""
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _fetch(session: requests.Session, url: str, timeout: float = 20.0) -> dict:
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        return {"url": url, "ok": False, "status": None, "final_url": None,
                "detail": f"{e.__class__.__name__}: {e}"}
    text = (resp.text or "").lower()
    parked = any(m in text for m in _PARKED_MARKERS)
    req_host = urlparse(url).netloc
    final_host = urlparse(resp.url).netloc
    # A redirect to a different SUBDOMAIN of the same organization (e.g. an
    # apps./online. portal) is normal and common among these clerk sites —
    # only a different registrable domain entirely is a red flag (expired
    # domain sold off, parked, or genuinely wrong link).
    cross_domain = bool(final_host) and _base_domain(final_host) != _base_domain(req_host)
    same_host_redirect_to_root = (not cross_domain
                                   and resp.url.rstrip("/") == f"{urlparse(url).scheme}://{req_host}"
                                   and urlparse(url).path not in ("", "/"))
    ok = resp.status_code < 400 and not parked and not cross_domain
    detail = "ok"
    if resp.status_code >= 400:
        detail = f"HTTP {resp.status_code}"
    elif parked:
        detail = "looks like a parked/for-sale domain page"
    elif cross_domain:
        detail = f"redirected to a different domain ({req_host} -> {final_host}) — site may have moved or expired"
    elif same_host_redirect_to_root:
        detail = "redirected to the site's homepage (path may have moved)"
    return {"url": url, "ok": ok, "status": resp.status_code, "final_url": resp.url,
            "bytes": len(resp.text or ""), "detail": detail}


def verify_links(out_dir: str | Path, delay: float = 2.0) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8")).get("counties", [])

    targets = []
    for c in registry:
        for field in ("clerk_url", "sale_list_url"):
            url = c.get(field)
            if url:
                targets.append((c["slug"], c["name"], field, url))
    # Same URL may serve both fields — check each URL once.
    seen_urls = {}
    for slug, name, field, url in targets:
        seen_urls.setdefault(url, []).append((slug, name, field))

    rate = RateLimiter(base_delay=delay)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    results = []
    for url, refs in seen_urls.items():
        rate.wait()
        r = _fetch(session, url)
        r["used_by"] = [f"{name} ({field})" for _, name, field in refs]
        results.append(r)
        log.info("%s -> %s (%s)", url, "OK" if r["ok"] else "FAIL", r["detail"])

    bad = [r for r in results if not r["ok"]]
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "checked": len(results), "failed": len(bad), "results": results}
    (out / "link_check.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\nChecked {len(results)} unique links, {len(bad)} failed:")
    for r in bad:
        print(f"  FAIL {r['url']}  [{r['detail']}]  used by: {', '.join(r['used_by'])}")
    return payload

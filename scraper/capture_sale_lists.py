"""Evidence capture for the counties RealAuction doesn't cover.

Phase C/E groundwork. Seven Florida counties sell tax deeds somewhere other
than RealAuction and are in the registry with a real sale-list URL but no
adapter yet:

  Okaloosa   online, Bid4Assets           (bid4assets.com/OkaloosaFLTax/listings)
  St. Johns  in person, TaxSmart list     (apps.stjohnsclerk.com/TaxSmart)
  Levy       in person, TaxSmart list     (online.levyclerk.com/TaxSmartWeb)
  Collier    in person, clerk sale list
  Sumter     in person, clerk sale list
  Hardee     in person, clerk sale list
  Columbia   in person, clerk sale list

Before a parser can be written for any of them we need to see what the page
actually is — an HTML table, a PDF, or a JS app that renders its rows client
side. This fetches each sale-list URL (robots-first, rate limited, with the
browser-standard headers a bare requests.get omits, since clerk sites often
403 otherwise) and prints a structural read: content type, whether it looks
JS-rendered, the tables and their header/first-row cells, and the markup
around the first date so the row shape is visible. Read-only.

    python -m scraper capture-sale-lists --out output/sale-lists
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .robots import USER_AGENT
from .sources import RateLimiter

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "config" / "florida_counties.json"

# The seven counties this phase targets, in the order we'll build them.
TARGETS = ["okaloosa", "stjohns", "levy", "collier", "sumter", "hardee", "columbia"]

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
}

DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
# Parcel/tax-deed numbers vary wildly by county; this is a loose "looks like an
# identifier" net just for spotting the data region, not for parsing.
IDENT_RE = re.compile(r"\b[0-9][0-9A-Za-z\-]{5,}\b")
# Client-rendered app markers — if the served HTML is mostly these, the rows
# come from JS and we'll need Playwright to see them.
JS_MARKERS = ("ng-app", "ng-controller", "data-reactroot", "__NEXT_DATA__",
              "id=\"root\"", "id=\"app\"", "ng-version", "window.__")


def _load_registry() -> dict[str, dict]:
    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("counties", list(data.values()))
    return {r["slug"]: r for r in rows if isinstance(r, dict) and r.get("slug")}


def _robots_allows(session: requests.Session, base: str, path: str) -> tuple[bool, str]:
    import urllib.robotparser
    try:
        r = session.get(base + "/robots.txt", timeout=15)
    except requests.RequestException:
        return True, "robots.txt unreachable; proceeding politely"
    if r.status_code >= 400:
        return True, f"robots.txt HTTP {r.status_code}; proceeding politely"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((r.text or "").splitlines())
    ok = rp.can_fetch(USER_AGENT, base + path)
    return ok, "robots allows" if ok else f"robots disallows {path}"


def _describe_pdf(raw: bytes) -> None:
    print("  looks like a PDF.")
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        print(f"  {len(reader.pages)} page(s).")
        text = (reader.pages[0].extract_text() or "") if reader.pages else ""
        print(f"  --- first page text (first 1500 chars) ---")
        print("  " + text[:1500].replace("\n", "\n  "))
        print("  --- end ---")
    except Exception as exc:                               # noqa: BLE001
        print(f"  (could not extract PDF text: {exc.__class__.__name__}: {exc})")


def _describe_html(html: str) -> None:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible = soup.get_text(" ", strip=True)
    low = html.lower()
    js_hits = [m for m in JS_MARKERS if m.lower() in low]
    print(f"  visible text: {len(visible)} chars; <script> tags: {len(BeautifulSoup(html, 'lxml')('script'))}")
    if js_hits and len(visible) < 3000:
        print(f"  LIKELY JS-RENDERED (markers: {js_hits}) — rows probably need Playwright, not requests.")

    tables = soup.find_all("table")
    print(f"  tables: {len(tables)}")
    for i, t in enumerate(tables[:6]):
        rows = t.find_all("tr")
        if len(rows) < 2:
            continue
        head = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
        body = [c.get_text(" ", strip=True) for c in rows[1].find_all(["th", "td"])]
        print(f"    table[{i}]: {len(rows)} rows; header={head[:8]}")
        print(f"                first data row={body[:8]}")

    dates = DATE_RE.findall(visible)
    print(f"  date-like tokens in visible text: {len(dates)}  sample={dates[:6]}")

    # Dump the markup around the first date — that's almost always inside the
    # sale-list row structure we need to parse.
    m = DATE_RE.search(html)
    if m:
        start = max(0, m.start() - 400)
        print(f"  --- markup around first date (offset {m.start()}) ---")
        print("  " + html[start:m.start() + 500].replace("\n", "\n  "))
        print("  --- end ---")
    else:
        print("  (no date tokens found in the served markup — likely JS-rendered or a link-only landing page)")

    # Links that look like they lead to a sale list / PDF, in case the real
    # data is one hop away.
    hop = []
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True).lower()
        href = a["href"].lower()
        if any(k in label + " " + href for k in ("sale list", "upcoming", "tax deed", ".pdf", "salelist", "auction")):
            hop.append((a.get_text(" ", strip=True)[:50], a["href"]))
    if hop:
        print(f"  candidate sale-list links ({len(hop)}):")
        for text, href in hop[:12]:
            print(f"    {text!r} -> {href}")


def capture_sale_lists(out_dir: str | Path, delay: float = 3.0,
                       counties: list[str] | None = None) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    reg = _load_registry()
    targets = counties or TARGETS
    rate = RateLimiter(base_delay=delay)
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    for slug in targets:
        entry = reg.get(slug)
        print(f"\n{'=' * 74}\n{slug} — {entry.get('name') if entry else '?'}\n{'=' * 74}")
        if not entry:
            print("  not in registry")
            continue
        url = entry.get("sale_list_url")
        print(f"  coverage: {entry.get('coverage')}   sale_list_url: {url}")
        if not url:
            print("  no sale_list_url configured")
            continue

        from urllib.parse import urlsplit
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rate.wait()
        allowed, note = _robots_allows(session, base, parts.path or "/")
        print(f"  {note}")
        if not allowed:
            print("  skipping (robots)")
            continue

        rate.wait()
        try:
            resp = session.get(url, timeout=30, allow_redirects=True)
        except requests.RequestException as exc:
            print(f"  EXCEPTION: {exc.__class__.__name__}: {exc}")
            continue
        ctype = resp.headers.get("content-type", "")
        print(f"  status: {resp.status_code}   final url: {resp.url}")
        print(f"  content-type: {ctype}   bytes: {len(resp.content)}")
        if resp.status_code >= 400:
            print(f"  --- error body (first 600 chars) ---\n  {(resp.text or '')[:600]}")
            continue

        ext = "pdf" if ("pdf" in ctype.lower() or resp.content[:5] == b"%PDF-") else "html"
        (out / f"{slug}_salelist.{ext}").write_bytes(resp.content)
        if ext == "pdf":
            _describe_pdf(resp.content)
        else:
            _describe_html(resp.text or "")

    print(f"\n{'=' * 74}\nDONE\n{'=' * 74}")

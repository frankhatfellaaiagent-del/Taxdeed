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


def _dump_forms(soup: BeautifulSoup) -> None:
    """Every <form>'s action/method + fields. TaxSmart portals (St. Johns,
    Levy) don't publish a static list — you submit a sale-date search — so we
    need the field names to know how to query the upcoming window."""
    forms = soup.find_all("form")
    if not forms:
        return
    print(f"  forms: {len(forms)}")
    for i, f in enumerate(forms[:4]):
        print(f"    form[{i}] method={f.get('method', 'get')} action={f.get('action')!r}")
        for el in f.find_all(["input", "select", "textarea", "button"]):
            tag = el.name
            typ = el.get("type", "")
            nid = el.get("id", "")
            nam = el.get("name", "")
            val = (el.get("value", "") or el.get_text(" ", strip=True))[:40]
            if nam or nid:
                print(f"      <{tag} type={typ!r} id={nid!r} name={nam!r} value={val!r}>")
    # jQuery-UI tab labels reveal the search categories (Sale Date, Parcel, …).
    tabs = [a.get_text(" ", strip=True) for a in soup.select("a[href^='#tabs-'], .ui-tabs-anchor")]
    if tabs:
        print(f"  tab labels: {tabs[:12]}")


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

    _dump_forms(soup)

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
        print("  no date tokens in the served markup. First 1600 chars of visible text:")
        print("  " + visible[:1600].replace("\n", "\n  "))

    # All links + a PDF flag: a small clerk (Sumter/Hardee) often just links
    # the upcoming-sales PDF or a per-sale page rather than tabulating rows.
    pdfs = [(a.get_text(" ", strip=True)[:60], a["href"])
            for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()]
    if pdfs:
        print(f"  PDF links ({len(pdfs)}):")
        for text, href in pdfs[:15]:
            print(f"    {text!r} -> {href}")
    links = [(a.get_text(' ', strip=True)[:45], a['href']) for a in soup.find_all("a", href=True)
             if a.get_text(strip=True) and not a['href'].startswith(("#", "javascript:", "mailto:", "tel:"))]
    if links:
        print(f"  all text links ({len(links)}, first 25):")
        for text, href in links[:25]:
            print(f"    {text!r} -> {href}")


def _render_capture(url: str, out: Path, slug: str) -> None:
    """Load a JS page in Chromium, dump the rendered row structure, and log
    every response URL — the Bid4Assets (and Laserfiche) listings come from a
    data endpoint we want to find and hit directly."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (Playwright not installed — cannot render)")
        return
    responses: list[str] = []
    print("  [render] loading in Chromium…")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-http2"])
        try:
            page = browser.new_page(user_agent=BROWSER_HEADERS["User-Agent"])
            page.on("response", lambda r: responses.append(f"{r.status} {r.request.method} {r.url}"))
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(1500)
            html = page.content()
            (out / f"{slug}_rendered.html").write_text(html, encoding="utf-8")
            soup = BeautifulSoup(html, "lxml")
            tables = soup.find_all("table")
            print(f"  [render] rendered {len(html)} bytes; tables={len(tables)}")
            for i, t in enumerate(tables[:4]):
                rows = t.find_all("tr")
                if len(rows) < 2:
                    continue
                head = [c.get_text(' ', strip=True) for c in rows[0].find_all(['th', 'td'])]
                body = [c.get_text(' ', strip=True) for c in rows[1].find_all(['th', 'td'])]
                print(f"    table[{i}]: {len(rows)} rows; header={head[:8]}")
                print(f"                first data row={body[:8]}")
            dates = DATE_RE.findall(soup.get_text(' ', strip=True))
            print(f"  [render] date tokens after render: {len(dates)}  sample={dates[:6]}")
            # Data endpoints worth hitting directly next round.
            data_urls = [r for r in responses
                         if any(k in r.lower() for k in ("api", "json", "listing", "search", "data", "asset"))
                         and "google" not in r.lower() and "font" not in r.lower()]
            print(f"  [render] candidate data requests ({len(data_urls)}):")
            for r in data_urls[:20]:
                print(f"    {r}")
        except Exception as exc:                           # noqa: BLE001
            print(f"  [render] EXCEPTION: {exc.__class__.__name__}: {exc}")
        finally:
            browser.close()


# Round-3 platform probes. The counties cluster onto a few platforms; these
# hit each platform the way its own front-end does so we can see the real
# sale rows before writing an adapter.
TAXSMART_SLUGS = {"stjohns", "levy"}
# Clerk sites on the "kmatailwind" template render their sale list from a
# docaccess.com JSON feed keyed by the clerk's domain.
DOCACCESS_DOMAINS = {"sumter": "sumterclerk.com", "columbia": "columbiaclerk.com"}
BID4ASSETS_SLUGS = {"okaloosa"}


def _probe_bid4assets(session: requests.Session, url: str, out: Path, slug: str) -> None:
    """Find where Bid4Assets' property list comes from. The landing page loads
    fine with requests (headless Chromium gets 403), shows the sale date and a
    'Download Property List' control, and carries LandingPageId +
    SelectedSaleDateId — the auction rows are fetched from those."""
    import re as _re
    from urllib.parse import urljoin
    print("  [deep] Bid4Assets property-list hunt")
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException as exc:
        print(f"    fetch failed: {exc}")
        return
    html = r.text
    soup = BeautifulSoup(html, "lxml")

    # The Download Property List / export control and its target.
    for el in soup.find_all(["a", "button"]):
        t = el.get_text(" ", strip=True).lower()
        if any(k in t for k in ("download", "property list", "export")):
            print(f"    control: <{el.name}> text={el.get_text(' ', strip=True)!r} "
                  f"href={el.get('href')} onclick={el.get('onclick')} "
                  f"data-url={el.get('data-url')} data-href={el.get('data-href')}")

    # Identifiers the rows are keyed by.
    for name in ("LandingPageId", "SelectedSaleDateId", "__RequestVerificationToken"):
        el = soup.find(attrs={"name": name}) or soup.find(id=name)
        if el:
            print(f"    {name} = {el.get('value')!r}")

    # Any URL in the markup that looks like the data/download endpoint.
    urls = set(_re.findall(r'https?://[^\s"\'<>]+|/[A-Za-z0-9_./\-]{4,}', html))
    hits = sorted(u for u in urls if _re.search(
        r'download|propertylist|property-list|csv|xls|getauction|auctionitem|/mvc/|listing|export|saleauction', u, _re.I))
    print(f"    candidate data/download URLs ({len(hits)}):")
    for u in hits[:35]:
        print(f"      {urljoin(url, u)}")

    # Auction data embedded in a <script> (Bid4Assets sometimes inlines it).
    for s in soup.find_all("script"):
        txt = s.string or ""
        if "auction" in txt.lower() and len(txt) > 200 and ("[" in txt or "{" in txt):
            print(f"    inline script with auction data ({len(txt)} chars): {txt[:500]}")
            break

    # The download control fires propertyListDownload(); find its definition so
    # we know the real request (URL, method, POST body) it issues.
    from urllib.parse import urljoin as _urljoin
    def _dump_func(source: str, label: str) -> bool:
        idx = source.find("propertyListDownload")
        # Prefer the definition ("function propertyListDownload") over the call.
        deff = source.find("function propertyListDownload")
        if deff != -1:
            idx = deff
        if idx == -1:
            return False
        print(f"    propertyListDownload in {label}: {source[idx:idx + 700]!r}")
        return True
    found = False
    for s in soup.find_all("script"):
        if s.string and "propertyListDownload" in s.string:
            found = _dump_func(s.string, "inline") or found
    # Not inline? It's in one of the bundled scripts — pull each and search.
    if not found:
        for s in soup.find_all("script", src=True):
            src = _urljoin(url, s["src"])
            if not any(k in src.lower() for k in ("main.js", "county", "auction", "listing", "channel")):
                continue
            try:
                js = session.get(src, timeout=30).text
            except requests.RequestException:
                continue
            if "propertyListDownload" in js and _dump_func(js, src):
                found = True
                break
    if not found:
        print("    propertyListDownload definition not located in page or bundled scripts")

    # Enumerate the sale-date dropdown — each option value is a salesdate id the
    # download endpoint is keyed by, so the adapter iterates them.
    sd_select = (soup.find("select", id="SelectedSaleDateId")
                 or soup.find("select", attrs={"name": "SelectedSaleDateId"}))
    sale_dates: list[tuple[str, str]] = []
    if sd_select:
        for o in sd_select.find_all("option"):
            v = (o.get("value") or "").strip()
            if v:
                sale_dates.append((v, o.get_text(" ", strip=True)))
        print(f"    sale-date options ({len(sale_dates)}): {sale_dates[:8]}")
    sd = soup.find(attrs={"name": "SelectedSaleDateId"}) or soup.find(id="SelectedSaleDateId")
    default_sd = (sd.get("value") if sd else None) or (sale_dates[0][0] if sale_dates else None)

    # propertyListDownload() navigates to
    #   /OkaloosaFLTax/listings/propertylistdownload?salesdate=<SelectedSaleDateId>
    # Fetch it WITH the salesdate and describe the parcel structure so the parser
    # can be written against the real export.
    dl = f"{url.rstrip('/')}/propertylistdownload"
    if default_sd:
        try:
            cr = session.get(dl, params={"salesdate": default_sd}, timeout=45)
        except requests.RequestException as exc:
            print(f"    GET {dl}?salesdate={default_sd} -> FAILED {exc.__class__.__name__}")
            return
        ct = cr.headers.get("content-type", "")
        disp = cr.headers.get("content-disposition", "")
        print(f"    GET {dl}?salesdate={default_sd} -> {cr.status_code}  {ct}  {len(cr.content)} bytes  disp={disp!r}")
        (out / f"{slug}_propertylist.html").write_bytes(cr.content)
        head = cr.content[:5]
        if head == b"%PDF-":
            print("      -> PDF payload"); _describe_pdf(cr.content); return
        if head[:2] == b"PK":
            print("      -> XLSX/zip payload"); return
        if "html" not in ct.lower() and "," in cr.text[:200]:
            print(f"      -> CSV/text payload; first 1500 chars:\n{cr.text[:1500]}"); return
        dsoup = BeautifulSoup(cr.text, "lxml")
        for tag in dsoup(["script", "style"]):
            tag.decompose()
        tables = dsoup.find_all("table")
        print(f"      -> HTML; tables={len(tables)}")
        for i, t in enumerate(tables[:3]):
            rows = t.find_all("tr")
            if not rows:
                continue
            hdr = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
            bdy = [c.get_text(" ", strip=True) for c in rows[1].find_all(["th", "td"])] if len(rows) > 1 else []
            print(f"        table[{i}]: {len(rows)} rows header={hdr[:10]}")
            print(f"                   first row={bdy[:10]}")
        # No table? Find the repeated block that holds each parcel. Look for the
        # class whose elements most often contain a parcel-like identifier.
        if not tables:
            from collections import Counter
            cls_counter: Counter = Counter()
            for el in dsoup.find_all(True, class_=True):
                txt = el.get_text(" ", strip=True)
                if IDENT_RE.search(txt) and 10 < len(txt) < 400:
                    cls_counter[" ".join(el.get("class"))] += 1
            print(f"      repeated parcel-ish classes: {cls_counter.most_common(8)}")
            dates = DATE_RE.findall(dsoup.get_text(" ", strip=True))
            print(f"      date tokens: {len(dates)} sample={dates[:6]}")
            print("      --- first 4000 chars of visible text ---")
            print("      " + dsoup.get_text(" ", strip=True)[:4000].replace("\n", " "))
            print("      --- first 2500 chars of raw HTML body ---")
            body = dsoup.find("body") or dsoup
            print("      " + str(body)[:2500].replace("\n", "\n      "))


def _probe_taxsmart(session: requests.Session, url: str, out: Path, slug: str) -> None:
    """Submit TaxSmart's Sale Date search and dump the result rows. The two
    SearchSaleDate<From|To> <select>s list the actual scheduled sale dates, so
    the full-range POST returns every upcoming parcel."""
    from urllib.parse import urljoin
    print("  [deep] TaxSmart sale-date search")
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException as exc:
        print(f"    fetch failed: {exc}")
        return
    soup = BeautifulSoup(r.text, "lxml")
    form = soup.find("form")
    if not form:
        print("    no form on page")
        return
    action = urljoin(r.url, form.get("action") or "")

    def opts(sel_id):
        sel = soup.find("select", id=sel_id)
        return [o.get("value", "") for o in sel.find_all("option")] if sel else []
    frm, to = opts("SearchSaleDateFrom"), opts("SearchSaleDateTo")
    print(f"    sale-date options: from={len(frm)} to={len(to)}; sample={frm[:3]}")
    if not frm:
        print("    no sale-date options — nothing scheduled, or a different field name")
        return
    data = {"SearchSaleDateFrom": frm[0], "SearchSaleDateTo": (to or frm)[-1],
            "buttonSubmitSaleDate": "Search"}
    try:
        pr = session.post(action, data=data, timeout=45)
    except requests.RequestException as exc:
        print(f"    POST failed: {exc}")
        return
    print(f"    POST {action} -> {pr.status_code}, {len(pr.content)} bytes")
    (out / f"{slug}_taxsmart_result.html").write_bytes(pr.content)
    rs = BeautifulSoup(pr.text, "lxml")
    tables = rs.find_all("table")
    print(f"    result tables: {len(tables)}")
    for i, t in enumerate(tables[:4]):
        rows = t.find_all("tr")
        if len(rows) < 2:
            continue
        head = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
        body = [c.get_text(" ", strip=True) for c in rows[1].find_all(["th", "td"])]
        print(f"      table[{i}]: {len(rows)} rows header={head[:9]}")
        print(f"                 first row={body[:9]}")
    details = [a["href"] for a in rs.find_all("a", href=True) if "/Home/Details" in a["href"]]
    print(f"    /Home/Details links: {len(details)} sample={details[:4]}")
    dates = DATE_RE.findall(rs.get_text(" ", strip=True))
    print(f"    date tokens in result: {len(dates)} sample={dates[:6]}")


def _probe_docaccess(session: requests.Session, domain: str, out: Path, slug: str) -> None:
    """Fetch the docaccess.com JSON the kmatailwind clerk template renders from.
    Hosted off the clerk's own domain, so a WAF on the clerk site (Columbia)
    doesn't necessarily block it."""
    url = f"https://docaccess.com/domains/{domain}/domain.json"
    print(f"  [deep] docaccess {url}")
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException as exc:
        print(f"    fetch failed: {exc}")
        return
    print(f"    {r.status_code}  {r.headers.get('content-type')}  {len(r.content)} bytes")
    if r.status_code >= 400:
        return
    (out / f"{slug}_docaccess.json").write_bytes(r.content)
    try:
        j = r.json()
    except ValueError as exc:
        print(f"    not JSON ({exc}); first 400 chars: {r.text[:400]}")
        return
    if isinstance(j, dict):
        print(f"    top-level keys: {list(j)[:25]}")
    else:
        print(f"    top-level: {type(j).__name__} of {len(j)}")
    print(f"    sample: {json.dumps(j)[:1400]}")


def capture_sale_lists(out_dir: str | Path, delay: float = 3.0,
                       counties: list[str] | None = None,
                       render: bool = False, deep: bool = False) -> None:
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
            print(f"  requests got HTTP {resp.status_code} (likely a WAF); "
                  f"{'trying a real browser below' if render else 'a real browser may get through — re-run with --render'}.")
        else:
            ext = "pdf" if ("pdf" in ctype.lower() or resp.content[:5] == b"%PDF-") else "html"
            (out / f"{slug}_salelist.{ext}").write_bytes(resp.content)
            if ext == "pdf":
                _describe_pdf(resp.content)
            else:
                _describe_html(resp.text or "")

        # A real Chromium render — for JS listings (Bid4Assets, Laserfiche) and
        # to walk past a WAF that 403s a bare request.
        if render:
            rate.wait()
            _render_capture(resp.url if resp.status_code < 400 else url, out, slug)

        # Platform-specific deep probes: hit each platform its own way to see
        # the real sale rows before writing an adapter.
        if deep:
            if slug in TAXSMART_SLUGS:
                rate.wait()
                _probe_taxsmart(session, url, out, slug)
            if slug in DOCACCESS_DOMAINS:
                rate.wait()
                _probe_docaccess(session, DOCACCESS_DOMAINS[slug], out, slug)
            if slug in BID4ASSETS_SLUGS:
                rate.wait()
                _probe_bid4assets(session, url, out, slug)

    print(f"\n{'=' * 74}\nDONE\n{'=' * 74}")

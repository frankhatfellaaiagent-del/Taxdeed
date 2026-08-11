"""Capture live pages (HTML + screenshot) and print structure diagnostics.

    python -m scraper capture [--url https://www.volusia.realtaxdeed.com/] [--out output/debug]

This is the breakage-playbook tool: when parsers stop matching, run this, read
the diagnostics in stdout, inspect the saved HTML/screenshots, then update the
parsers in scraper/parsing.py (and refresh fixtures if the new markup is
canonical).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .parsing import parse_calendar_dates, parse_county_selector
from .sources import LiveSource, RateLimiter

log = logging.getLogger(__name__)


def _diagnose(html: str, name: str):
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else "(no title)"
    print(f"\n########## {name}: title={title!r} len={len(html)}")
    for i, sel in enumerate(soup.find_all("select")):
        opts = sel.find_all("option")
        groups = sel.find_all("optgroup")
        print(f"  select[{i}] id={sel.get('id')!r} name={sel.get('name')!r} options={len(opts)} optgroups={len(groups)}")
        for g in groups:
            g_opts = g.find_all("option")
            sample = "; ".join(o.get_text(' ', strip=True)[:30] for o in g_opts[:4])
            print(f"    optgroup label={g.get('label')!r} options={len(g_opts)}  e.g. {sample}")
        if not groups:
            for o in opts[:8]:
                print(f"    option value={o.get('value')!r} text={o.get_text(' ', strip=True)[:60]!r}")
            if len(opts) > 8:
                print(f"    ... {len(opts) - 8} more options")
    interesting = re.compile(r"cgi|calendar|auction|tax|deed|sale", re.I)
    anchors = [(a.get_text(" ", strip=True)[:40], a["href"][:90]) for a in soup.find_all("a", href=True)]
    keep = [t for t in anchors if interesting.search(t[0]) or interesting.search(t[1])] or anchors[:20]
    print(f"  anchors ({len(anchors)} total, showing {len(keep[:25])}):")
    for text, href in keep[:25]:
        print(f"    a text={text!r} href={href!r}")
    for fr in soup.find_all(["frame", "iframe"]):
        print(f"  {fr.name} src={fr.get('src')!r} id={fr.get('id')!r}")
    for s in soup.find_all("script", src=True)[:15]:
        print(f"  script src={s['src']!r}")
    markers = {
        "dayid attr": len(soup.find_all(attrs={"dayid": True})),
        "CALBOX-ish class": len(soup.select("[class*=CAL]")),
        "AUCTION_ITEM": len(soup.select("div.AUCTION_ITEM")),
        "AUCTION-ish class": len(soup.select("[class*=AUCTION]")),
        "'tax deed' text": len(re.findall(r"tax\s*deed", html, re.I)),
        "'foreclos' text": len(re.findall(r"foreclos", html, re.I)),
        "AUCTIONDATE= in html": len(re.findall(r"AUCTIONDATE=", html, re.I)),
        "realtaxdeed hosts in html": len(set(re.findall(r"[\w.-]+\.realtaxdeed\.com", html, re.I))),
    }
    print("  markers:", markers)
    body = soup.body
    if body:
        kids = [f"{c.name}#{c.get('id','')}.{'.'.join(c.get('class', []))}" for c in body.find_all(recursive=False)][:12]
        print("  top-level body children:", kids)


def capture(url: str, out_dir: str | Path):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    src = LiveSource(rate=RateLimiter(base_delay=2.0))
    try:
        # 1. Home page
        html = src.home_html("capture", url)
        (out / "home.html").write_text(html, encoding="utf-8")
        src._page.screenshot(path=str(out / "home.png"), full_page=True)
        _diagnose(html, "HOME " + url)
        sel_entries = parse_county_selector(html, base_url=url)
        print(f"  parse_county_selector -> {len(sel_entries)} entries; first 10:")
        for e in sel_entries[:10]:
            print(f"    {e['kind']:<11} {e['state']:<3} {e['host']}  label={e['label'][:40]!r}")

        # 1b. Drive the splash-page jump menu like a human: pick this county's
        # "<County> Taxdeed" entry and follow wherever the site's JS goes.
        # This reveals the real app URL when option values are opaque indexes.
        county = urlparse(url).netloc.replace("www.", "").split(".")[0]
        try:
            page = src._page
            opts = page.eval_on_selector_all(
                "select option",
                "els => els.map(e => ({v: e.value, t: (e.textContent || '').trim()}))")
            target = next((o for o in opts
                           if county in o["t"].lower() and "tax" in o["t"].lower()), None)
            print(f"\n  jump-menu: {len(opts)} options; target for {county!r}: {target}")
            if target:
                sel_handle = page.locator(f"select:has(option[value='{target['v']}'])").first
                new_page = None
                try:
                    with page.context.expect_page(timeout=8000) as pinfo:
                        sel_handle.select_option(value=target["v"])
                    new_page = pinfo.value
                except Exception:
                    # No new tab — maybe same-tab navigation.
                    try:
                        page.wait_for_load_state("load", timeout=8000)
                    except Exception:
                        pass
                app = new_page or page
                try:
                    app.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                print(f"  after jump-menu selection: URL={app.url}")
                html = app.content()
                (out / "county_app.html").write_text(html, encoding="utf-8")
                app.screenshot(path=str(out / "county_app.png"), full_page=True)
                _diagnose(html, "COUNTY APP " + app.url)
                if new_page is not None:
                    src._page = new_page  # continue capture in the app context
        except Exception as e:
            print(f"  jump-menu drive failed: {e.__class__.__name__}: {e}")

        # 2. Calendar page: prefer clicking the app's own calendar link (the
        # guessed CGI URL 404s on the new skin), fall back to the classic URL.
        clicked = False
        try:
            link = src._page.locator("a", has_text=re.compile("calendar|tax deed sale", re.I)).first
            if link.count() > 0:
                print(f"\n  clicking calendar-ish link: {link.get_attribute('href')!r}")
                link.click(timeout=8000)
                src._page.wait_for_load_state("networkidle", timeout=10000)
                clicked = True
        except Exception as e:
            print(f"  calendar link click failed: {e.__class__.__name__}")
        if clicked:
            cal_url = src._page.url
        else:
            cal_url = src._app_url(url, "zaction=USER&zmethod=CALENDAR")
            src._goto(cal_url, wait_selector="[dayid], .CALBOX, .CALDAYBOX")
        src._dismiss_modals()
        html = src._page.content()
        (out / "calendar.html").write_text(html, encoding="utf-8")
        src._page.screenshot(path=str(out / "calendar.png"), full_page=True)
        _diagnose(html, "CALENDAR " + cal_url)
        dates = parse_calendar_dates(html)
        print(f"  parse_calendar_dates -> {dates}")

        # 3. First auction page, if we found a date
        if dates:
            a_url = urljoin(url, f"/index.cgi?zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE={dates[0]['date']}")
            html = src._goto(a_url, wait_selector="div.AUCTION_ITEM")
            (out / "auction.html").write_text(html, encoding="utf-8")
            src._page.screenshot(path=str(out / "auction.png"), full_page=True)
            _diagnose(html, "AUCTION " + a_url)
    finally:
        src.close()
    print(f"\nSaved captures to {out}/ (home/calendar[/auction] .html + .png)")

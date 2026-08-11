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
from urllib.parse import urljoin

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
        print(f"  select[{i}] id={sel.get('id')!r} name={sel.get('name')!r} options={len(opts)}")
        for o in opts[:8]:
            print(f"    option value={o.get('value')!r} text={o.get_text(' ', strip=True)[:60]!r}")
        if len(opts) > 8:
            print(f"    ... {len(opts) - 8} more options")
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

        # 2. Calendar page
        cal_url = urljoin(url, "/index.cgi?zaction=USER&zmethod=CALENDAR")
        html = src._goto(cal_url, wait_selector="[dayid], .CALBOX, .CALDAYBOX")
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

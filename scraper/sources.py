"""Page sources: live (Playwright) and fixture (saved HTML for tests/demos).

The scraper only ever talks to a Source, so the whole pipeline runs unchanged
against saved HTML — that is how the offline demo and regression tests work.
"""

from __future__ import annotations

import datetime
import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)


class RateLimiter:
    """Conservative politeness delay between page loads (base + jitter)."""

    def __init__(self, base_delay: float = 3.0, jitter: float = 1.5):
        self.base_delay = base_delay
        self.jitter = jitter
        self._last = 0.0

    def wait(self):
        elapsed = time.monotonic() - self._last
        target = self.base_delay + random.uniform(0, self.jitter)
        if elapsed < target:
            time.sleep(target - elapsed)
        self._last = time.monotonic()


class FixtureSource:
    """Reads saved HTML from fixtures/<county>/ instead of the network.

    Files: home.html, calendar.html (or calendar_MM_YYYY.html),
    auction_MMDDYYYY.html (pagination: auction_MMDDYYYY_p2.html ...).
    """

    is_live = False

    def __init__(self, fixtures_dir: str | Path):
        self.root = Path(fixtures_dir)

    def _county_dir(self, county_slug: str) -> Path:
        return self.root / county_slug

    def _read(self, path: Path) -> str | None:
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def home_html(self, county_slug: str, base_url: str) -> str | None:
        return self._read(self._county_dir(county_slug) / "home.html")

    def calendar_pages(self, county_slug: str, base_url: str, months: int) -> list[str]:
        d = self._county_dir(county_slug)
        pages = sorted(d.glob("calendar*.html"))
        return [p.read_text(encoding="utf-8") for p in pages]

    def auction_pages(self, county_slug: str, base_url: str, date_mmddyyyy: str) -> list[str]:
        d = self._county_dir(county_slug)
        stem = "auction_" + date_mmddyyyy.replace("/", "")
        pages = sorted(d.glob(stem + "*.html"))
        return [p.read_text(encoding="utf-8") for p in pages]

    def close(self):
        pass


class LiveSource:
    """Fetches rendered pages with Playwright (RealAuction pages fill auction
    details in with JavaScript, so plain requests is not reliable for them)."""

    is_live = True

    def __init__(self, rate: RateLimiter | None = None, headless: bool = True, nav_timeout_ms: int = 45000):
        self.rate = rate or RateLimiter()
        self._pw = None
        self._browser = None
        self._page = None
        self._headless = headless
        self._nav_timeout_ms = nav_timeout_ms
        self._app_ext: dict[str, str] = {}  # host root -> "cfm" | "cgi"
        # Some county certs only cover the bare host, others only www.
        # (e.g. marion/alachua fail on www.). Learned per host on first error.
        self._host_swap: dict[str, str] = {}

    def _ensure(self):
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        ctx = self._browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36 MADDAssetsTaxDeedResearch/0.1"
        )
        self._page = ctx.new_page()
        self._page.set_default_navigation_timeout(self._nav_timeout_ms)

    @staticmethod
    def _toggle_www(host: str) -> str:
        return host[4:] if host.startswith("www.") else "www." + host

    def _goto(self, url: str, wait_selector: str | None = None) -> str:
        self._ensure()
        host = urlparse(url).netloc
        if host in self._host_swap:
            url = url.replace(host, self._host_swap[host], 1)
        self.rate.wait()
        log.debug("GET %s", url)
        try:
            self._page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            if "ERR_CERT" not in str(e):
                raise
            alt_host = self._toggle_www(urlparse(url).netloc)
            alt_url = url.replace(urlparse(url).netloc, alt_host, 1)
            log.warning("TLS cert mismatch on %s — retrying as %s", url, alt_url)
            self.rate.wait()
            self._page.goto(alt_url, wait_until="domcontentloaded")
            self._host_swap[host] = alt_host
        if wait_selector:
            try:
                self._page.wait_for_selector(wait_selector, timeout=15000)
            except Exception:
                log.warning("Selector %r never appeared on %s (page structure may have changed)", wait_selector, url)
        # Give late AJAX fills a moment to settle.
        try:
            self._page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        return self._page.content()

    def home_html(self, county_slug: str, base_url: str) -> str | None:
        html = self._goto(base_url, wait_selector="select")
        self._dismiss_modals()
        return self._page.content()

    def _dismiss_modals(self):
        """Best-effort click-through of announcement/terms popups some county
        skins show before any page content is usable."""
        for sel in ["button:has-text('Accept')", "button:has-text('I Agree')",
                    "button:has-text('Agree')", "button:has-text('OK')",
                    "button:has-text('Continue')", "input[value*='Accept' i]",
                    "a:has-text('Accept')", ".ui-dialog button"]:
            try:
                el = self._page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(timeout=3000)
                    log.info("Dismissed a popup via %r", sel)
                    self._page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    def _looks_like_404(self) -> bool:
        try:
            title = self._page.title() or ""
        except Exception:
            return False
        return "404" in title or "not found" in title.lower()

    def _app_url(self, base_url: str, query: str) -> str:
        """Newer county sites serve the app at /index.cfm, classic ones at
        /index.cgi. Resolve once per host and remember."""
        host = urljoin(base_url, "/")
        ext = self._app_ext.get(host)
        if ext:
            return urljoin(base_url, f"/index.{ext}?{query}")
        for candidate in ("cfm", "cgi"):
            url = urljoin(base_url, f"/index.{candidate}?{query}")
            self._goto(url)
            if not self._looks_like_404():
                self._app_ext[host] = candidate
                log.info("App entry point for %s: index.%s", host, candidate)
                return url
        log.warning("Both index.cfm and index.cgi 404 on %s", host)
        return urljoin(base_url, f"/index.cfm?{query}")

    def calendar_pages(self, county_slug: str, base_url: str, months: int) -> list[str]:
        """Current month plus `months` following months, via the next-month arrow."""
        # Land on the home page first: it establishes any session cookies and
        # is where announcement popups appear.
        try:
            self._goto(base_url)
            self._dismiss_modals()
        except Exception as e:
            log.warning("[%s] could not preload home page: %s", county_slug, e)
        url = self._app_url(base_url, "zaction=USER&zmethod=CALENDAR")
        if self._page.url.rstrip("/") != url.rstrip("/"):
            self._goto(url, wait_selector="[dayid], .CALBOX, .CALDAYBOX")
        else:
            try:
                self._page.wait_for_selector("[dayid], .CALBOX, .CALDAYBOX", timeout=15000)
            except Exception:
                log.warning("Calendar day cells never appeared on %s", url)
        self._dismiss_modals()
        pages = [self._page.content()]

        # Month stepping: the calendar is server-rendered, so request each
        # following month directly with selCalDate (deterministic), and only
        # fall back to clicking the CALNAV arrow if the month didn't change.
        from .parsing import parse_calendar_month_label
        prev_label = parse_calendar_month_label(pages[0])
        today = datetime.date.today().replace(day=1)
        for i in range(1, months + 1):
            month = today.month - 1 + i
            target = today.replace(year=today.year + month // 12, month=month % 12 + 1)
            month_url = self._app_url(
                base_url, f"zaction=USER&zmethod=CALENDAR&selCalDate={target.strftime('%m/%d/%Y')}")
            html = self._goto(month_url, wait_selector="[dayid], .CALBOX, .CALDAYBOX")
            label = parse_calendar_month_label(html)
            if label and label == prev_label:
                log.warning("selCalDate did not advance the month (%s); trying the CALNAV arrow", label)
                if not self._click_next_month():
                    break
                try:
                    self._page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                html = self._page.content()
                label = parse_calendar_month_label(html)
                if label == prev_label:
                    break
            prev_label = label or prev_label
            pages.append(html)
        return pages

    def _click_next_month(self) -> bool:
        # RealAuction has used several skins for the calendar nav arrow.
        candidates = [
            "a.CALNAV >> nth=-1", ".CALNAV >> nth=-1",
            "#CALNAV_RIGHT", ".CALNAVR", ".CalNavRight",
            "a[title*='Next' i]", "img[alt*='Next' i]",
            "text=/next month/i",
        ]
        for sel in candidates:
            try:
                el = self._page.locator(sel).first
                if el.count() > 0:
                    self.rate.wait()
                    el.click(timeout=5000)
                    return True
            except Exception:
                continue
        log.warning("Could not find next-month control on calendar page")
        return False

    def auction_pages(self, county_slug: str, base_url: str, date_mmddyyyy: str) -> list[str]:
        url = self._app_url(base_url, f"zaction=AUCTION&Zmethod=PREVIEW&AUCTIONDATE={date_mmddyyyy}")
        self.last_auction_url = url
        pages = [self._goto(url, wait_selector="div.AUCTION_ITEM")]
        # Paginate. The page can hold MULTIPLE paginated lists (e.g. an upper
        # and lower area, each with its own "page ▸ of N" pager and ~10 items
        # per page). Advance every right-arrow each round — an area already on
        # its last page is a no-op — and stop when content stops changing.
        # Downstream dedupe makes any overlap harmless.
        max_pages = self._read_max_pages()
        prev = pages[0]
        for _ in range(max_pages - 1 if max_pages else 0):
            if not self._click_next_page():
                break
            try:
                self._page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            self._page.wait_for_timeout(600)  # let AJAX-swapped items settle
            html = self._page.content()
            if html == prev:
                break
            pages.append(html)
            prev = html
        return pages

    def _read_max_pages(self) -> int:
        """Max page count across all pager areas on the page."""
        counts = [1]
        try:
            for sel in ("input#maxCA", "input[name='maxCA']", "input#maxWA", "input[name='maxWA']"):
                loc = self._page.locator(sel)
                for i in range(min(loc.count(), 4)):
                    val = loc.nth(i).input_value(timeout=1000)
                    if val and val.strip().isdigit():
                        counts.append(int(val.strip()))
        except Exception:
            pass
        try:
            text = self._page.locator("body").inner_text(timeout=2000)
            # Current page number often lives in an <input>, so the text reads
            # "page of 3" (any case) — match with or without the number.
            for m in re.finditer(r"page\s*\d*\s*of\s*(\d+)", text, re.I):
                counts.append(int(m.group(1)))
        except Exception:
            pass
        return max(counts)

    def _click_next_page(self) -> bool:
        """Click every next-page arrow on the page (each list area has its
        own); returns True if at least one was clicked."""
        clicked = 0
        try:
            arrows = self._page.locator(".PageRight")
            n = arrows.count()
            for i in range(min(n, 6)):
                el = arrows.nth(i)
                try:
                    if el.is_visible():
                        if clicked == 0:
                            self.rate.wait()
                        el.click(timeout=3000)
                        clicked += 1
                        self._page.wait_for_timeout(250)
                except Exception:
                    continue
        except Exception:
            pass
        if clicked:
            return True
        for sel in ("img[alt*='Next' i]", "a[title*='Next' i]"):
            try:
                el = self._page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    self.rate.wait()
                    el.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    def close(self):
        for closer in (self._browser, self._pw):
            try:
                if closer is not None:
                    closer.stop() if hasattr(closer, "stop") else closer.close()
            except Exception:
                pass
        self._page = self._browser = self._pw = None

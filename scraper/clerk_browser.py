"""Browser-driven clerk portals — currently NewVision (Marion County).

Marion's tax deed portal (nvweb.marioncountyclerk.org/browserviewtd/) is a
stateful ASP.NET postback app: searching does not change the URL, so there is
nothing to deep-link. To get a parcel's case file we have to drive it — search
by parcel or tax number, open the first hit, and read the Document tab that the
client showed in their screenshot (Tax Number, Parcel #, Deed Status, Appl.
Name/Address, and the dated document list: All Forms, Tax Deed, Notice of
Publication, Clerk's Affidavit…).

Kept apart from scraper/clerk.py so the HTTP resolvers never depend on a
browser being installed. Playwright is already a project dependency and is
installed in CI.
"""

from __future__ import annotations

import logging
import re

from .clerk import parse_case_page

log = logging.getLogger(__name__)

# Search inputs on these portals are labeled by what they hold; try the most
# specific identifier first so we land on one case instead of a list.
SEARCH_FIELDS = [
    ("tax_number", ["tax number", "taxnumber", "tax no", "taxno", "tax deed"]),
    ("parcel_id", ["parcel", "parcelno", "parcel #", "parcel number"]),
]


class NewVisionResolver:
    """Resolve records through a NewVision SearchNG/BrowserView portal."""

    def __init__(self, page, timeout_ms: int = 20000):
        self.page = page
        self.timeout = timeout_ms
        self._loaded: str | None = None

    def _open_portal(self, portal: str) -> bool:
        if self._loaded == portal:
            return True
        try:
            self.page.goto(portal, wait_until="domcontentloaded", timeout=self.timeout)
            self._loaded = portal
            return True
        except Exception as exc:                       # noqa: BLE001 - portal may be down
            log.debug("newvision portal unreachable %s: %s", portal, exc)
            return False

    def _find_input(self, keywords: list[str]):
        """An <input> whose name/id/placeholder or neighboring label matches."""
        for kw in keywords:
            for sel in (f'input[name*="{kw}" i]', f'input[id*="{kw}" i]',
                        f'input[placeholder*="{kw}" i]'):
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        return loc
                except Exception:                      # noqa: BLE001
                    continue
            # Label text sitting next to the field.
            try:
                loc = self.page.get_by_label(re.compile(kw, re.I)).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:                          # noqa: BLE001
                continue
        return None

    def _submit(self) -> None:
        for sel in ('input[type="submit"]', 'button[type="submit"]',
                    'input[value*="Search" i]', 'button:has-text("Search")',
                    'a:has-text("Search")'):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click()
                    return
            except Exception:                          # noqa: BLE001
                continue
        self.page.keyboard.press("Enter")

    def resolve(self, rec: dict, cfg: dict) -> dict:
        portal = (cfg.get("portal") or cfg.get("search") or cfg.get("url") or "").strip()
        if not portal or not self._open_portal(portal):
            return {}

        for field, keywords in SEARCH_FIELDS:
            value = str(rec.get(field) or rec.get("case_number") or "").strip()
            if not value:
                continue
            box = self._find_input(keywords)
            if box is None:
                continue
            try:
                box.fill("")
                box.fill(value)
                self._submit()
                self.page.wait_for_load_state("networkidle", timeout=self.timeout)
            except Exception as exc:                   # noqa: BLE001
                log.debug("newvision search failed (%s=%s): %s", field, value, exc)
                self._loaded = None                    # force a clean reload next time
                continue

            # A results grid appears before the document view; open the first row.
            try:
                row = self.page.locator(
                    'table tr:has(a), tr[onclick], a:has-text("View")').first
                if row.count():
                    row.click(timeout=5000)
                    self.page.wait_for_load_state("networkidle", timeout=self.timeout)
            except Exception:                          # noqa: BLE001
                pass

            html = self.page.content()
            parsed = parse_case_page(html, self.page.url)
            # Confirm we actually landed on this parcel's record before trusting it.
            hay = re.sub(r"[^A-Za-z0-9]", "", html).upper()
            if re.sub(r"[^A-Za-z0-9]", "", value).upper() in hay and (
                    parsed.get("deed_status") or parsed.get("case_docs")):
                parsed["clerk_case_url"] = self.page.url
                parsed["clerk_platform"] = "newvision"
                parsed["clerk_search_value"] = value
                self._loaded = None                    # reset for the next record
                return parsed
            self._loaded = None
        return {}

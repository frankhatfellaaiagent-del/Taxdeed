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
    # Marion's own field is labeled "Tax Value" (id="txtTaxValue"), not "tax
    # number" — the certificate/tax-deed number is still what goes in it.
    ("tax_number", ["tax number", "taxnumber", "tax no", "taxno", "tax deed", "tax value"]),
    ("parcel_id", ["parcel", "parcelno", "parcel #", "parcel number"]),
]

# Marion's portal (and likely other NewVision installs) renders one row per
# identifier (Name/Tax Number/Parcel Number/...) inside a single form — every
# row's Search/Clear button pair is in the DOM at once, but a row's own input
# stays hidden until its label is clicked. _find_input never finds Tax
# Number/Parcel Number without that click first. The label is a plain <a>
# naming the category; clicking it is a harmless no-op on a portal that
# doesn't use this pattern (nothing matches, so the click is skipped).
TAB_LABELS = {"tax_number": "Tax Number", "parcel_id": "Parcel Number"}


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

    def _select_search_tab(self, field: str) -> None:
        """Click the category tab that reveals this field's input, if the
        portal uses that pattern (Marion's NewVision does)."""
        label = TAB_LABELS.get(field)
        if not label:
            return
        try:
            tab = self.page.locator("a", has_text=label).first
            if tab.count() and tab.is_visible():
                tab.click(timeout=5000)
                self.page.wait_for_timeout(300)
        except Exception:                              # noqa: BLE001
            pass

    def _submit_near(self, box) -> bool:
        """Click the Search button in the SAME row as this input.

        Marion's portal isn't tabbed — one form holds a separate row per
        identifier (Name/Tax Number/Parcel Number/...), each with its own
        Search/Clear button pair, all present in the DOM at once. The global
        'first Search button' belongs to the always-visible Name row, not
        whichever row's label was just clicked, so scope to the input's own
        row container first. Returns True if it found and clicked one."""
        try:
            row = box.locator("xpath=ancestor::div[contains(@class,'row-padding')][1]")
            btn = row.locator('button:has-text("Search")').first
            if btn.count() and btn.is_visible():
                btn.click()
                return True
        except Exception:                              # noqa: BLE001
            pass
        return False

    def _submit(self, box=None) -> None:
        if box is not None and self._submit_near(box):
            return
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
            self._select_search_tab(field)
            box = self._find_input(keywords)
            if box is None:
                continue
            try:
                box.fill("")
                box.fill(value)
                self._submit(box)
                self.page.wait_for_load_state("networkidle", timeout=self.timeout)
            except Exception as exc:                   # noqa: BLE001
                log.debug("newvision search failed (%s=%s): %s", field, value, exc)
                self._loaded = None                    # force a clean reload next time
                continue
            log.debug("newvision search submitted (%s=%s), now at %s", field, value, self.page.url)

            # A results grid appears before the document view; open the first row.
            # It can render below the fold (Marion's grid does), so Playwright's
            # actionability check sees it as present but not visible until
            # scrolled into view.
            try:
                row = self.page.locator(
                    'table tr:has(a), tr[onclick], a:has-text("View")').first
                if row.count():
                    row.scroll_into_view_if_needed(timeout=5000)
                    row.click(timeout=5000)
                    self.page.wait_for_load_state("networkidle", timeout=self.timeout)
                    log.debug("newvision result row clicked, now at %s", self.page.url)
                else:
                    log.debug("newvision no result row found after search (%s=%s)", field, value)
            except Exception as exc:                   # noqa: BLE001
                log.debug("newvision result row click failed (%s=%s): %s", field, value, exc)

            html = self.page.content()
            parsed = parse_case_page(html, self.page.url)
            # Confirm we actually landed on this parcel's record before trusting it.
            hay = re.sub(r"[^A-Za-z0-9]", "", html).upper()
            value_present = re.sub(r"[^A-Za-z0-9]", "", value).upper() in hay
            if value_present and (parsed.get("deed_status") or parsed.get("case_docs")):
                parsed["clerk_case_url"] = self.page.url
                parsed["clerk_platform"] = "newvision"
                parsed["clerk_search_value"] = value
                self._loaded = None                    # reset for the next record
                return parsed
            log.debug("newvision unresolved (%s=%s): value_on_page=%s parsed_keys=%s",
                      field, value, value_present, list(parsed.keys()))
            self._loaded = None
        return {}

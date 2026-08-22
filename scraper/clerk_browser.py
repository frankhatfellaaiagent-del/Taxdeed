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

import re

from .clerk import parse_case_page

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
            print(f"[newvision] portal unreachable {portal}: {exc}", flush=True)
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

        print(f"[newvision] portal ready: {portal}", flush=True)
        for field, keywords in SEARCH_FIELDS:
            value = str(rec.get(field) or rec.get("case_number") or "").strip()
            if not value:
                print(f"[newvision] field={field}: record has no value, skipped", flush=True)
                continue
            self._select_search_tab(field)
            box = self._find_input(keywords)
            if box is None:
                print(f"[newvision] field={field} value={value}: no fillable input found", flush=True)
                continue
            try:
                box.fill("")
                box.fill(value)
                self._submit(box)
                self.page.wait_for_load_state("networkidle", timeout=self.timeout)
            except Exception as exc:                   # noqa: BLE001
                print(f"[newvision] search failed ({field}={value}): {exc.__class__.__name__}: {exc}", flush=True)
                self._loaded = None                    # force a clean reload next time
                continue
            print(f"[newvision] search submitted ({field}={value}), now at {self.page.url}", flush=True)

            # A results grid appears before the document view; open the row that
            # holds this search's value. Marion's grid is an ag-Grid virtual
            # table (div.ag-row / div.ag-cell), not an HTML <table> — a plain
            # table/tr selector can silently match an unrelated element
            # elsewhere in this single-page app instead of a real result, so
            # look for the searched value itself first and only fall back to a
            # generic clickable-row selector for portals that use a real table.
            row_status = None
            try:
                cell = self.page.locator(".ag-cell", has_text=value).first
                if cell.count():
                    row = cell.locator("xpath=ancestor::div[contains(@class,'ag-row')][1]")
                else:
                    row = self.page.locator(
                        'table tr:has(a), tr[onclick], a:has-text("View")').first
                if row.count():
                    row.scroll_into_view_if_needed(timeout=5000)
                    # The grid row itself already carries the case's outcome
                    # (e.g. "SOLD"/"REDEEM") in one of its plain cells — a
                    # reliable status signal that needs no click at all.
                    try:
                        cell_texts = row.evaluate(
                            "el => [...el.querySelectorAll('.ag-cell')].map(c => c.textContent.trim())")
                        row_status = next(
                            (t for t in cell_texts
                             if t.upper() in ("SOLD", "REDEEM", "REDEEMED", "CANCELLED", "CANCELED")),
                            None)
                    except Exception:                       # noqa: BLE001
                        pass
                    # Clicking the row itself never opens the case detail
                    # (confirmed on 5/5 sample records with both single- and
                    # double-click). The row's own "View" button
                    # (ng-click="fetchDocument(id, 1)") is the real trigger.
                    view_btn = row.locator('button:has-text("View")').first
                    if view_btn.count():
                        view_btn.click(timeout=5000)
                    else:
                        row.click(timeout=5000)
                    self.page.wait_for_load_state("networkidle", timeout=self.timeout)
                    print(f"[newvision] row_status={row_status!r}, View button clicked, "
                          f"now at {self.page.url}", flush=True)
                else:
                    print(f"[newvision] no result row found after search ({field}={value})", flush=True)
            except Exception as exc:                   # noqa: BLE001
                print(f"[newvision] result row click failed ({field}={value}): "
                      f"{exc.__class__.__name__}: {exc}", flush=True)

            # The click triggers an async fetchDocument() call; give Angular a
            # moment to fill the detail panel's ng-binding cells afterward,
            # since networkidle alone doesn't guarantee the digest cycle has
            # finished.
            self.page.wait_for_timeout(800)

            html = self.page.content()
            parsed = parse_case_page(html, self.page.url)
            if not parsed.get("deed_status") and row_status:
                parsed["deed_status"] = row_status
            # Confirm we actually landed on this parcel's record before trusting it.
            hay = re.sub(r"[^A-Za-z0-9]", "", html).upper()
            value_present = re.sub(r"[^A-Za-z0-9]", "", value).upper() in hay
            print(f"[newvision] page content: {len(html)} bytes, value_on_page={value_present}, "
                  f"parsed_keys={list(parsed.keys())}", flush=True)
            if value_present and (parsed.get("deed_status") or parsed.get("case_docs")):
                parsed["clerk_case_url"] = self.page.url
                parsed["clerk_platform"] = "newvision"
                parsed["clerk_search_value"] = value
                self._loaded = None                    # reset for the next record
                return parsed
            print(f"[newvision] unresolved ({field}={value}): value_on_page={value_present} "
                  f"parsed_keys={list(parsed.keys())}", flush=True)
            self._loaded = None
        return {}

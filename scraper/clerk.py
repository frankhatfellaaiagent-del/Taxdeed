"""Resolve a parcel to its own Clerk of Court tax deed case file.

The county page link (config/clerk_sites.yaml) tells you where the clerk keeps
tax deeds; it does not tell you anything about *your* parcel. This module goes
the last mile: given an auction record, find that parcel's case record and pull
what the clerk publishes about it — deed status, applicant, and the paperwork
list (Tax Deed, Notice of Publication, Clerk's Affidavit, 513 form…).

County portals cluster onto a handful of platforms, so there is a resolver per
platform rather than per county:

  realtdm    RealAuction's clerk-side module. Case list at
             <county>.realtdm.com/public/cases/list; detail at
             .../public/cases/getCase/caseid/<internal id>. The id is internal,
             so we index the list once per county per run.
  taxsmart   Pioneer Technology Group. Detail at <portal>/Home/Details?id=<internal id>,
             documents at <portal>/Home/Image/<doc id>. Same indexing approach.
  putnam     Custom PHP; the one portal with a domain-meaningful deep link —
             public_certification.php?certnum=<certificate number>.
  newvision  NewVision SearchNG/BrowserView (Marion). A stateful ASP.NET
             postback app with no linkable URLs — driven with a browser in
             scraper/clerk_browser.py, kept separate so a missing browser never
             breaks the HTTP resolvers.

Every resolver is best-effort: anything it cannot resolve leaves the record with
the county-level link it already had.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CLERK_SITES_PATH = ROOT / "config" / "clerk_sites.yaml"

# Document rows worth surfacing on the property card, in the order a buyer
# reads them. Matching is substring, case-insensitive. The ownership &
# encumbrance report (a.k.a. O&E / current owner search / property information
# report) is the single most valuable document — it lists every recorded
# mortgage, judgment, IRS lien and encumbrance — so its many aliases are here.
DOC_INTEREST = ["all forms", "tax deed", "notice of publication", "clerk",
                "affidavit", "513", "certificate", "title", "search",
                "sale", "receipt", "statement", "lien", "notice",
                "ownership", "encumbrance", "o&e", "o & e", "owner search",
                "property information", "property info", "current owner"]


def load_clerk_sites(path: str | Path | None = None) -> dict:
    p = Path(path) if path else CLERK_SITES_PATH
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.warning("clerk_sites.yaml unreadable (%s)", exc)
        return {}


def _norm_num(s: str) -> str:
    """Compare case/parcel/tax numbers without punctuation noise."""
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


_MONEY_RE = re.compile(r"^\$?\s*\d{1,3}(?:,\d{3})+(?:\.\d{2})?$|^\$\s*[\d.,]+$|^\d+\.\d{2}$")


def _is_identifier(cell: str) -> bool:
    """Only index cells that could be a case/parcel/tax number.

    Identifiers always carry digits, which rules out statuses ("SCHEDULED")
    and names; money and dates are excluded explicitly. Bare digit strings do
    stay — plenty of counties use them as parcel or tax numbers."""
    text = cell.strip()
    if not text or "/" in text or _MONEY_RE.match(text):
        return False
    key = _norm_num(text)
    return len(key) >= 5 and any(c.isdigit() for c in key)


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _doc_rows(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Links on a case page that look like case paperwork."""
    docs: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = _text(a)
        # The link text is often just "View" — the document name sits in the
        # row beside it, so read the whole row when the label is unhelpful.
        row = a.find_parent("tr")
        row_text = _text(row) if row else ""
        name = label if len(label) > 4 else row_text
        if not name:
            continue
        hay = (name + " " + href).lower()
        if not any(k in hay for k in DOC_INTEREST):
            continue
        if not re.search(r"(image|document|doc|pdf|view|form)", href, re.I):
            continue
        url = urljoin(page_url, href)
        if url in seen:
            continue
        seen.add(url)
        date = ""
        m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", row_text)
        if m:
            date = m.group(1)
            name = name.replace(date, "")
        # Row text starts with the link's own label ("View", "Image"); the
        # document's real name is what follows.
        name = re.sub(r"^\s*(view|image|open|download|pdf)\b[\s:|-]*", "", name, flags=re.I)
        name = re.sub(r"\s+", " ", name).strip(" -·|")
        if not name:
            continue
        docs.append({"name": name[:80], "date": date, "url": url})
    return docs[:25]


def _labeled_fields(soup: BeautifulSoup, labels: dict[str, list[str]]) -> dict:
    """Scan label/value cells (the layout every one of these portals uses)."""
    out: dict[str, str] = {}
    for cell in soup.find_all(["td", "th", "dt", "label", "strong", "b", "span"]):
        if len(cell.find_all(["td", "th", "tr"])) > 1:
            continue
        text = _text(cell).rstrip(":").lower()
        if not text or len(text) > 40:
            continue
        for field, names in labels.items():
            if field in out:
                continue
            if any(text == n or text.startswith(n) for n in names):
                for sib in cell.find_next_siblings(["td", "dd", "span", "div"]):
                    val = _text(sib)
                    if val:
                        out[field] = val[:160]
                        break
    return out


CASE_LABELS = {
    "deed_status": ["deed status", "status", "case status"],
    "applicant": ["appl. name", "appl name", "applicant name", "applicant"],
    "applicant_address": ["appl. address", "appl address", "applicant address"],
    "sale_date": ["date of sale", "sale date"],
    "tax_number": ["tax number", "tax no", "tax deed number", "tax deed no"],
}


def parse_case_page(html: str, page_url: str) -> dict:
    """Fields + document links from any of these portals' case detail pages."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    out = _labeled_fields(soup, CASE_LABELS)
    # Label scans occasionally grab the wrong cell; a value that starts like a
    # street address is not an applicant name — better no field than a wrong
    # one on the client's card.
    if re.match(r"^\d+\s", out.get("applicant", "")):
        out.pop("applicant", None)
    if len(out.get("deed_status", "")) > 40:
        out.pop("deed_status", None)
    docs = _doc_rows(soup, page_url)
    if docs:
        out["case_docs"] = docs
    return out


# ---------------------------------------------------------------- RealTDM ----

def parse_realtdm_list(html: str, base_url: str) -> dict:
    """Map every number on the public case list to its case-detail URL.

    Returns {normalized number: case url} — the same case is indexed under its
    case number, tax deed number and parcel id so any of them can find it.
    """
    soup = BeautifulSoup(html, "lxml")
    index: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"/cases/getCase/caseid/(\d+)", a["href"])
        if not m:
            continue
        url = urljoin(base_url, a["href"])
        row = a.find_parent("tr")
        cells = [_text(td) for td in row.find_all(["td", "th"])] if row else [_text(a)]
        for cell in cells:
            if not _is_identifier(cell) or "/" in cell:      # skip dates
                continue
            index.setdefault(_norm_num(cell), url)
    return index


# --------------------------------------------------------------- TaxSmart ----

def parse_taxsmart_list(html: str, base_url: str) -> dict:
    """Same idea for Pioneer TaxSmart portals (/Home/Details?id=N)."""
    soup = BeautifulSoup(html, "lxml")
    index: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        if not re.search(r"/Home/Details\?id=\d+", a["href"], re.I):
            continue
        url = urljoin(base_url, a["href"])
        row = a.find_parent("tr")
        cells = [_text(td) for td in row.find_all(["td", "th"])] if row else [_text(a)]
        for cell in cells:
            if not _is_identifier(cell) or "/" in cell:
                continue
            index.setdefault(_norm_num(cell), url)
    return index


# ------------------------------------------------------------------ driver ---

class ClerkResolver:
    """Resolves records to case files, caching each county's index per run."""

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

    def __init__(self, sites: dict | None = None, timeout: int = 25,
                 session: requests.Session | None = None):
        self.sites = sites if sites is not None else load_clerk_sites()
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = self.UA
        self._index_cache: dict[str, dict] = {}     # county -> {number: url}
        self._failed: set[str] = set()              # counties whose index failed

    # -- helpers ------------------------------------------------------------
    def _get(self, url: str) -> str | None:
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            log.debug("clerk fetch failed %s: %s", url, exc)
            return None

    def _county_cfg(self, county: str) -> dict:
        return self.sites.get(re.sub(r"[^a-z]", "", (county or "").lower()), {}) or {}

    def _realtdm_base(self, county: str, cfg: dict) -> str:
        portal = cfg.get("portal")
        if portal:
            return portal.rstrip("/")
        slug = re.sub(r"[^a-z]", "", (county or "").lower())
        return f"https://{slug}.realtdm.com"

    def _index(self, county: str, cfg: dict) -> dict:
        """Build (once) the number → case-URL index for a county."""
        if county in self._index_cache:
            return self._index_cache[county]
        if county in self._failed:
            return {}
        platform = cfg.get("platform")
        index: dict = {}
        if platform == "realtdm":
            base = self._realtdm_base(county, cfg)
            list_url = f"{base}/public/cases/list"
            html = self._get(list_url)
            if html:
                index = parse_realtdm_list(html, list_url)
        elif platform == "taxsmart":
            portal = (cfg.get("portal") or cfg.get("search") or "").rstrip("/")
            if portal:
                for path in ("/Home/Index", "", "/Home/List"):
                    html = self._get(portal + path)
                    if html:
                        found = parse_taxsmart_list(html, portal + path)
                        index.update(found)
                        if found:
                            break
        if index:
            log.info("clerk index: %s (%s) — %d numbers", county, platform, len(index))
            self._index_cache[county] = index
        else:
            self._failed.add(county)
        return index

    # -- public -------------------------------------------------------------
    def resolve(self, rec: dict) -> dict:
        """Return case-file info for one auction record ({} when unresolved)."""
        county = (rec.get("county") or "").lower()
        cfg = self._county_cfg(county)
        platform = cfg.get("platform")
        if not platform:
            return {}

        case_url = None
        if platform == "template":
            # Direct deep link (Putnam: certification by certificate number).
            # {value} is the number as published, {digits} strips punctuation —
            # portals keyed on a bare number want the latter.
            tpl = cfg.get("case_url")
            field = cfg.get("case_url_key", "certificate_number")
            value = str(rec.get(field) or "").strip()
            if tpl and value:
                case_url = (tpl.replace("{value}", value)
                               .replace("{digits}", re.sub(r"\D", "", value)))
        elif platform in ("realtdm", "taxsmart"):
            index = self._index(county, cfg)
            if index:
                for field in ("case_number", "certificate_number", "parcel_id"):
                    key = _norm_num(rec.get(field))
                    if key and key in index:
                        case_url = index[key]
                        break
        if not case_url:
            return {}

        out = {"clerk_case_url": case_url, "clerk_platform": platform}
        html = self._get(case_url)
        if html:
            out.update(parse_case_page(html, case_url))
        return out

"""HTML parsers for RealAuction tax deed pages.

All parsers are defensive: RealAuction tweaks markup occasionally, so we match
on several selector strategies and normalize label text rather than relying on
exact classes. If parsing yields nothing, callers treat it as a page-structure
change and log it loudly (the tax-deed-scrub skill picks that up).
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .models import AuctionRecord

DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
# "FL - Miami-Dade Taxdeed" / "GA - Fulton Foreclosure" style option labels.
SELECTOR_LABEL_RE = re.compile(
    r"^\s*([A-Za-z]{2})\s*[-–—]\s*(.+?)\s*(tax\s*deed|taxdeed|foreclosure)\s*$", re.I
)
HOST_RE = re.compile(r"(?:[\w-]+\.)+real(?:taxdeed|foreclose)\.com", re.I)
MONEY_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM)(?:\s*ET)?)\b", re.I)

# Normalized label -> record attribute
LABEL_MAP = {
    "auction type": "auction_type",
    "case": "case_number",
    "case number": "case_number",
    "tax deed number": "case_number",
    "certificate": "certificate_number",
    "certificate number": "certificate_number",
    "cert": "certificate_number",
    "opening bid": "opening_bid",
    "parcel id": "parcel_id",
    "parcel": "parcel_id",
    "alternate key": "parcel_id",
    "property address": "property_address",
    "assessed value": "assessed_value",
    "owner": "owner_name",
    "owner name": "owner_name",
    "property use": "property_use",
    "land use": "property_use",
    "use code": "property_use",
}

MONEY_FIELDS = {"opening_bid", "assessed_value"}


def _norm_label(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip().strip(":").strip()
    text = text.replace("#", "").strip()
    return text.lower()


def parse_money(text: str) -> float | None:
    m = MONEY_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_county_selector(html: str, base_url: str = "") -> list[dict]:
    """Extract ALL entries from the county/state selector on a RealAuction page.

    Returns dicts: {label, url, host, state, kind} where kind is
    'taxdeed' | 'foreclosure' | 'other'. Callers filter to FL taxdeed.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: list[dict] = []
    seen: set[str] = set()

    def classify(host: str, label: str) -> str:
        if "realtaxdeed.com" in host or re.search(r"\btax\s*deed\b", label, re.I):
            if "realtaxdeed.com" in host:
                return "taxdeed"
        if "realforeclose.com" in host or re.search(r"foreclos", label, re.I):
            return "foreclosure"
        return "other"

    def add(label: str, raw_url: str):
        label = re.sub(r"\s+", " ", label or "").strip()
        raw_url = (raw_url or "").strip()
        if not raw_url or raw_url in ("#", "javascript:void(0)"):
            return
        if not raw_url.startswith("http"):
            # option values are often bare hosts like "www.volusia.realtaxdeed.com"
            if re.match(r"^[\w.-]+\.(com|org|net|gov)", raw_url):
                raw_url = "https://" + raw_url
            else:
                raw_url = urljoin(base_url, raw_url)
        host = urlparse(raw_url).netloc.lower()
        if not host or host in seen:
            return
        seen.add(host)
        state = ""
        m = re.match(r"^\s*([A-Z]{2})\s*[-–]", label)
        if m:
            state = m.group(1)
        entries.append(
            {
                "label": label,
                "url": raw_url,
                "host": host,
                "state": state,
                "kind": classify(host, label),
            }
        )

    def add_indexed_option(label: str):
        # Opaque option value ("17") — the site's JS holds the URL mapping, so
        # derive the host from the label using the platform's uniform naming:
        # "FL - Marion Taxdeed" -> www.marion.realtaxdeed.com.
        m = SELECTOR_LABEL_RE.match(label)
        if not m:
            return
        state, county, kind_word = m.group(1).upper(), m.group(2), m.group(3).lower()
        slug = re.sub(r"[^a-z0-9]", "", county.lower())
        if not slug:
            return
        domain = "realtaxdeed.com" if "tax" in kind_word else "realforeclose.com"
        add(f"{state} - {county} {m.group(3)}", f"https://www.{slug}.{domain}/")

    for select in soup.find_all("select"):
        for opt in select.find_all("option"):
            label = opt.get_text(" ", strip=True)
            val = (opt.get("value") or "").strip()
            if val.startswith("http") or re.match(r"^[\w.-]+\.(com|org|net|gov)", val):
                add(label, val)
            else:
                add_indexed_option(label)
    # Fallback: some skins render the selector as a link list.
    if not entries:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "realtaxdeed.com" in href or "realforeclose.com" in href:
                add(a.get_text(" ", strip=True), href)
    # Last resort: hosts embedded in inline JS (the numeric-value mapping).
    for host in sorted(set(h.lower() for h in HOST_RE.findall(html))):
        add(host, "https://" + host + "/")
    return entries


def filter_fl_taxdeed(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split selector entries into (florida taxdeed counties, rejected)."""
    counties, rejected = [], []
    for e in entries:
        is_fl = e["state"] in ("FL", "") and e["host"].endswith("realtaxdeed.com")
        # A non-FL state prefix on a realtaxdeed host would be unusual; reject
        # anything explicitly labeled as another state.
        if e["state"] and e["state"] != "FL":
            is_fl = False
        if is_fl and e["kind"] == "taxdeed":
            slug = e["host"].replace("www.", "").split(".")[0]
            name = re.sub(r"^\s*FL\s*[-–]\s*", "", e["label"], flags=re.I)
            name = re.sub(r"\btax\s*deed\b", "", name, flags=re.I).strip(" -–") or slug.title()
            counties.append({"slug": slug, "name": name, "url": e["url"], "host": e["host"], "label": e["label"]})
        else:
            rejected.append(e)
    counties.sort(key=lambda c: c["slug"])
    return counties, rejected


def parse_calendar_dates(html: str) -> list[dict]:
    """Find tax-deed sale dates on a calendar page.

    Returns [{date: 'MM/DD/YYYY', text: <cell text>}] for day cells whose text
    mentions a tax deed sale. Foreclosure-looking cells are skipped.
    """
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()

    def consider(date: str, text: str):
        text_l = re.sub(r"\s+", " ", text).strip()
        if date in seen:
            return
        if re.search(r"foreclos", text_l, re.I):
            return
        if re.search(r"tax\s*deed|taxdeed", text_l, re.I):
            seen.add(date)
            out.append({"date": date, "text": text_l})

    # Primary: RealAuction day cells carry a dayid="MM/DD/YYYY" attribute.
    for el in soup.find_all(attrs={"dayid": True}):
        m = DATE_RE.search(el.get("dayid", ""))
        if m:
            consider(m.group(1), el.get_text(" ", strip=True))
    # Fallback: onclick handlers embedding AUCTIONDATE=MM/DD/YYYY.
    if not out:
        for el in soup.find_all(onclick=True):
            m = re.search(r"AUCTIONDATE=(\d{2}/\d{2}/\d{4})", el["onclick"], re.I)
            if m:
                consider(m.group(1), el.get_text(" ", strip=True))
    out.sort(key=lambda d: (d["date"][6:], d["date"][:2], d["date"][3:5]))
    return out


def parse_calendar_month_label(html: str) -> str:
    """Best-effort 'August 2026'-style label of the month a calendar page shows."""
    soup = BeautifulSoup(html, "lxml")
    m = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b",
        soup.get_text(" ", strip=True),
    )
    return f"{m.group(1)} {m.group(2)}" if m else ""


def parse_auction_items(html: str, page_url: str, county: str, sale_date: str) -> list[AuctionRecord]:
    """Parse AUCTION_ITEM blocks from an auction preview/status page."""
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(page_url).netloc
    records: list[AuctionRecord] = []

    items = soup.select("div.AUCTION_ITEM")
    if not items:
        # Fallback: any element containing an auction-details table.
        items = [t.parent for t in soup.select("table.ad_tab, table.AUCTION_DETAILS")]

    for item in items:
        rec = AuctionRecord(county=county, sale_date=sale_date, auction_url=page_url, source_host=host)
        last_attr = None
        for row in item.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            if len(cells) == 1:
                label_text, value_cell = "", cells[0]
            else:
                label_text, value_cell = cells[0].get_text(" ", strip=True), cells[-1]
            value = value_cell.get_text(" ", strip=True)
            label = _norm_label(label_text)
            rec.raw_fields[label or f"(cont:{last_attr})"] = value

            if not label:
                # Continuation row (2nd address line: city/state/zip).
                if last_attr == "property_address" and value:
                    rec.property_address = f"{rec.property_address}, {value}".strip(", ")
                continue

            attr = LABEL_MAP.get(label)
            if attr is None:
                # Fuzzy: match on prefix, e.g. "case number" variants.
                for known, mapped in LABEL_MAP.items():
                    if label.startswith(known):
                        attr = mapped
                        break
            if attr is None:
                last_attr = None
                continue

            if attr in MONEY_FIELDS:
                setattr(rec, attr, parse_money(value))
            else:
                setattr(rec, attr, value)
            if attr == "parcel_id":
                link = value_cell.find("a", href=True)
                if link:
                    rec.appraiser_url = urljoin(page_url, link["href"])
            last_attr = attr

        # Status/time box that sits beside the details table.
        stat = item.select_one(".ASTAT_MSGB, .ASTAT_MSG, .Astat_DATA")
        if stat:
            rec.auction_status = stat.get_text(" ", strip=True)
        blob = item.get_text(" ", strip=True)
        tm = TIME_RE.search(rec.auction_status or blob)
        if tm:
            rec.sale_time = tm.group(1)

        if rec.parcel_id or rec.case_number or rec.certificate_number:
            records.append(rec)
    return records


def looks_like_foreclosure(rec: AuctionRecord) -> bool:
    """Sanity check: True if a record smells like a foreclosure listing."""
    at = (rec.auction_type or "").upper().replace(" ", "").replace("-", "")
    if at and "TAXDEED" not in at:
        return True
    if rec.source_host and "realforeclose.com" in rec.source_host:
        return True
    raw = " ".join(rec.raw_fields.keys())
    # Foreclosure listings carry judgment/plaintiff fields taxdeed pages never have.
    if re.search(r"final judgment|plaintiff", raw, re.I):
        return True
    return False


def page_looks_like_taxdeed_site(html: str) -> bool:
    """Cheap page-level check used before trusting any auction data."""
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    has_taxdeed = re.search(r"tax\s*deed", text, re.I) is not None
    has_foreclose_only = re.search(r"foreclosure sale", text, re.I) and not has_taxdeed
    return has_taxdeed and not has_foreclose_only

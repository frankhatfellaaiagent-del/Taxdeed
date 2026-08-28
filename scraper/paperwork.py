"""Read the paperwork inside a clerk case file.

Once scraper/clerk.py has found a parcel's case record and its document list,
this module opens those documents and reads them, so the property card can say
what is actually in the file rather than just linking it. The things that
change a bid — a rescheduled or cancelled sale, homestead, an IRS or municipal
lien that survives the deed, a bankruptcy stay, an HOA claim — are exactly the
things buried in these PDFs.

Extraction is text-layer only (pypdf). Many clerk documents are page scans with
no text layer; those are reported as "not machine-readable" rather than guessed
at, and the link is still on the card for a human to open. OCR is deliberately
out of scope for now.
"""

from __future__ import annotations

import io
import logging
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

TIMEOUT = 30
MAX_BYTES = 12 * 1024 * 1024      # skip oversized scans
MAX_PAGES = 12                    # first pages carry the operative text

# Patterns worth surfacing on the property card. Each entry is
# (flag label, regex) — matched against the document's text.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("sale rescheduled", re.compile(r"\bresched(?:uled)?\b", re.I)),
    ("sale cancelled", re.compile(r"\bcancell?ed\b", re.I)),
    ("redeemed", re.compile(r"\bredeem(?:ed|ption)\b", re.I)),
    ("homestead", re.compile(r"\bhomestead\b", re.I)),
    ("IRS lien", re.compile(r"\b(?:internal revenue|irs)\b[^.]{0,60}\blien\b", re.I)),
    ("federal tax lien", re.compile(r"\bfederal tax lien\b", re.I)),
    ("municipal or code lien", re.compile(
        r"\b(?:code enforcement|municipal|city of|county)\b[^.]{0,60}\blien\b", re.I)),
    ("special assessment", re.compile(r"\bspecial assessment\b", re.I)),
    ("lis pendens", re.compile(r"\blis pendens\b", re.I)),
    ("mortgage of record", re.compile(r"\bmortgage\b", re.I)),
    ("judgment lien", re.compile(r"\bjudgment\b[^.]{0,40}\blien\b", re.I)),
    ("bankruptcy", re.compile(r"\bbankrupt(?:cy)?\b", re.I)),
    ("HOA or association claim", re.compile(
        r"\b(?:homeowners?|property owners?|condominium)\s+assoc", re.I)),
    ("easement", re.compile(r"\beasement\b", re.I)),
    ("mobile home on parcel", re.compile(r"\bmobile home\b", re.I)),
]

# Documents worth opening, most decision-relevant first. The ownership &
# encumbrance / title report is the lien source, so it leads. Anything not
# matching is skipped so a case with 20 scanned exhibits doesn't blow the budget.
DOC_PRIORITY = ["ownership", "encumbrance", "o&e", "current owner",
                "property information", "title", "search", "lien",
                "513", "notice of publication", "affidavit", "tax deed",
                "certificate", "statement", "notice"]


def _doc_rank(doc: dict) -> int:
    name = (doc.get("name") or "").lower()
    for i, key in enumerate(DOC_PRIORITY):
        if key in name:
            return i
    return len(DOC_PRIORITY)


def extract_text(content: bytes, content_type: str = "") -> str:
    """Text from a PDF (text layer) or an HTML document. '' when unreadable."""
    head = content[:5]
    if head.startswith(b"%PDF") or "pdf" in content_type.lower():
        try:
            from pypdf import PdfReader
        except ImportError:                       # pypdf missing → skip quietly
            log.debug("pypdf not installed; skipping PDF text extraction")
            return ""
        try:
            reader = PdfReader(io.BytesIO(content))
            pages = reader.pages[:MAX_PAGES]
            return "\n".join((p.extract_text() or "") for p in pages)
        except Exception as exc:                  # noqa: BLE001 - malformed PDFs are common
            log.debug("PDF unreadable: %s", exc)
            return ""
    try:
        soup = BeautifulSoup(content, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text(" ", strip=True)
    except Exception:                             # noqa: BLE001
        return ""


def derive_flags(text: str) -> list[str]:
    """Which watch-items the document text mentions."""
    if not text:
        return []
    body = re.sub(r"\s+", " ", text)
    return [label for label, pat in PATTERNS if pat.search(body)]


def read_case_docs(docs: list[dict], session: requests.Session | None = None,
                   limit: int = 4) -> dict:
    """Open the most relevant case documents and summarize what they say.

    Returns {case_flags, docs_read, docs_unreadable} — never raises."""
    if not docs:
        return {}
    sess = session or requests.Session()
    ordered = sorted(docs, key=_doc_rank)[:limit]
    flags: list[str] = []
    read = unreadable = 0
    for doc in ordered:
        url = doc.get("url")
        if not url:
            continue
        try:
            resp = sess.get(url, timeout=TIMEOUT, stream=True)
            resp.raise_for_status()
            length = int(resp.headers.get("Content-Length") or 0)
            if length and length > MAX_BYTES:
                resp.close()
                unreadable += 1
                continue
            content = resp.raw.read(MAX_BYTES + 1, decode_content=True)
            ctype = resp.headers.get("Content-Type", "")
            resp.close()
        except requests.RequestException as exc:
            log.debug("case doc fetch failed %s: %s", url, exc)
            unreadable += 1
            continue
        text = extract_text(content, ctype)
        if not text.strip():
            unreadable += 1
            continue
        read += 1
        for f in derive_flags(text):
            if f not in flags:
                flags.append(f)
    out: dict = {"docs_read": read}
    if flags:
        out["case_flags"] = flags
    if unreadable:
        out["docs_unreadable"] = unreadable
    return out

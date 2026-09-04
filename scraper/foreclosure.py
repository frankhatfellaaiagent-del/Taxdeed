"""Risk flags for foreclosure auction records: the HOA / junior-lien trap.

Why: when the foreclosing plaintiff is an HOA/condo association or a junior
lienholder, the senior mortgage SURVIVES the sale (Fla. Stat. 720.3085(1),
718.116(5)(a)) and the buyer takes subject to it. Auction cards don't name the
plaintiff, so until docket enrichment fills `plaintiff` in, the strongest
available signals are the case number (association lien suits are usually
county-court "CC" cases; mortgage cases are circuit "CA") and a final judgment
that is tiny relative to assessed value.

Flags are screening signals, never title conclusions. A flag is never cleared
for lack of evidence — only stronger evidence (docket/official-records data)
upgrades or replaces it.
"""

from __future__ import annotations

import re

from .models import AuctionRecord

ASSOC_RE = re.compile(
    r"\b(HOMEOWNERS?'?|HOME\s*OWNERS?|PROPERTY\s*OWNERS?|CONDOMINIUM|CONDO|"
    r"COOPERATIVE|CO-?OP|TOWNHOMES?|VILLAS?|MASTER|COMMUNITY|RECREATION|"
    r"MAINTENANCE)\s+(ASSOCIATION|ASSN\.?|ASSOC\.?)\b"
    r"|\b(HOA|COA|POA)\b(?!\s*(BANK|TRUST))"
    r"|\bASSOCIATION\s*,?\s*INC\b",
    re.I,
)
BANK_RE = re.compile(
    r"\b(BANK|TRUST(EE)?|MORTGAGE|LENDING|LOAN|FUNDING|SAVINGS|CREDIT\s*UNION|"
    r"FINANCIAL|CAPITAL|SERVICING|FANNIE|FREDDIE|HUD|SECRETARY\s+OF\s+HOUSING|"
    r"VETERANS|USDA)\b",
    re.I,
)
JUNIOR_INSTR_RE = re.compile(
    r"\b(HOME\s*EQUITY|HELOC|LINE\s*OF\s*CREDIT|SECOND\s*MORTGAGE|2ND\s*MORTGAGE|"
    r"SUBORDINATE|JUNIOR|PURCHASE\s*MONEY\s*SECOND|SHIP|DOWN\s*PAYMENT\s*ASSISTANCE)\b",
    re.I,
)
CC_CASE_RE = re.compile(r"\b\d{2,4}[-\s]?CC[-\s]?\d+", re.I)

SMALL_JUDGMENT_RATIO = 0.15


def classify(rec: AuctionRecord) -> list[str]:
    """Compute flags for one foreclosure record; also stored on the record."""
    flags: list[str] = []
    plaintiff = rec.plaintiff or ""

    is_assoc = bool(ASSOC_RE.search(plaintiff)) and not BANK_RE.search(plaintiff)
    is_cc = bool(CC_CASE_RE.search(rec.case_number or ""))

    small_judgment = False
    if rec.final_judgment_amount and rec.assessed_value:
        small_judgment = (
            rec.final_judgment_amount / rec.assessed_value < SMALL_JUDGMENT_RATIO
        )

    if is_assoc:
        flags.append("HOA_TRAP")
    elif plaintiff and JUNIOR_INSTR_RE.search(plaintiff):
        flags.append("JUNIOR_TRAP")
    elif not plaintiff:
        if is_cc or small_judgment:
            flags.append("PROBABLE_HOA")
    elif small_judgment and BANK_RE.search(plaintiff):
        flags.append("POSSIBLE_JUNIOR")

    if is_cc and not any(f in flags for f in ("HOA_TRAP", "PROBABLE_HOA")):
        flags.append("COUNTY_COURT_CASE")

    rec.foreclosure_flags = ",".join(flags)
    return flags

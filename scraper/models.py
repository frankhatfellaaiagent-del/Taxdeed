"""Data model for a single tax deed auction listing."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass
class AuctionRecord:
    county: str = ""
    sale_date: str = ""            # MM/DD/YYYY as shown on the auction site
    sale_time: str = ""            # e.g. "9:00 AM ET" when available
    auction_type: str = ""         # must be TAXDEED (sanity-checked)
    case_number: str = ""
    certificate_number: str = ""
    parcel_id: str = ""
    owner_name: str = ""           # often only available via appraiser enrichment
    property_address: str = ""
    property_use: str = ""         # land use / property use description
    acreage: str = ""              # from appraiser enrichment when available
    opening_bid: float | None = None
    assessed_value: float | None = None
    auction_status: str = ""       # free text from the status box, if any
    auction_url: str = ""          # the preview page this row came from
    appraiser_url: str = ""        # parcel link captured from the site or template
    source_host: str = ""          # e.g. www.volusia.realtaxdeed.com
    scraped_at: str = ""           # ISO timestamp (UTC)
    raw_fields: dict = field(default_factory=dict)  # every label/value pair seen

    def key(self) -> tuple:
        """Stable identity for diffing across runs."""
        ident = self.parcel_id or self.case_number or self.certificate_number
        return (self.county.lower(), ident.strip().upper(), self.sale_date)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AuctionRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# Column order used for CSV output and the Excel master sheet.
CSV_COLUMNS = [
    "county",
    "sale_date",
    "sale_time",
    "parcel_id",
    "case_number",
    "certificate_number",
    "owner_name",
    "property_address",
    "property_use",
    "acreage",
    "opening_bid",
    "assessed_value",
    "auction_status",
    "auction_type",
    "auction_url",
    "appraiser_url",
    "source_host",
    "scraped_at",
]

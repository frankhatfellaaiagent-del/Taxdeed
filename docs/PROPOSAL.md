# Proposal — Florida Tax Deed Intelligence Tool

**Prepared for:** MADD Assets (Marlon & Jennifer)
**Prepared by:** Frank (Hat Fella AI)
**Date:** August 2026 · Status: beta, in active refinement

---

## The problem

Working Florida tax deed sales today means clicking in and out of 37 county
auction sites, appraiser sites and clerk portals, copying rows into
spreadsheets, and manually filtering out redeemed sales, foreclosures and
properties that never fit your criteria. Third-party lead lists are stale,
unrefined, and don't understand the land-focused business.

## What the tool does today (live)

- **One dashboard for 37 Florida counties** — every upcoming tax deed auction
  on the RealAuction platform, scraped automatically every Monday at 4:00 AM,
  with per-county filtering, full-text search, and redeemed sales separated
  out. Tax deeds only — foreclosure auctions are detected and excluded.
- **Buy-box first-wave filter** — every parcel is flagged MATCH / REVIEW / NO
  against your criteria (target counties, land-focused use, max bid caps,
  county deposit limits), so the day's list starts pre-sorted to your business.
- **Quick-look scrub (AI agent)** — an automated agent works through the
  buy-box parcels, pulls the county appraiser record (owner, mailing address,
  land use, acreage) and writes it onto the property card, upgrading or
  eliminating parcels before a human ever looks.
- **Every research link on one card** — property appraiser record, clerk of
  courts tax deed files (correct portal per county, including the odd ones),
  FEMA flood map centered on the parcel, National Wetlands Inventory mapper,
  satellite view that opens the maps app in the field.
- **Team workflow** — personal notes, Interested/Watching/Pass positions,
  custom lists grouped by sale date and county, max-bid tracking.
- **Live spreadsheet feed** — a Google Sheet that refreshes itself from every
  scrape, exportable by county or by list, with mailing addresses as
  enrichment coverage grows.
- **Phone-friendly** — the dashboard works on mobile for research on the road.

## Data quality

The scraper is validated row-by-row against the county sites (opening bids,
case numbers, statuses), counts are cross-checked against each county's own
advertised auction counts, and every run logs what it saw, skipped and
excluded. Missing addresses or values are the county's data, clearly marked —
never silently invented.

## Roadmap (next phases)

1. **Enrichment coverage** — appraiser quick-look across all counties and
   vendor platforms; mailing-address standardization for outreach lists.
2. **Deeper clerk integration** — case-number deep links where county portals
   support them; surplus/deposit info surfaced per county.
3. **Alerts** — notification when a new parcel hits your buy-box or a tracked
   parcel changes (price drop, redemption, cancellation).
4. **Private mobile app** — packaged app with push notifications and
   location-aware parcel lookup for field work.

## Delivery & maintenance model

- Hosted and running now; nothing to install. Weekly automation is scheduled;
  on-demand refreshes are available.
- County websites change — the maintenance arrangement covers monitoring,
  fixing parsers when counties redesign, adding counties, and tuning the
  buy-box as the business evolves.

## Commercial terms (to be completed)

| Item | Terms |
| --- | --- |
| One-time setup & customization | *[to fill]* |
| Monthly hosting + maintenance | *[to fill]* |
| Feature development (roadmap items) | *[to fill]* |
| License scope (company vs. individual) | *[to discuss]* |

## Next step

A short call with the decision maker: 15-minute live walkthrough of the
dashboard on real auction data, then agreement on scope and terms.

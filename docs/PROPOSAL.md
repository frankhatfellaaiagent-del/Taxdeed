# Proposal — Florida Tax Deed Intelligence Tool

**Prepared for:** MADD Assets (Marlon & Jennifer)
**Prepared by:** Frank (Hat Fella AI)
**Date:** August 2026 · Founding-customer subscription

---

## The problem

Working Florida tax deed sales today means clicking in and out of dozens of
county auction sites, appraiser sites and clerk portals, copying rows into
spreadsheets, and manually filtering out redeemed sales, foreclosures and
properties that never fit your criteria. Third-party lead lists are stale,
unrefined, and don't understand the land-focused business.

## What the tool does today (live)

- **All 67 Florida counties on one board** — a live auction feed for every
  county that sells tax deeds online (45 counties on the RealAuction platform,
  refreshed automatically every Monday at 4:00 AM), and for the counties that
  sell in person at the courthouse, the county's sale schedule and clerk
  records right in the app. Per-county filtering, full-text search, redeemed
  sales separated out. Tax deeds only — foreclosure auctions are detected and
  excluded. The evidence behind the claim is documented county by county.
- **Buy-box first-wave filter** — every parcel is flagged MATCH / REVIEW / NO
  against your criteria (target counties, land-focused use, max bid caps,
  county deposit limits), so the day's list starts pre-sorted to your business.
- **Quick-look scrub (AI agent)** — an automated agent works through the
  buy-box parcels, pulls the county appraiser record (owner, mailing address,
  land use, acreage) and writes it onto the property card, upgrading or
  eliminating parcels before a human ever looks.
- **The clerk's case file, per parcel** (rolling out county by county) — not a
  link to the county's tax deed page, but *this parcel's* case record: deed status (rescheduled, cancelled),
  the applicant who forced the sale, and every document in the file — All
  Forms, Tax Deed, Notice of Publication, Clerk's Affidavit, 513 — as a link.
- **The agent reads the paperwork** (as case files roll out) — it opens those
  documents and flags what
  changes a bid: reschedules, homestead, IRS and municipal liens, mortgages,
  judgments, bankruptcy, HOA claims, easements. Scanned documents it cannot
  read are marked as such rather than guessed at.
- **Every research link on one card** — property appraiser record, FEMA flood
  map centered on the parcel, National Wetlands Inventory mapper, satellite
  view that opens the maps app in the field.
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

1. **Coverage depth** — appraiser quick-look and clerk case-file resolution
   across the remaining counties and portal platforms; parsing the sale lists
   the in-person counties' clerks publish (and Okaloosa's Bid4Assets auctions)
   into the feed itself; mailing-address standardization for outreach lists.
2. **Parcel data (LandGlide's source)** — integration with ReportAll, the
   parcel database behind LandGlide, is built and waiting on an API key: true
   parcel boundaries and centroids, owner and mailing data — including for the
   vacant lots counties publish no address for. Their free trial covers
   validation before any spend.
3. **Alerts** — notification when a new parcel hits your buy-box or a tracked
   parcel changes (price drop, redemption, cancellation).
4. **Private mobile app** — packaged app with push notifications and
   location-aware parcel lookup for field work.

## How the subscription works

- Hosted and running now; nothing to install or maintain on your side. The
  subscription includes hosting, the weekly automated refresh (plus on-demand
  pulls), scraper fixes when county sites change, support, and every platform
  improvement — the roadmap ships as part of the plan, not as change orders.
- Your workspace is private to your team (lists, notes, positions, buy box).
  The license is non-exclusive — the platform serves other investors — but
  nobody ever sees your research. Founding rate locked 12 months.
- Month to month; cancel anytime; your data exports with you.

## Founding-customer terms

| Item | Terms |
| --- | --- |
| One-time setup & customization (counties, buy box, accounts) | **$500** |
| Monthly subscription — locked 12 months, up to 5 seats | **$197 / month** |
| Optional: ReportAll parcel data add-on | at cost + service margin |

In exchange for the founding rate: honest feedback while the beta hardens,
and a reference when it earns one.

## Next step

A 15-minute walkthrough with the decision maker on live auction data, pick a
start date, and the team's logins are issued the same day.

# Data feed for the dashboard

**If you are building the dashboard UI: this is your data source.** Fetch the JSON
feed below — no auth, no API key, CORS-open. Do not scrape the auction sites from
the frontend and do not read the Google Sheet; both are downstream of these files.

## Feeds (fixed URLs, refreshed by every scraper run)

| Feed | URL |
| --- | --- |
| JSON (use this) | `https://raw.githubusercontent.com/frankhatfellaaiagent-del/Taxdeed/main/data/exports/master_list.json` |
| TSV (Google Sheet import) | `https://raw.githubusercontent.com/frankhatfellaaiagent-del/Taxdeed/main/data/exports/master_list.tsv` |

(These URLs track `main`, the production branch.)

`raw.githubusercontent.com` serves `Access-Control-Allow-Origin: *`, so a plain
browser `fetch()` works from any origin:

```js
const FEED = "https://raw.githubusercontent.com/frankhatfellaaiagent-del/Taxdeed/main/data/exports/master_list.json";
const { generated_at, counts, records } = await (await fetch(FEED)).json();
```

## JSON shape

```jsonc
{
  "generated_at": "2026-08-11T22:45:00+00:00",   // UTC, when the feed was built
  "source_run": "2026-08-11T213144Z",            // most recent run merged into the feed
  "county_runs": { "putnam": "2026-08-15T035014Z", ... },  // which run each county's rows
                                                 // came from — partial runs refresh their
                                                 // counties and carry the rest forward
  "counts": {
    "total": 2648,
    "scheduled": 1894,                           // upcoming auctions
    "redeemed": 754,                             // owner paid; auction cancelled
    "counties": 30,                              // counties WITH records in the feed
    "counties_total": 67,                        // every Florida county (registry below)
    "by_county": { "putnam": {"total": 361, "scheduled": ..., "redeemed": ...}, ... }
  },
  "clerk_sites": {                               // Clerk of Court tax-deed pages (config/clerk_sites.yaml)
    "volusia": { "url": "https://www.clerk.org/tax-deeds.aspx", "search": "https://app02.clerk.org/or_td/" }
  },
  "counties_registry": [                         // ALL 67 Florida counties and how each sells
                                                 // (config/florida_counties.json, hand-maintained;
                                                 // evidence: docs/COVERAGE.md). The app shows every
                                                 // county — an info card where there's no online feed.
    { "slug": "alachua", "name": "Alachua", "coverage": "online", "platform": "realauction" },
    { "slug": "dixie", "name": "Dixie", "coverage": "in-person",  // or "online-other" (Okaloosa/Bid4Assets)
      "sale_info": "In person in the Courthouse Board Room ...",
      "clerk_url": "https://dixieclerk.com/...", "sale_list_url": "https://dixieclerk.com/..." }
  ],
  "default_buybox": {                            // NEUTRAL template every new team's buy-box seeds from
                                                 // (all counties targeted, generic land vocabulary — no
                                                 // customer's real criteria ever ships in the feed)
    "target_counties": ["putnam", "..."], "excluded_counties": ["volusia", "..."],
    "land_use_keywords": ["vacant", "land", "..."], "non_land_keywords": ["condo", "..."],
    "max_opening_bid": null, "county_caps": { "putnam": { "max_bid": 25000, "deposit": 2000 } }
  },
  "records": [
    {
      "county": "putnam",
      "sale_date": "08/12/2026",                 // MM/DD/YYYY (US Eastern)
      "sale_time": "09:00 AM ET",                // may be ""
      "parcel_id": "01-10-26-7200-0070-0010",
      "case_number": "...", "certificate_number": "...",
      "property_address": "400 ASH ST, PALATKA, FL- 32177",  // "" when the county doesn't publish it
      "owner_name": "",                          // from the auction record or appraiser enrichment
      "mailing_address": "",                     // owner's mailing address (appraiser enrichment only)
      "property_use": "",                        // land-use text (auction record or appraiser enrichment)
      "acreage": "",                             // acreage text when available
      "enriched": false,                         // true = appraiser quick-look scrub ran for this parcel
      "opening_bid": 8474.0,                     // number or null
      "assessed_value": 12000.0,                 // number or null (often null on redeemed/future rows)
      "bid_to_value_pct": 71,                    // integer % or null
      "anomalies": ["missing assessed value"],   // data-quality flags, may be []
      "status": "Scheduled",                     // Scheduled | Redeemed
      "auction_url": "https://www.putnam.realtaxdeed.com/index.cfm?...",   // live auction page
      "appraiser_url": "http://...",             // county property appraiser record, may be ""
      "lat": 29.648251, "lng": -81.637149,       // parcel coordinates; null when unresolved

      // --- clerk case file (from the scrub, or from the auction page itself
      //     where the county hyperlinks the case number to the clerk record) ---
      "clerk_case_url": "https://...",           // THIS parcel's case record at the clerk
      "deed_status": "RESCHED",                  // clerk's status for the tax deed
      "applicant": "JOCALBRO INC ...",           // who applied for the deed (forced the sale)
      "applicant_address": "PO BOX 2407 ...",
      "case_docs": [                             // paperwork the clerk publishes on the case
        {"name": "Notice of Publication", "date": "04/30/2026", "url": "https://..."}
      ],
      "case_flags": ["homestead", "IRS lien"]    // what the agent read in those documents
    }
  ]
}
```

Coordinates come from the free US Census geocoder (`scraper/geocode.py`), cached in
`data/geocache.json` so only new addresses are geocoded each run. They power the
dashboard's parcel-centered links (FEMA flood viewer, USFWS wetlands map, satellite
view). Treat them as approximate — rooftop/street-segment accuracy, not a surveyed
parcel centroid.

## Buy-box is per-team, computed in the browser

The scrape and this feed are the same for every customer — every Florida tax-deed
county, every property, unfiltered. What used to be baked in server-side
(`buybox`/`buybox_notes` on each record, computed from the single `config/buybox.yaml`)
is now only a legacy default for non-JS consumers (the TSV/Google Sheet). The
dashboard (`dashboard/index.html`) instead:

1. On first load, seeds a per-team buy-box config from `default_buybox` above.
2. Recomputes MATCH/REVIEW/NO for every record **client-side** (`evalBuybox()`, a
   direct JS port of `scraper/judgment.py::buybox_flag`) from that config.
3. Lets each team edit their own target counties, land-use keywords, max bid and
   per-county caps in Settings — instantly re-flagging the board, no redeploy.
4. Persists the config per-team (signed in: Supabase `team_state` row keyed
   `buybox`, shared live across the team; signed out: that browser's localStorage only).

This is what makes the platform multi-tenant on one shared scrape: onboarding a new
customer with entirely different counties or criteria is a Settings change they make
themselves, not a code change. See `docs/LOGIN_SETUP.md` for team onboarding.

## The quick-look scrub

`scraper/enrich.py` (`python -m scraper enrich`) runs over scheduled buy-box
MATCH/REVIEW parcels and gathers, per parcel:

1. **County appraiser record** — owner, mailing address, land use, acreage.
2. **Clerk case file** — some counties (Volusia among them) hyperlink the case
   number on the auction page straight to the clerk's tax deed record; the
   scraper keeps that link, which is the only case-file link available where no
   portal resolver exists. Where a resolver does exist, `scraper/clerk.py`
   resolves the parcel to *its own* case
   record, not the county's tax-deed page. One resolver per portal platform:
   `realtdm` (RealAuction's clerk module — index the public case list, then link
   `…/cases/getCase/caseid/<id>`), `taxsmart` (Pioneer — `…/Home/Details?id=<id>`),
   `template` (Putnam's certificate-number deep link), and `newvision` (Marion's
   postback app, driven with a browser in `scraper/clerk_browser.py`). Counties
   without a resolver keep the county-level link. Platforms are declared per county
   in `config/clerk_sites.yaml`.
3. **The paperwork** (`scraper/paperwork.py`) — opens the case's documents and reads
   the text layer, flagging what changes a bid: reschedules, cancellations,
   homestead, IRS/municipal liens, mortgages, judgments, bankruptcy, HOA claims,
   easements. Scanned documents with no text layer are reported, never guessed at.
4. **ReportAll parcel record** (`scraper/reportall.py`) — the parcel database behind
   LandGlide. Dormant unless `REPORTALL_API_KEY` is set; when enabled it fills owner,
   mailing address, acreage, land use and replaces rooftop geocodes with true parcel
   centroids.

Results accumulate in `data/enrichment.json` and are merged by the exporter *before*
buy-box flagging, so an enriched land use can move a REVIEW row to MATCH/NO. Each
source is optional and isolated — a portal that changes shape costs coverage, never
the feed. Entries refresh every 30 days, so daily runs widen coverage rather than
refetching the same parcels.

## Dashboard guidance

- Default view should filter `status == "Scheduled"` (Redeemed auctions won't happen);
  keep Redeemed reachable via a toggle.
- `buybox` drives the client's priority: `REVIEW` rows are target-county parcels
  needing a click on `appraiser_url` to confirm land use; `NO` includes the reason in
  `buybox_notes` (e.g. coastal county).
- Missing `property_address` / `assessed_value` is the county site's data, not an
  error — render as "—".
- Show `generated_at` ("data as of …") so stale data is visible.
- Cadence: the feed updates whenever a scraper workflow run completes (scheduled
  daily in production, plus manual runs). Poll or re-fetch on page load; no webhook.

## Provenance

Feeds are written by `python -m scraper export` (see `scraper/exporter.py`) from the
latest committed run in `data/runs/<timestamp>/`, and refreshed automatically by the
"Daily data refresh" GitHub Actions workflow after each run (plus the on-demand
enrichment workflow). The Google
Sheet ("MADD Assets — FL Tax Deed Auctions") imports the TSV via IMPORTDATA and
refreshes itself; treat it as a human-facing mirror, not a source.

## On-demand AI property analysis (not part of this feed)

The dashboard's "Research with AI" button runs a per-parcel AI research agent.
That analysis is **not** part of `master_list.json` and does not change this
feed's schema. It runs server-side in a Supabase Edge Function
(`supabase/functions/analyze-property`) and is cached in the Supabase
`parcel_analysis` table, keyed by the same `county|parcel_id|case_number`
composite the app uses everywhere. Results are shared across teams and reused for
30 days. The feature is dormant until an OpenAI key is configured — see
`docs/AI_SETUP.md`.

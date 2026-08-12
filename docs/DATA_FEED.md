# Data feed for the dashboard

**If you are building the dashboard UI: this is your data source.** Fetch the JSON
feed below — no auth, no API key, CORS-open. Do not scrape the auction sites from
the frontend and do not read the Google Sheet; both are downstream of these files.

## Feeds (fixed URLs, refreshed by every scraper run)

| Feed | URL |
| --- | --- |
| JSON (use this) | `https://raw.githubusercontent.com/frankhatfellaaiagent-del/Taxdeed/claude/florida-tax-deed-scraper-fqzwzd/data/exports/master_list.json` |
| TSV (Google Sheet import) | `https://raw.githubusercontent.com/frankhatfellaaiagent-del/Taxdeed/claude/florida-tax-deed-scraper-fqzwzd/data/exports/master_list.tsv` |

(When the branch merges to `main`, swap the branch segment of the URL accordingly.)

`raw.githubusercontent.com` serves `Access-Control-Allow-Origin: *`, so a plain
browser `fetch()` works from any origin:

```js
const FEED = "https://raw.githubusercontent.com/frankhatfellaaiagent-del/Taxdeed/claude/florida-tax-deed-scraper-fqzwzd/data/exports/master_list.json";
const { generated_at, counts, records } = await (await fetch(FEED)).json();
```

## JSON shape

```jsonc
{
  "generated_at": "2026-08-11T22:45:00+00:00",   // UTC, when the feed was built
  "source_run": "2026-08-11T213144Z",            // scraper run the data came from
  "counts": {
    "total": 2648,
    "scheduled": 1894,                           // upcoming auctions
    "redeemed": 754,                             // owner paid; auction cancelled
    "counties": 30,
    "by_county": { "putnam": {"total": 361, "scheduled": ..., "redeemed": ...}, ... }
  },
  "records": [
    {
      "county": "putnam",
      "sale_date": "08/12/2026",                 // MM/DD/YYYY (US Eastern)
      "sale_time": "09:00 AM ET",                // may be ""
      "parcel_id": "01-10-26-7200-0070-0010",
      "case_number": "...", "certificate_number": "...",
      "property_address": "400 ASH ST, PALATKA, FL- 32177",  // "" when the county doesn't publish it
      "opening_bid": 8474.0,                     // number or null
      "assessed_value": 12000.0,                 // number or null (often null on redeemed/future rows)
      "bid_to_value_pct": 71,                    // integer % or null
      "buybox": "REVIEW",                        // MATCH | REVIEW | NO (client's rural/Central-FL criteria)
      "buybox_notes": "target county; property use unknown — verify on appraiser site",
      "anomalies": ["missing assessed value"],   // data-quality flags, may be []
      "status": "Scheduled",                     // Scheduled | Redeemed
      "auction_url": "https://www.putnam.realtaxdeed.com/index.cfm?...",   // live auction page
      "appraiser_url": "http://..."              // county property appraiser record, may be ""
    }
  ]
}
```

## Dashboard guidance

- Default view should filter `status == "Scheduled"` (Redeemed auctions won't happen);
  keep Redeemed reachable via a toggle.
- `buybox` drives the client's priority: `REVIEW` rows are target-county parcels
  needing a click on `appraiser_url` to confirm land use; `NO` includes the reason in
  `buybox_notes` (e.g. coastal county).
- Missing `property_address` / `assessed_value` is the county site's data, not an
  error — render as "—".
- Show `generated_at` ("data as of …") so stale data is visible.
- Cadence: the feed updates whenever a scraper workflow run completes (manual runs
  today, weekly scheduled in production). Poll or re-fetch on page load; no webhook.

## Provenance

Feeds are written by `python -m scraper export` (see `scraper/exporter.py`) from the
latest committed run in `data/runs/<timestamp>/`, and refreshed automatically by the
"Pilot scrape (live)" GitHub Actions workflow after each manual data run. The Google
Sheet ("MADD Assets — FL Tax Deed Auctions") imports the TSV via IMPORTDATA and
refreshes itself; treat it as a human-facing mirror, not a source.

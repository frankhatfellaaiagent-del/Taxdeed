---
name: tax-deed-scrub
description: >
  Run the Florida tax deed auction scrape (RealAuction counties) and produce the
  client Excel report for MADD Assets. Use when asked to run the tax deed scrub,
  refresh auction data, scrape tax deed sales, or check for new/changed auctions.
  Accepts an optional comma-separated county list (e.g. "volusia,polk") to run a
  subset; default is all discovered Florida taxdeed counties.
---

# Florida Tax Deed Scrub

You are running a recurring data pipeline for MADD Assets Office (tax deed
investors, rural/land-focused Central Florida). The mechanical work lives in
`scraper/`; your job is to drive it, then apply judgment to the results.

Hard rules, never relax them:
- **Tax deed only.** Only `*.realtaxdeed.com` hosts. Never scrape
  `realforeclose.com` or non-FL sites. If foreclosure rows show up in output,
  they must stay excluded and be called out in your summary.
- **Politeness.** Keep the default rate limits (or slower). Never parallelize
  requests to the auction sites. Respect robots.txt skips — report them, don't
  work around them.

## Arguments

`$ARGUMENTS` may contain a comma-separated county subset (slugs as in
`config/counties.json`), e.g. `volusia,polk`. Empty = all counties. A full
50+ county run takes on the order of 1–2 hours at polite rate limits — warn
the user, then proceed.

## Workflow

1. **Preflight.** `pip install -r requirements.txt` if needed; for live runs
   also `playwright install chromium` (skip if already installed). Verify
   network access to `https://www.volusia.realtaxdeed.com` before a long run;
   if unreachable, stop and tell the user (corporate egress policies often
   block it — the run must happen from a machine that can reach the site).
2. **County list.** If `config/counties.json` is missing or older than ~30
   days, refresh it: `python -m scraper discover`. Sanity-check the result:
   ~50+ FL counties, all hosts `*.realtaxdeed.com`, rejected list contains the
   foreclosure/non-FL entries. If discovery finds < 40 counties, treat it as a
   site change and investigate before scraping.
3. **Scrape.**
   `python -m scraper scrape [--counties <subset>]`
   (add `--months N` to widen the calendar window; default current + 2).
   Watch the log: individual county errors are OK (they're skipped and
   reported), but if *most* counties error the site structure changed — go to
   the breakage playbook instead of shipping an empty report.
4. **Report.** `python -m scraper report` (auto-picks the latest run and the
   previous one for diffing). This writes `<run>/tax_deed_scrub.xlsx` and
   `<run>/findings.json`.
5. **Judgment pass — read `findings.json` and the run outputs, then:**
   - **REVIEW rows**: rows flagged `REVIEW` are target-county parcels with no
     property-use data. Open each row's appraiser link (column in the sheet)
     if network allows, determine land use/acreage, and upgrade to MATCH or
     downgrade to NO in the Excel file; otherwise list them for the client to
     verify manually.
   - **Anomalies**: sanity-check each one (missing bids, bid > assessed,
     cancelled/redeemed statuses). Drop obvious parse garbage; keep real
     oddities flagged.
   - **Changes/removals**: summarize what's new, what changed (esp. bid
     increases), what disappeared (likely redeemed/cancelled — say which when
     the status text says so).
   - **Excluded foreclosure rows**: confirm they were correctly excluded;
     more than a handful suggests a mis-resolved county URL — check
     `config/counties.json` for that county.
6. **Deliver.** Tell the user: counties covered, total auctions, NEW / CHANGED
   / REMOVED counts, buy-box matches by county (call out the best-looking
   ones: low opening bid vs assessed, acreage), counties skipped with errors,
   and attach/point to the Excel file.

## Breakage playbook (site changed)

Symptoms → likely cause → fix. Parsers live in `scraper/parsing.py`; keep them
selector-flexible, don't hardcode one skin.

- **Discovery finds 0 or few counties** → county selector markup changed →
  save the homepage HTML into `scraper/fixtures/volusia/home.html`-style file,
  inspect the new dropdown/link structure, update `parse_county_selector`,
  re-test offline: `python -m scraper discover --fixtures scraper/fixtures`.
- **Calendar loads, no dates** → day-cell markup changed (`dayid` attr gone)
  → capture a live calendar HTML, add the new pattern to
  `parse_calendar_dates` (keep old patterns as fallbacks).
- **Auction pages load, no items** → `AUCTION_ITEM`/details-table markup
  changed → capture a live auction HTML, extend `parse_auction_items` and
  `LABEL_MAP` (labels vary slightly per county — add variants, don't replace).
- **Playwright timeouts everywhere** → site blocking or slow; do NOT lower
  delays or retry aggressively. Re-run the failed counties once with
  `--delay 8`. If still blocked, report it — the answer may be to run less
  often, not harder.
- After any parser fix: re-run the offline regression
  (`python -m scraper run --counties volusia,polk --fixtures scraper/fixtures --skip-robots`)
  to confirm nothing else broke, update fixtures if the new markup is now
  canonical, then re-run the live scrape for the affected counties only.

## Where things live

- County list (generated): `config/counties.json`
- Buy-box rules (client-editable): `config/buybox.yaml`
- Runs: `output/runs/<UTC timestamp>/` — per-county CSV/JSON, `run_meta.json`,
  `excluded_foreclosure.json`, `findings.json`, `tax_deed_scrub.xlsx`
- Diffing compares against the most recent earlier run dir automatically.

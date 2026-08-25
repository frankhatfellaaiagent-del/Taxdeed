# Tax Deed Radar

A Florida tax deed auction intelligence platform: one automated pipeline collects
every upcoming tax deed sale from the county auction sites, verifies parcels
against county appraiser and clerk records, and serves a multi-tenant dashboard
where each customer team grades the board against their own buy box.

Live site: landing page at the Pages root, the app at `/app/`.

## Architecture

```
GitHub Actions (daily cron, 08:00 UTC; discovery + capture Mondays only)
  └─ scraper/            Playwright collector for the county auction sites
      ├─ data/runs/<ts>/ raw per-county output (committed)
      ├─ enrich          appraiser + clerk case-file verification (best-effort)
      └─ exporter        merges runs → data/exports/master_list.{json,tsv}
data/exports/master_list.json   the feed — one file, all counties, no flags
  └─ dashboard/index.html       static app (GitHub Pages /app/)
      ├─ per-team buy box       flags computed client-side from team settings
      └─ Supabase Auth+Postgres   login, cross-device sync, team workspaces
site/index.html                 sales landing page (Pages root)
```

Key properties:

- **One scrape serves every customer.** The feed is neutral (no flags, no
  customer criteria); each team's buy box lives in their private Supabase
  `team_state` row and MATCH/REVIEW/NO is computed in the browser (`evalBuybox`
  in `dashboard/index.html`, mirrored server-side by `scraper/judgment.py` for
  the operator's TSV mirror).
- **Partial runs merge, never replace.** `scraper/exporter.py` refreshes the
  counties a run scraped and carries every other county forward;
  `county_runs` in the feed records each county's source run.
- **Tax deeds only.** Discovery validates the state and auction type from the
  platform's own selector; foreclosure-looking rows are excluded and logged.
- **Tenant isolation** is enforced by Supabase row-level security (each user
  scoped to their own rows and their team's, keyed off an operator-set
  `profiles.team_id`) plus closed signup. See `docs/LOGIN_SETUP.md` for
  provisioning a customer.

## Repository map

| Path | What |
| --- | --- |
| `scraper/` | Collector, parsers, enrichment (appraiser/clerk/paperwork), exporter |
| `dashboard/` | The customer app (single static file + design system) |
| `site/` | The sales landing page |
| `config/client_counties.txt` | Counties the daily run collects |
| `config/buybox.yaml` | Operator's default buy-box (drives TSV flags + enrichment priority) |
| `config/clerk_sites.yaml` | Clerk of Court portals per county + platform resolvers |
| `data/runs/` | Committed raw runs (retention: pruned by the daily workflow) |
| `data/exports/` | The feeds consumed by the app and the Google Sheet |
| `docs/DATA_FEED.md` | Feed contract |
| `docs/LOGIN_SETUP.md` | Operator runbook: Supabase login + customer onboarding |
| `.github/workflows/` | Daily scrape + enrichment + Pages deploy |

## Running it

```bash
pip install -r requirements.txt && playwright install chromium

# offline regression against saved fixtures (no network)
python -m scraper run --counties volusia,polk --fixtures scraper/fixtures --skip-robots

# live: discover counties, scrape, build the feed
python -m scraper discover
python -m scraper run --counties @config/client_counties.txt
python -m scraper export

# enrichment (appraiser + clerk case files) against the current feed
python -m scraper enrich --limit 200
```

Production runs entirely on GitHub Actions — the scrape workflow has run on
schedule since 2026-08-11 (weekly at first, daily since 2026-08-25 so
redemptions surface same-day), committing each run and redeploying Pages. No
server, no cron on anyone's machine.

## Politeness & sourcing

The collector reads public pages only (never behind a login), respects
robots.txt, rate-limits per host with jittered delays, and runs once daily. All
data is Florida public record; the product's value is collection, verification
and workflow, not the data itself. Every surface tells users to verify with
the county before bidding.

## Testing

- Fixture regression: `python -m scraper run --fixtures scraper/fixtures ...`
  runs the full parse pipeline against saved HTML (also runs in CI on push).
- Playwright suites cover the dashboard (rendering, filters, case-file block,
  login/sync against a stubbed Supabase, per-team buy-box, lists, sort, branding
  and failure states); they live in the operator's session scratchpad and run
  before every deploy.
- When a county site changes shape: `python -m scraper capture --url <page>`
  saves HTML + structure diagnostics to iterate parsers against.

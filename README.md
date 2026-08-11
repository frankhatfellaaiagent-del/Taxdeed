# Florida Tax Deed Scrub — MADD Assets Office

Automation that replaces manual copying of Florida county tax deed auction
data into Excel. A Python scraper does the mechanical work against the
RealAuction platform (`{county}.realtaxdeed.com`); the `tax-deed-scrub`
Claude skill drives it and applies judgment (buy-box flags, diffs vs the
previous run, anomaly review, fixing the scraper when the site changes).

**Scope guardrails:** Florida **tax deed** auctions only. Foreclosure sites
(`realforeclose.com`) and non-FL entries in the platform's county selector are
detected and rejected at discovery time, and every scraped record passes a
tax-deed sanity check — foreclosure-looking rows are excluded and logged.

## Setup

**Mac, no terminal needed:** after cloning, double-click in Finder:
`Start Dashboard.command` (opens the dashboard in your browser and keeps it
running), then `Run Demo Scrape.command` (sample data, no internet) or
`Run Live Pilot.command` (real Volusia scrape). Leave the dashboard window open.

**Terminal:**

```bash
pip install -r requirements.txt
playwright install chromium        # needed for live scraping only
```

**Live pilot on GitHub's servers (no local setup):** the
`Pilot scrape (live)` workflow (Actions tab) runs the real scrape on 1–3
counties — trigger it with "Run workflow", results appear in the job log and
as a downloadable artifact. It also runs automatically on pushes that touch
the scraper.

## Usage

```bash
# 1. Build the authoritative county list from the site's own county selector
#    (writes config/counties.json; re-run monthly or when counties change)
python -m scraper discover

# 2. Scrape - all counties, or a fast subset for demos
python -m scraper scrape                          # all (~50 counties, 1-2h at polite rates)
python -m scraper scrape --counties volusia,polk  # subset, minutes
python -m scraper scrape --months 3               # widen calendar window (default: current + 2)

# 3. Build the Excel report (auto-diffs against the previous run)
python -m scraper report

# Or all in one:
python -m scraper run --counties volusia,polk

# Watch it live: in a second terminal, then open http://127.0.0.1:8777
python -m scraper dashboard
```

### Dashboard

`python -m scraper dashboard` serves a local page (stdlib only, binds to
127.0.0.1) that refreshes every 3 seconds:

- **Run in progress** — progress bar, county currently being scraped, and a
  live feed of finished counties with ok/error/robots-skip status, record
  counts, and warnings. Start a scrape in another terminal and watch it pull.
- **Latest results** — auctions, NEW/CHANGED/REMOVED, buy-box matches,
  county errors; auctions-by-county chart; per-county status table.
- **Run history** — last 20 runs with links to download each Excel report.

Outputs land in `output/runs/<UTC timestamp>/`:

| File | What |
| --- | --- |
| `<county>.csv` / `<county>.json` | Raw records per county |
| `run_meta.json` | Per-county status, warnings, errors, robots.txt results |
| `excluded_foreclosure.json` | Rows excluded by the tax-deed sanity check |
| `findings.json` | Machine-readable summary (new/changed/removed, buy-box, anomalies) |
| `tax_deed_scrub.xlsx` | Client deliverable (see below) |

### The Excel report

- **Summary** — totals, NEW/CHANGED/REMOVED vs previous run, buy-box matches, per-county table with errors.
- **All Auctions** — one master tab (county column, sortable/filterable): sale date/time,
  parcel, case & certificate #, owner, address, property use, acreage, opening bid,
  assessed value, bid/assessed ratio, buy-box flag + reasons, status vs last run,
  changed fields, anomalies, links to the auction page and county appraiser.
  Rows are color-coded: green = NEW, yellow = CHANGED.
- **Removed** — rows present last run, gone now (usually redeemed/cancelled/sold).
- **Issues** — county errors, parse warnings, robots.txt skips, excluded foreclosure rows.

### Buy-box flagging

`config/buybox.yaml` is the client-editable rule file: target counties
(rural/Central FL), excluded counties (coastal/metro), land-use keywords, and
optional bid caps. Flags: `MATCH` (target county + land-like use), `REVIEW`
(target county, use unknown — verify on the appraiser site), `NO`. Everything
is still scraped regardless; the flag only prioritizes.

Owner name and acreage are usually **not** on the RealAuction pages; the sheet
carries a per-parcel **appraiser link** (captured from the auction page) for
filling those during review. Deeper automated appraiser enrichment is a
possible follow-up — appraiser sites vary per county (qPublic, custom, etc.).

## The skill (judgment layer)

`.claude/skills/tax-deed-scrub/SKILL.md`. In Claude Code, run:

```
/tax-deed-scrub              # all counties
/tax-deed-scrub volusia,polk # subset
```

It runs the pipeline, reviews REVIEW rows and anomalies, summarizes
new/changed/removed listings and the best buy-box matches, and — if the site
changed — diagnoses and fixes the parsers, re-tests offline, then re-runs.

## Scheduling weekly

The run must happen on a machine that can reach `*.realtaxdeed.com` (some
managed/cloud environments block it — see Politeness below for why we don't
route around anything).

- **With judgment layer (recommended):** on a machine with Claude Code
  installed, schedule a weekly headless invocation, e.g. cron:
  `0 7 * * 1  cd /path/to/Taxdeed && claude -p "/tax-deed-scrub" --dangerously-skip-permissions >> cron.log 2>&1`
- **Scraper only:** `0 7 * * 1  cd /path/to/Taxdeed && python -m scraper run >> cron.log 2>&1`
  (Windows: same command in Task Scheduler.) Someone then eyeballs the Issues
  tab instead of the skill doing it.
- **GitHub Actions:** a weekly workflow can run the scraper and commit
  outputs, IF GitHub runner IPs aren't blocked by the site — test before
  relying on it.

## Politeness / compliance

- Sequential requests only, ~3s + jitter between page loads (`--delay` to
  slow further); per-county `robots.txt` is checked at runtime and honored —
  blocked counties are skipped and reported, never worked around.
- These are public records, but RealAuction's terms restrict aggressive
  crawling. Weekly cadence + these limits is deliberately conservative. Do
  not parallelize and do not add retry storms.

## Fixtures & offline regression test

`scraper/fixtures/` holds RealAuction-shaped HTML. After touching any parser:

```bash
python -m scraper discover --fixtures scraper/fixtures
python -m scraper run --counties volusia,polk --fixtures scraper/fixtures --skip-robots
```

Expected: 19 counties discovered (8 selector entries rejected), 9 records,
1 excluded foreclosure row, 2 buy-box MATCHes (Polk grazing + timberland).

## Adding / removing counties

Don't edit `config/counties.json` by hand — re-run `python -m scraper
discover` (the site's selector is the source of truth). To *scrape* fewer
counties, use `--counties`. To change buy-box county lists, edit
`config/buybox.yaml` (names are punctuation-insensitive).

## Common breakages

See the breakage playbook in `.claude/skills/tax-deed-scrub/SKILL.md` —
symptoms, likely cause, and fix procedure for: county selector changes,
calendar markup changes, auction-item markup changes, and blocking/timeouts.

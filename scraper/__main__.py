"""CLI entry point.

  python -m scraper discover  [--fixtures DIR]
  python -m scraper scrape    [--counties a,b,c] [--months N] [--delay S]
                              [--fixtures DIR] [--out DIR] [--skip-robots]
  python -m scraper report    [--run DIR] [--prev DIR] [--buybox FILE] [--out FILE]
  python -m scraper run       (scrape + report in one shot; same flags)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import diffing, judgment
from .discovery import discover_counties, load_counties, save_counties
from .excel_report import build_workbook, load_json
from .scrape import run_scrape
from .sources import FixtureSource, LiveSource, RateLimiter

ROOT = Path(__file__).resolve().parent.parent
COUNTIES_PATH = ROOT / "config" / "counties.json"
RUNS_ROOT = ROOT / "output" / "runs"

log = logging.getLogger("scraper")


def make_source(args):
    if args.fixtures:
        return FixtureSource(args.fixtures)
    return LiveSource(rate=RateLimiter(base_delay=args.delay))


def cmd_discover(args) -> int:
    source = make_source(args)
    try:
        result = discover_counties(source)
    finally:
        source.close()
    save_counties(result, COUNTIES_PATH)
    print(f"{len(result['counties'])} FL taxdeed counties -> {COUNTIES_PATH}")
    for c in result["counties"]:
        print(f"  {c['slug']:<14} {c['url']}")
    if result["rejected"]:
        print(f"({len(result['rejected'])} selector entries rejected: foreclosure/non-FL — see counties.json)")
    return 0


def _slugify(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _county_names_from(arg: str) -> list[str]:
    """Comma-separated slugs, or @path/to/file with one county name per line."""
    if arg.startswith("@"):
        lines = Path(arg[1:]).read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return [c.strip() for c in arg.split(",") if c.strip()]


def _select_counties(args) -> list[dict]:
    if not COUNTIES_PATH.exists():
        sys.exit("config/counties.json missing — run `python -m scraper discover` first")
    counties = load_counties(COUNTIES_PATH)
    if args.counties:
        wanted = [_slugify(w) for w in _county_names_from(args.counties)]
        by_slug = {c["slug"]: c for c in counties}
        missing = [w for w in wanted if w not in by_slug]
        if missing:
            sys.exit(f"Unknown counties: {missing}. Known: {sorted(by_slug)}")
        counties = [by_slug[w] for w in wanted]
    return counties


def cmd_scrape(args) -> Path:
    counties = _select_counties(args)
    out_dir = Path(args.out) if args.out else RUNS_ROOT / datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    source = make_source(args)
    try:
        meta = run_scrape(source, counties, out_dir, months=args.months, skip_robots=args.skip_robots)
    finally:
        source.close()
    ok = sum(1 for c in meta["counties"].values() if c["status"] == "ok")
    err = sum(1 for c in meta["counties"].values() if c["status"] == "error")
    total = sum(c.get("auctions", 0) for c in meta["counties"].values())
    print(f"Run complete: {ok} counties ok, {err} errored, {total} auction records -> {out_dir}")
    return out_dir


def cmd_report(args, run_dir: Path | None = None) -> int:
    run_dir = Path(args.run) if getattr(args, "run", None) else run_dir
    if run_dir is None:
        runs = sorted(d for d in RUNS_ROOT.iterdir() if d.is_dir()) if RUNS_ROOT.exists() else []
        if not runs:
            sys.exit("No runs found under output/runs — scrape first")
        run_dir = runs[-1]

    records = diffing.load_run_records(run_dir)
    records, n_dupes = judgment.dedupe(records)

    prev_dir = Path(args.prev) if getattr(args, "prev", None) else diffing.find_previous_run(run_dir.parent, run_dir)
    previous = diffing.load_run_records(prev_dir) if prev_dir else None
    diff = diffing.diff_runs(records, previous)

    cfg = judgment.load_buybox(getattr(args, "buybox", None))
    annotations = {}
    for rec in records:
        flag, reasons = judgment.buybox_flag(rec, cfg)
        annotations[rec.key()] = {
            "buybox": flag,
            "buybox_reasons": reasons,
            "anomalies": judgment.find_anomalies(rec),
        }

    run_meta = load_json(run_dir / "run_meta.json", {})
    excluded = load_json(run_dir / "excluded_foreclosure.json", [])

    out_path = Path(args.report_out) if getattr(args, "report_out", None) else run_dir / "tax_deed_scrub.xlsx"
    build_workbook(out_path, records, annotations, diff, run_meta, excluded)

    by_county: dict[str, dict] = {}
    for rec in records:
        d = by_county.setdefault(rec.county, {"auctions": 0, "match": 0, "review": 0, "new": 0, "changed": 0})
        d["auctions"] += 1
        ann = annotations[rec.key()]
        d["match"] += ann["buybox"] == "MATCH"
        d["review"] += ann["buybox"] == "REVIEW"
        st = diff["status"].get(rec.key(), "")
        d["new"] += st == "NEW"
        d["changed"] += st == "CHANGED"

    findings = {
        "run": str(run_dir),
        "previous_run": str(prev_dir) if prev_dir else None,
        "records": len(records),
        "duplicates_merged": n_dupes,
        "new": sum(1 for s in diff["status"].values() if s == "NEW"),
        "changed": sum(1 for s in diff["status"].values() if s == "CHANGED"),
        "removed": len(diff["removed"]),
        "buybox_match": sum(1 for a in annotations.values() if a["buybox"] == "MATCH"),
        "buybox_review": sum(1 for a in annotations.values() if a["buybox"] == "REVIEW"),
        "by_county": by_county,
        "anomalies": {"|".join(map(str, k)): a["anomalies"] for k, a in annotations.items() if a["anomalies"]},
        "changes": {"|".join(map(str, k)): v for k, v in diff["changes"].items()},
        "excluded_foreclosure": len(excluded),
        "county_errors": {s: c.get("error") for s, c in (run_meta.get("counties") or {}).items() if c.get("status") == "error"},
        "county_warnings": {s: c.get("warnings") for s, c in (run_meta.get("counties") or {}).items() if c.get("warnings")},
        "report": str(out_path),
    }
    (run_dir / "findings.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in findings.items() if k not in ("anomalies", "changes")}, indent=2))
    print(f"Excel report: {out_path}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scraper", description="FL tax deed auction scraper (RealAuction platform)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--fixtures", help="Directory of saved HTML fixtures (offline mode)")
        p.add_argument("--delay", type=float, default=3.0, help="Base seconds between page loads (default 3.0)")

    p = sub.add_parser("discover", help="Build config/counties.json from the site's county selector")
    add_common(p)

    p = sub.add_parser("scrape", help="Scrape upcoming tax deed auctions")
    add_common(p)
    p.add_argument("--counties", help="Comma-separated county slugs (default: all discovered)")
    p.add_argument("--months", type=int, default=2, help="Extra months of calendar to scan (default 2)")
    p.add_argument("--out", help="Output run directory (default output/runs/<utc timestamp>)")
    p.add_argument("--skip-robots", action="store_true", help="Skip the per-county robots.txt check (fixtures/testing only)")

    p = sub.add_parser("report", help="Diff vs previous run, flag buy-box, build Excel")
    p.add_argument("--run", help="Run directory (default: latest under output/runs)")
    p.add_argument("--prev", help="Previous run directory (default: auto-detect)")
    p.add_argument("--buybox", help="Path to buybox.yaml (default config/buybox.yaml)")
    p.add_argument("--report-out", help="Excel output path (default <run>/tax_deed_scrub.xlsx)")

    p = sub.add_parser("capture", help="Save live page HTML/screenshots + structure diagnostics (debug)")
    p.add_argument("--url", default="https://www.volusia.realtaxdeed.com/")
    p.add_argument("--out", default="output/debug")

    p = sub.add_parser("coverage", help="Probe non-RealAuction counties against DeedAuction; write coverage.json + HTML evidence")
    p.add_argument("--out", default="output/coverage")
    p.add_argument("--delay", type=float, default=3.0, help="Base seconds between requests (default 3.0)")

    p = sub.add_parser("verify-links", help="Fetch every clerk_url/sale_list_url in config/florida_counties.json and report broken ones")
    p.add_argument("--out", default="output/link-check")
    p.add_argument("--delay", type=float, default=2.0, help="Base seconds between requests (default 2.0)")

    p = sub.add_parser("diagnose-enrichment", help="Debug why appraiser/clerk enrichment fails for specific counties")
    p.add_argument("--out", default="output/diagnose")

    p = sub.add_parser("capture-sale-lists", help="Fetch + structurally describe the non-RealAuction counties' sale-list pages (Phase C/E evidence)")
    p.add_argument("--out", default="output/sale-lists")
    p.add_argument("--counties", help="Comma-separated slugs (default: the 7 non-RealAuction targets)")
    p.add_argument("--delay", type=float, default=3.0, help="Base seconds between requests (default 3.0)")
    p.add_argument("--render", action="store_true", help="Also load each page in Chromium (JS listings / WAF pages) and log its data requests")
    p.add_argument("--deep", action="store_true", help="Run platform-specific probes: submit the TaxSmart sale-date search, fetch the docaccess JSON feed")

    p = sub.add_parser("scrape-clerk", help="Scrape the non-RealAuction counties (Bid4Assets + in-person clerk lists) into a run dir")
    p.add_argument("--out", help="Run directory to write <slug>.json into (default: latest under output/runs, else a new one)")
    p.add_argument("--counties", help="Comma-separated slugs, or @file (default: every county with a collector)")
    p.add_argument("--delay", type=float, default=3.0, help="Base seconds between requests (default 3.0)")
    p.add_argument("--skip-robots", action="store_true", help="Skip robots.txt checks (fixtures/testing only)")

    p = sub.add_parser("export", help="Write data/exports feeds (tsv + json) from a run")
    p.add_argument("--run", help="Run directory (default: latest under data/runs, else output/runs)")

    p = sub.add_parser("enrich", help="Fetch appraiser pages for buy-box rows (quick-look scrub)")
    p.add_argument("--feed", default=str(ROOT / "data" / "exports" / "master_list.json"),
                   help="Feed JSON to pick targets from (default data/exports/master_list.json)")
    p.add_argument("--counties", help="Comma-separated county slugs, or @file (default: all)")
    p.add_argument("--limit", type=int, default=200, help="Max parcels to fetch this run (default 200)")
    p.add_argument("--out", help="Enrichment store (default data/enrichment.json)")
    p.add_argument("--debug-dir", help="Save first fetched HTML pages here for parser tuning")

    p = sub.add_parser("run", help="scrape + report in one command")
    add_common(p)
    p.add_argument("--counties")
    p.add_argument("--months", type=int, default=2)
    p.add_argument("--out")
    p.add_argument("--skip-robots", action="store_true")
    p.add_argument("--prev")
    p.add_argument("--buybox")
    p.add_argument("--report-out")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.cmd == "discover":
        return cmd_discover(args)
    if args.cmd == "scrape":
        cmd_scrape(args)
        return 0
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "capture":
        from .debug_capture import capture
        capture(args.url, args.out)
        return 0
    if args.cmd == "coverage":
        from .coverage_probe import probe_coverage
        probe_coverage(args.out, delay=args.delay)
        return 0
    if args.cmd == "verify-links":
        from .verify_links import verify_links
        result = verify_links(args.out, delay=args.delay)
        return 1 if result["failed"] else 0
    if args.cmd == "diagnose-enrichment":
        from .diagnose_enrichment import diagnose_enrichment
        diagnose_enrichment(args.out)
        return 0
    if args.cmd == "capture-sale-lists":
        from .capture_sale_lists import capture_sale_lists
        counties = [_slugify(c) for c in _county_names_from(args.counties)] if args.counties else None
        capture_sale_lists(args.out, delay=args.delay, counties=counties, render=args.render, deep=args.deep)
        return 0
    if args.cmd == "scrape-clerk":
        from .clerk_scrape import scrape_clerk_counties
        if args.out:
            out_dir = Path(args.out)
        else:
            runs = sorted(d for d in RUNS_ROOT.iterdir() if d.is_dir()) if RUNS_ROOT.exists() else []
            out_dir = runs[-1] if runs else RUNS_ROOT / datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        counties = [_slugify(c) for c in _county_names_from(args.counties)] if args.counties else None
        meta = scrape_clerk_counties(out_dir, counties=counties, delay=args.delay, skip_robots=args.skip_robots)
        ok = sum(1 for c in meta["counties"].values() if c["status"] == "ok")
        total = sum(c.get("records", 0) for c in meta["counties"].values())
        print(f"Clerk scrape complete: {ok} counties ok, {total} records -> {out_dir}")
        return 0
    if args.cmd == "export":
        from .exporter import export_run
        run_dir = Path(args.run) if args.run else None
        if run_dir is None:
            for root in (ROOT / "data" / "runs", RUNS_ROOT):
                if root.exists():
                    runs = sorted(d for d in root.iterdir() if (d / "run_meta.json").exists())
                    if runs:
                        run_dir = runs[-1]
                        break
        if run_dir is None:
            sys.exit("No run directories found — scrape first")
        counts = export_run(run_dir)
        print(json.dumps(counts, indent=2))
        return 0
    if args.cmd == "enrich":
        from .enrich import enrich_records
        feed_path = Path(args.feed)
        if not feed_path.exists():
            sys.exit(f"Feed not found: {feed_path} — run `python -m scraper export` first")
        records = json.loads(feed_path.read_text(encoding="utf-8")).get("records", [])
        counties = [_slugify(c) for c in _county_names_from(args.counties)] if args.counties else None
        summary = enrich_records(records, counties=counties, limit=args.limit,
                                 out_path=args.out, debug_dir=args.debug_dir)
        print(json.dumps(summary, indent=2))
        return 0
    if args.cmd == "run":
        run_dir = cmd_scrape(args)
        args.run = None
        return cmd_report(args, run_dir=run_dir)
    return 1


if __name__ == "__main__":
    sys.exit(main())

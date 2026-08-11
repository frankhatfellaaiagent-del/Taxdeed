"""Compare the current run against the previous run.

Row identity: (county, parcel/case id, sale date). Statuses:
  NEW       — not in the previous run
  CHANGED   — key fields differ (opening bid, sale time, status, address, values)
  UNCHANGED — present and identical on key fields
  REMOVED   — was in the previous run, gone now (cancelled / redeemed / sold)
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import AuctionRecord

WATCHED_FIELDS = [
    "opening_bid", "sale_time", "auction_status", "assessed_value",
    "property_address", "case_number", "certificate_number",
]


def load_run_records(run_dir: str | Path) -> list[AuctionRecord]:
    run_dir = Path(run_dir)
    records: list[AuctionRecord] = []
    for path in sorted(run_dir.glob("*.json")):
        if path.name in ("run_meta.json", "excluded_foreclosure.json", "findings.json", "status.json"):
            continue
        for row in json.loads(path.read_text(encoding="utf-8")):
            records.append(AuctionRecord.from_dict(row))
    return records


def find_previous_run(runs_root: str | Path, current: str | Path) -> Path | None:
    runs_root, current = Path(runs_root), Path(current)
    candidates = sorted(
        [d for d in runs_root.iterdir() if d.is_dir() and d.name < current.name and (d / "run_meta.json").exists()]
    )
    return candidates[-1] if candidates else None


def diff_runs(current: list[AuctionRecord], previous: list[AuctionRecord] | None) -> dict:
    """Returns {status_by_key, changes_by_key, removed_records}."""
    status: dict[tuple, str] = {}
    changes: dict[tuple, list[str]] = {}
    if previous is None:
        for r in current:
            status[r.key()] = "NEW"
        return {"status": status, "changes": changes, "removed": [], "baseline": False}

    prev_by_key = {r.key(): r for r in previous}
    cur_keys = set()
    for r in current:
        k = r.key()
        cur_keys.add(k)
        if k not in prev_by_key:
            status[k] = "NEW"
            continue
        old = prev_by_key[k]
        diffs = []
        for f in WATCHED_FIELDS:
            a, b = getattr(old, f), getattr(r, f)
            if (a or b) and a != b:
                diffs.append(f"{f}: {a!r} -> {b!r}")
        if diffs:
            status[k] = "CHANGED"
            changes[k] = diffs
        else:
            status[k] = "UNCHANGED"
    removed = [prev_by_key[k] for k in prev_by_key if k not in cur_keys]
    return {"status": status, "changes": changes, "removed": removed, "baseline": True}

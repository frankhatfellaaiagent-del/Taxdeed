"""Local dashboard: watch the automation pull data, browse latest results.

    python -m scraper dashboard [--port 8777]

Serves (stdlib only, no extra deps):
  /            the dashboard page (scraper/dashboard.html)
  /api/state   JSON: current run heartbeat + latest finished run + run history
  /files/...   read-only access to output/runs/ (e.g. the Excel report)

The page polls /api/state every 3 seconds, so a scrape started in another
terminal shows up live: per-county progress, records, errors, warnings.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = ROOT / "output" / "runs"
PAGE = Path(__file__).resolve().parent / "dashboard.html"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_state() -> dict:
    current = _read_json(ROOT / "output" / "current_run.json")
    if current and current.get("finished_at"):
        current = None  # last run completed; show it under "latest" instead

    runs = []
    if RUNS_ROOT.exists():
        for d in sorted((p for p in RUNS_ROOT.iterdir() if p.is_dir()), reverse=True)[:20]:
            meta = _read_json(d / "run_meta.json")
            if not meta:
                # A run in progress has status.json but no run_meta.json yet.
                continue
            findings = _read_json(d / "findings.json")
            counties = meta.get("counties") or {}
            runs.append({
                "name": d.name,
                "started_at": meta.get("started_at"),
                "finished_at": meta.get("finished_at"),
                "live": meta.get("live"),
                "counties_ok": sum(1 for c in counties.values() if c.get("status") == "ok"),
                "counties_error": sum(1 for c in counties.values() if c.get("status") == "error"),
                "counties_total": len(counties),
                "records": sum(c.get("auctions", 0) for c in counties.values()),
                "findings": findings,
                "county_detail": counties,
                "has_report": (d / "tax_deed_scrub.xlsx").exists(),
                "report_href": f"/files/{d.name}/tax_deed_scrub.xlsx",
            })
    latest = runs[0] if runs else None
    return {"current": current, "latest": latest, "runs": runs}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet default logging
        log.debug("dashboard: " + fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(build_state()).encode(), "application/json")
        elif path.startswith("/files/"):
            rel = unquote(path[len("/files/"):])
            target = (RUNS_ROOT / rel).resolve()
            if not str(target).startswith(str(RUNS_ROOT.resolve())) or not target.is_file():
                self._send(404, b"not found", "text/plain")
                return
            ctype = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                     if target.suffix == ".xlsx" else "application/octet-stream")
            self._send(200, target.read_bytes(), ctype)
        else:
            self._send(404, b"not found", "text/plain")


def serve(port: int = 8777):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Dashboard: http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

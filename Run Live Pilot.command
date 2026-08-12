#!/bin/bash
# Double-click me: runs the REAL scrape against the live auction site for the
# pilot county (Volusia). Needs internet. First run installs a browser (~2 min).
cd "$(dirname "$0")"
python3 -m pip install --quiet -r requirements.txt
python3 -m playwright install chromium
python3 -m scraper discover
python3 -m scraper run --counties volusia
echo
echo "Live pilot finished. Check the dashboard or the Excel file printed above."
read -r -p "Press return to close this window..."

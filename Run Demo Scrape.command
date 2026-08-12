#!/bin/bash
# Double-click me: runs a scrape against the BUNDLED SAMPLE DATA (no internet
# needed) so you can watch it move on the dashboard. Start the dashboard first
# ("Start Dashboard.command"), then run this.
cd "$(dirname "$0")"
python3 -m pip install --quiet -r requirements.txt
python3 -m scraper discover --fixtures scraper/fixtures
python3 -m scraper run --counties volusia,polk --fixtures scraper/fixtures --skip-robots
echo
echo "Demo run finished. Check the dashboard or the Excel file printed above."
read -r -p "Press return to close this window..."

#!/bin/bash
# Double-click me: starts the local dashboard, then opens it in your browser.
# Leave this window open while you use the dashboard (closing it stops the server).
cd "$(dirname "$0")"
echo "Installing/checking dependencies (first time takes a minute)..."
python3 -m pip install --quiet -r requirements.txt
( sleep 2 && open "http://127.0.0.1:8777" ) &
python3 -m scraper dashboard

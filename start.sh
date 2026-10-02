#!/bin/bash
# Starts the scout in the background with auto-restart. Safe to run again anytime.
cd /opt/data/scout
pkill -f "scout_loop" 2>/dev/null; pkill -f "scout.py" 2>/dev/null; sleep 1
nohup bash -c 'exec -a scout_loop bash -c "while true; do venv/bin/python scout.py >> scout.log 2>&1; echo restarting >> scout.log; sleep 20; done"' >/dev/null 2>&1 &
sleep 3
pgrep -f "scout.py" >/dev/null && echo "Scout running. Log: tail -n 30 /opt/data/scout/scout.log" || echo "Scout failed to start - check scout.log"

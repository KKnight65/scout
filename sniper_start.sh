#!/bin/bash
# Starts the sniper in the background with auto-restart. Safe to run again anytime.
cd /opt/data/scout
pkill -f "sniper_loop" 2>/dev/null; pkill -f "sniper.py" 2>/dev/null; sleep 1
nohup bash -c 'exec -a sniper_loop bash -c "while true; do venv/bin/python sniper.py >> sniper.log 2>&1; echo restarting >> sniper.log; sleep 20; done"' >/dev/null 2>&1 &
sleep 3
pgrep -f "venv/bin/python sniper.py" >/dev/null && echo "Sniper running. Log: tail -n 30 /opt/data/scout/sniper.log" || echo "Sniper failed to start - check sniper.log"

"""
scout_cli.py - the "hands" Hermes Agent uses to read and steer the scout.

  python scout_cli.py stats                  totals, alerts today, runners
  python scout_cli.py recent [N]             last N reviewed projects (default 30)
  python scout_cli.py runners [N]            projects that hit the runner market cap
  python scout_cli.py search <word>          find projects by name/hook
  python scout_cli.py post ape|ideas <text>  post a message to #ape or #ideas
  python scout_cli.py rule add <text>        add a rule (applies to #ape + #ideas)
  python scout_cli.py rule list | rule del <N>
  python scout_cli.py prompt                 show the main prompt
  python scout_cli.py lessons                show learned lessons
"""
import datetime as dt
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB, RULES, PROMPT, LESSONS = (BASE / n for n in ("scout.db", "my_rules.md", "prompt.md", "lessons.md"))

import os

env = dict(os.environ)
cfg = BASE / "config.env"
for line in (cfg.read_text(encoding="utf-8").splitlines() if cfg.exists() else []):
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()


def q(sql, args=()):
    con = sqlite3.connect(DB)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def usd(v):
    v = float(v or 0)
    return f"${v/1e6:.2f}M" if v >= 1e6 else f"${v/1e3:.0f}K"


def show(rows):
    for ts, p, cat, al, hook, skip, peak, x in rows:
        print(f"{ts[:16]} | {p} | {cat} | {'APE' if al else 'skip'} | peak {usd(peak) if peak else '-'} | {x or ''}\n"
              f"    {hook or skip}")


COLS = "ts,project,category,alerted,hook,skip_reason,peak_mcap,x_url"
a = sys.argv[1:] or ["help"]
cmd = a[0]

if cmd == "stats":
    day = dt.datetime.now().strftime("%Y-%m-%d")
    total = q("SELECT COUNT(*) FROM reviews")[0][0]
    today = q("SELECT COUNT(*) FROM reviews WHERE ts LIKE ?", (day + "%",))[0][0]
    apes = q("SELECT COUNT(*) FROM reviews WHERE alerted=1 AND ts LIKE ?", (day + "%",))[0][0]
    runners = q("SELECT COUNT(*) FROM reviews WHERE peak_mcap >= ?", (float(env.get("MIN_MCAP", 500000)),))[0][0]
    ape_runners = q("SELECT COUNT(*) FROM reviews WHERE alerted=1 AND peak_mcap >= ?",
                    (float(env.get("MIN_MCAP", 500000)),))[0][0]
    print(f"Reviewed total: {total} | today: {today} | #ape today: {apes}\n"
          f"Runners all-time: {runners} (of which we aped: {ape_runners})")
elif cmd == "recent":
    show(q(f"SELECT {COLS} FROM reviews ORDER BY ts DESC LIMIT ?", (int(a[1]) if len(a) > 1 else 30,)))
elif cmd == "runners":
    show(q(f"SELECT {COLS} FROM reviews WHERE peak_mcap >= ? ORDER BY peak_mcap DESC LIMIT ?",
           (float(env.get("MIN_MCAP", 500000)), int(a[1]) if len(a) > 1 else 20)))
elif cmd == "search" and len(a) > 1:
    w = f"%{' '.join(a[1:])}%"
    show(q(f"SELECT {COLS} FROM reviews WHERE project LIKE ? OR hook LIKE ? OR raw LIKE ? ORDER BY ts DESC LIMIT 20",
           (w, w, w)))
elif cmd == "post" and len(a) > 2 and a[1] in ("ape", "ideas"):
    url = env["APE_WEBHOOK_URL" if a[1] == "ape" else "IDEAS_WEBHOOK_URL"]
    text = " ".join(a[2:]).replace("\\n", "\n")
    for i in range(0, len(text), 1900):
        req = urllib.request.Request(url, data=json.dumps({"content": text[i:i + 1900]}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "scout"})
        urllib.request.urlopen(req, timeout=20)
    print(f"Posted to #{a[1]}")
elif cmd == "rule" and len(a) > 1:
    lines = RULES.read_text(encoding="utf-8").splitlines() if RULES.exists() else []
    if a[1] == "add" and len(a) > 2:
        lines.append("- " + " ".join(a[2:]))
        print("Added.")
    elif a[1] == "del" and len(a) > 2 and a[2].isdigit() and 1 <= int(a[2]) <= len(lines):
        print("Removed:", lines.pop(int(a[2]) - 1))
    RULES.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    for i, l in enumerate(lines, 1):
        print(f"{i}. {l[2:]}")
elif cmd in ("prompt", "lessons"):
    f = PROMPT if cmd == "prompt" else LESSONS
    print(f.read_text(encoding="utf-8") if f.exists() else "(empty)")
else:
    print(__doc__)

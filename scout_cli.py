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
  python scout_cli.py feedback <name> good|bad <why>   record Juan's verdict on a project (teaches the scout)
  python scout_cli.py verdicts               list Juan's verdicts
  python scout_cli.py setting <KEY> <VALUE>  change a setting and restart the scout
  python scout_cli.py formats                show formats.md (how posts look)
  python scout_cli.py audit                  everything currently in effect (run after every change)
"""
import subprocess
import datetime as dt
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB, RULES, PROMPT, LESSONS, FORMATS = (BASE / n for n in ("scout.db", "my_rules.md", "prompt.md", "lessons.md", "formats.md"))
SETTINGS = {"DAILY_ALERT_CAP", "MIN_MCAP", "DIGEST_HOUR", "TRACK_HOURS", "MCAP_EVERY_MIN", "CHECK_EVERY_MIN", "MODEL"}

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
        for col in ("juan_verdict TEXT", "juan_note TEXT"):
            try:
                con.execute(f"ALTER TABLE reviews ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        rows = con.execute(sql, args).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def restart_scout():
    subprocess.run(["bash", str(BASE / "start.sh")], check=False)


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
elif cmd == "feedback" and len(a) > 3 and a[2].lower() in ("good", "bad"):
    name, verdict, note = a[1].lstrip("@"), a[2].lower(), " ".join(a[3:])
    w = f"%{name}%"
    row = q("SELECT id, project FROM reviews WHERE project LIKE ? OR x_url LIKE ? OR key LIKE ? ORDER BY ts DESC LIMIT 1",
            (w, w, w))
    if row:
        q("UPDATE reviews SET juan_verdict=?, juan_note=? WHERE id=?", (verdict, note, row[0][0]))
        print(f"Saved: {row[0][1]} = {verdict.upper()} ({note}). The scout uses this on every future judgment.")
    else:
        q("INSERT INTO reviews(ts, grp, key, project, hook, alerted, juan_verdict, juan_note) VALUES(?,?,?,?,?,?,?,?)",
          (dt.datetime.now().isoformat(timespec="seconds"), "manual", f"manual:{name.lower()}", name, "", 0, verdict, note))
        print(f"Not in the log - saved as a manual example: {name} = {verdict.upper()} ({note}).")
elif cmd == "verdicts":
    for p, v, n in q("SELECT project, juan_verdict, juan_note FROM reviews WHERE juan_verdict IS NOT NULL ORDER BY rowid DESC"):
        print(f"{v.upper():4} | {p} | {n}")
elif cmd == "setting" and len(a) > 2:
    key, val = a[1].upper(), a[2]
    if key not in SETTINGS:
        sys.exit(f"Unknown setting. Allowed: {', '.join(sorted(SETTINGS))}")
    lines = [l for l in (cfg.read_text(encoding="utf-8").splitlines() if cfg.exists() else [])
             if not l.startswith(key + "=")]
    lines.append(f"{key}={val}")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    restart_scout()
    print(f"{key}={val} saved and scout restarted.")
elif cmd == "formats":
    print(FORMATS.read_text(encoding="utf-8") if FORMATS.exists() else "(no formats.md)")
elif cmd == "audit":
    print("=== SETTINGS ===")
    for k in sorted(SETTINGS):
        print(f"{k}={env.get(k, '(default)')}")
    print("\n=== JUAN'S RULES ===")
    print(RULES.read_text(encoding="utf-8") if RULES.exists() else "(none)")
    print("=== FORMATS ===")
    print(FORMATS.read_text(encoding="utf-8") if FORMATS.exists() else "(none)")
    print("=== VERDICTS ===")
    good = q("SELECT COUNT(*) FROM reviews WHERE juan_verdict='good'")[0][0]
    bad = q("SELECT COUNT(*) FROM reviews WHERE juan_verdict='bad'")[0][0]
    print(f"{good} good, {bad} bad")
    print("\n=== LESSONS (first 15 lines) ===")
    print("\n".join((LESSONS.read_text(encoding="utf-8") if LESSONS.exists() else "(none yet)").splitlines()[:15]))
    running = subprocess.run(["pgrep", "-f", "venv/bin/python scout.py"], capture_output=True, text=True).stdout.split()
    print(f"\n=== SCOUT PROCESS === {'running (' + str(len(running)) + ')' if running else 'NOT RUNNING'}")
else:
    print(__doc__)

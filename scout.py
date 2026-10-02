"""
Resurgence Scout
Watches 2 Telegram groups -> judges each project with a Hermes model -> posts
only the genuinely unique ones to your private Discord via webhook.
Also: logs everything, checks outcomes at 24h/72h on DexScreener, learns
(lessons.md), and posts a nightly digest with coin concepts.

Run once interactively to log in:   python scout.py
List your Telegram chats + IDs:     python scout.py --list
"""
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import httpx
from telethon import TelegramClient, events, utils
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

BASE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- config
def load_env(path: Path):
    if not path.exists():
        return  # no file: use environment variables (e.g. Zeabur Variables)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env(BASE / "config.env")
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_GROUPS = [g.strip() for g in os.environ.get("TG_GROUPS", "").split(",") if g.strip()]
APE_WEBHOOK = os.environ["APE_WEBHOOK_URL"]      # #ape   - live picks
IDEAS_WEBHOOK = os.environ["IDEAS_WEBHOOK_URL"]  # #ideas - runners + coin ideas
OR_KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = os.environ.get("MODEL", "nousresearch/hermes-4-405b")
DAILY_CAP = int(os.environ.get("DAILY_ALERT_CAP", "5"))
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "21"))  # VPS local time
CHECK_EVERY_MIN = int(os.environ.get("CHECK_EVERY_MIN", "360"))
MIN_MCAP = float(os.environ.get("MIN_MCAP", "500000"))        # runner threshold for #ideas
MCAP_EVERY_MIN = int(os.environ.get("MCAP_EVERY_MIN", "15"))  # how often to check market caps
TRACK_HOURS = int(os.environ.get("TRACK_HOURS", "72"))        # how long to watch each project

DB = BASE / "scout.db"
LESSONS = BASE / "lessons.md"

# ---------------------------------------------------------------- prompts
PROMPT_FILE = BASE / "prompt.md"   # YOUR prompt - edit anytime, used by #ape AND #ideas
RULES_FILE = BASE / "my_rules.md"  # your /rule commands - never overwritten by the model


def core_prompt():
    """Your prompt + your rules + learned lessons. Re-read on every call, so edits apply instantly."""
    base = PROMPT_FILE.read_text(encoding="utf-8") if PROMPT_FILE.exists() else ""
    rules = RULES_FILE.read_text(encoding="utf-8").strip() if RULES_FILE.exists() else ""
    return (base.strip() +
            ("\n\nJUAN'S RULES (highest priority, always follow):\n" + rules if rules else "") +
            "\n\nLESSONS FROM PAST OUTCOMES (apply these):\n" + lessons())


FILTER_TASK = """

TASK: judge the ONE project below for #ape. Reply with ONLY this JSON, no other text:
{"alert": true|false, "project": "name", "x_url": "url or empty", "hook": "one sentence describing the actual mechanism/concept/narrative", "source": "best first-party url or empty", "category": "mechanism|game|economic-loop|narrative|premise|primitive|incentive|generic", "skip_reason": "short reason if not alerting"}"""

LESSONS_PROMPT = """You maintain lessons.md for a crypto project scout. Below are the current lessons and recent reviewed projects with their real outcomes 24h/72h later (DexScreener volume, liquidity, buys/sells; "no token" means none found).

Rewrite lessons.md (max 40 lines) as concrete rules:
- which hook types/categories actually pulled buyers vs looked clever but died
- false positives to stop alerting on
- misses: skipped projects that ran, and what was overlooked
- RUNNERS: projects with peak_mcap at or above the runner threshold - what they had in common, so both #ape picks and #ideas concepts follow what actually runs
Raise the bar where alerts underperform; lower it where runners are being missed. Never contradict Juan's rules. Output only the new lessons.md text."""

CONCEPT_PROMPT = """You help Juan create original memecoin concepts to launch on hood.fun (Robinhood Chain). Use the reviewed projects and lessons below to identify which HOOK TYPES are winning, then write the requested output.

Rules: be original - never reuse another project's name, ticker, art, branding or copy. Borrow the pattern (loop, incentive, narrative angle) and make it new or better. It must work as a simple token launch on hood.fun; if the mechanism needs a custom contract, say so and give the simplest version that launches today. Each concept needs a clear reason to buy and a clear reason to share.

Concept format (plain text, Discord markdown ok):
**CONCEPT: Name ($TICKER)**
Hook: one sentence - why people buy
Inspired by pattern: hook type + 1-2 examples from the log
Mechanic: how it works at launch
Why it can beat the originals: one sentence
PFP prompt: ...
Banner prompt: ...
Site headline + subline: ...
Launch X posts: 3 posts under 200 chars
Risk: the one thing most likely to make it flop"""

RUNNER_PROMPT = CONCEPT_PROMPT + """

You are given ONE project that actually ran (hit the market cap shown). Output, in this order:
**What it is:** one sentence
**Why it ran:** 2-3 bullets - the real hook, the incentive, the narrative timing
**Pattern to copy:** one sentence naming the reusable pattern
Then 2 concepts in the concept format above."""

# ---------------------------------------------------------------- helpers
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
X_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])", re.I)
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
SKIP_SITE = ("x.com", "twitter.com", "t.me", "telegram", "dexscreener", "dextools", "birdeye",
             "gmgn", "pump.fun", "photon", "bullx", "axiom", "discord", "youtube", "tiktok",
             "instagram", "medium.com", "linktr.ee", "geckoterminal", "coingecko", "etherscan",
             "solscan", "basescan", "hood.fun")
X_NON_PROFILE = {"i", "home", "search", "intent", "share", "hashtag", "explore", "settings"}

http: httpx.AsyncClient = None  # set in main
llm_sem = asyncio.Semaphore(3)


def now():
    return dt.datetime.now()


def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS reviews(
        id INTEGER PRIMARY KEY, ts TEXT, grp TEXT, key TEXT UNIQUE, project TEXT,
        x_url TEXT, site TEXT, ca TEXT, category TEXT, hook TEXT, skip_reason TEXT,
        alerted INTEGER, raw TEXT, chk24 TEXT, chk72 TEXT)""")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    for col in ("peak_mcap REAL", "ideas_posted INTEGER DEFAULT 0", "last_search TEXT"):
        try:
            con.execute(f"ALTER TABLE reviews ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return con


def meta_get(k):
    with db() as con:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(k, v):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))


def lessons():
    return LESSONS.read_text(encoding="utf-8") if LESSONS.exists() else "(none yet)"


def extract(msg):
    text = msg.message or ""
    urls = set(URL_RE.findall(text))
    for ent in msg.entities or []:
        if isinstance(ent, MessageEntityTextUrl):
            urls.add(ent.url)
        elif isinstance(ent, MessageEntityUrl):
            u = text[ent.offset: ent.offset + ent.length]
            urls.add(u if u.startswith("http") else "https://" + u)
    if msg.reply_markup and getattr(msg.reply_markup, "rows", None):
        for row in msg.reply_markup.rows:
            for b in row.buttons:
                if getattr(b, "url", None):
                    urls.add(b.url)
    blob = text + "\n" + "\n".join(urls)
    x_url = ""
    for m in X_RE.finditer(blob):
        if m.group(1).lower() not in X_NON_PROFILE:
            x_url = f"https://x.com/{m.group(1)}"
            break
    site = next((u for u in urls if not any(s in u.lower() for s in SKIP_SITE)), "")
    ca = ""
    m = EVM_RE.search(blob)
    if m:
        ca = m.group(0)
    else:
        for c in SOL_RE.findall(text):
            if any(ch.isdigit() for ch in c) and any(ch.isupper() for ch in c):
                ca = c
                break
    return text, sorted(urls), x_url, site, ca


async def fetch_site(url):
    try:
        r = await http.get(url, timeout=15, follow_redirects=True,
                           headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        h = r.text
        title = re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I)
        desc = re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']+)', h, re.I)
        h = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()
        parts = [f"TITLE: {title.group(1).strip()}" if title else "",
                 f"DESCRIPTION: {desc.group(1).strip()}" if desc else "", body]
        return "\n".join(p for p in parts if p)[:4000]
    except Exception as e:
        return f"(could not load site: {type(e).__name__})"


async def llm(system, user, max_tokens=900, temp=0.3):
    async with llm_sem:
        for attempt in range(3):
            try:
                r = await http.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OR_KEY}"},
                    json={"model": MODEL, "temperature": temp, "max_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": user}]},
                    timeout=120)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"] or ""
            except Exception as e:
                print(f"[llm] attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(5 * (attempt + 1))
    return ""


def parse_json(s):
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def discord(text, hook=None):
    hook = hook or APE_WEBHOOK
    for i in range(0, len(text), 1900):
        chunk = text[i:i + 1900]
        for _ in range(3):
            r = await http.post(hook, json={"content": chunk, "allowed_mentions": {"parse": []}}, timeout=20)
            if r.status_code == 429:
                await asyncio.sleep(float(r.json().get("retry_after", 2)))
                continue
            if r.status_code >= 300:
                print(f"[discord] {r.status_code}: {r.text[:200]}")
            break


def alerts_today():
    day = now().strftime("%Y-%m-%d")
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM reviews WHERE alerted=1 AND ts LIKE ?", (day + "%",)).fetchone()[0]


# ---------------------------------------------------------------- core
async def handle(msg, grp):
    text, urls, x_url, site, ca = extract(msg)
    if not (x_url or site or ca):
        return  # nothing to judge
    key = (x_url.lower() or ca.lower() or re.sub(r"^https?://(www\.)?", "", site).split("/")[0].lower()
           or hashlib.md5(text.encode()).hexdigest())
    with db() as con:
        if con.execute("SELECT 1 FROM reviews WHERE key=?", (key,)).fetchone():
            return  # already reviewed

    site_text = await fetch_site(site) if site else "(no first-party site linked)"
    user = (f"TELEGRAM POST ({grp}):\n{text[:3000]}\n\nLINKS:\n" + "\n".join(urls[:20]) +
            f"\n\nCA: {ca or 'none'}\n\nFIRST-PARTY SITE ({site or 'none'}):\n{site_text}")
    out = parse_json(await llm(core_prompt() + FILTER_TASK, user, max_tokens=400, temp=0.2))
    if not out:
        print(f"[skip] bad model output for {key}")
        return

    alert = bool(out.get("alert")) and bool(out.get("hook"))
    if alert and alerts_today() >= DAILY_CAP:
        alert = False
        out["skip_reason"] = "daily cap reached: " + out.get("hook", "")

    with db() as con:
        try:
            con.execute("""INSERT INTO reviews(ts,grp,key,project,x_url,site,ca,category,hook,skip_reason,alerted,raw)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (now().isoformat(timespec="seconds"), grp, key, out.get("project", ""),
                         out.get("x_url") or x_url, site, ca, out.get("category", ""), out.get("hook", ""),
                         out.get("skip_reason", ""), int(alert), text[:2000]))
        except sqlite3.IntegrityError:
            return

    name = out.get("project") or "Unknown"
    print(f"[{'ALERT' if alert else 'skip '}] {name} - {out.get('hook') or out.get('skip_reason')}")
    if alert:
        await discord(f"**{name} — UNIQUE WATCH**\n"
                      f"X: {out.get('x_url') or x_url or 'n/a'}\n"
                      f"Why it is different: {out['hook']}\n"
                      f"Source: {out.get('source') or site or 'n/a'}")


async def dex(ca, project):
    try:
        if ca:
            r = await http.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=20)
            pairs = r.json().get("pairs") or []
        else:
            r = await http.get("https://api.dexscreener.com/latest/dex/search", params={"q": project}, timeout=20)
            p = project.lower().replace(" ", "")
            pairs = [x for x in (r.json().get("pairs") or [])
                     if p and p in (x["baseToken"]["name"] + x["baseToken"]["symbol"]).lower().replace(" ", "")]
        if not pairs:
            return {"token": False}
        top = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
        tx = (top.get("txns") or {}).get("h24") or {}
        return {"token": True, "chain": top.get("chainId"), "vol24": (top.get("volume") or {}).get("h24"),
                "liq": (top.get("liquidity") or {}).get("usd"), "fdv": top.get("fdv"),
                "buys24": tx.get("buys"), "sells24": tx.get("sells"),
                "chg24": (top.get("priceChange") or {}).get("h24")}
    except Exception as e:
        return {"error": type(e).__name__}


def fmt_usd(v):
    v = float(v or 0)
    return f"${v/1e6:.2f}M" if v >= 1e6 else f"${v/1e3:.0f}K"


async def dex_batch(cas):
    """market cap per CA (max of all its pairs). DexScreener allows 30 per call."""
    out = {}
    for i in range(0, len(cas), 30):
        chunk = cas[i:i + 30]
        try:
            r = await http.get("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk), timeout=20)
            for pr in r.json().get("pairs") or []:
                addr = pr["baseToken"]["address"].lower()
                mc = pr.get("marketCap") or pr.get("fdv") or 0
                if mc > out.get(addr, (0,))[0]:
                    out[addr] = (mc, pr.get("url", ""), pr.get("chainId", ""))
        except Exception as e:
            print(f"[mcap] {e}")
        await asyncio.sleep(1)
    return out


async def find_ca(rid, project):
    """pre-launch projects: look for a token by name every 2h."""
    with db() as con:
        con.execute("UPDATE reviews SET last_search=? WHERE id=?", (now().isoformat(timespec="seconds"), rid))
    if not project or len(project) < 3:
        return ""
    try:
        r = await http.get("https://api.dexscreener.com/latest/dex/search", params={"q": project}, timeout=20)
        p = project.lower().replace(" ", "")
        for pr in r.json().get("pairs") or []:
            b = pr["baseToken"]
            if p in (b["name"].lower().replace(" ", ""), b["symbol"].lower()):
                with db() as con:
                    con.execute("UPDATE reviews SET ca=? WHERE id=?", (b["address"], rid))
                return b["address"]
    except Exception as e:
        print(f"[search] {e}")
    return ""


async def post_runner(rid):
    with db() as con:
        row = con.execute("SELECT project,x_url,site,ca,hook,raw,peak_mcap,category FROM reviews WHERE id=?",
                          (rid,)).fetchone()
        con.execute("UPDATE reviews SET ideas_posted=1 WHERE id=?", (rid,))
    project, x_url, site, ca, hook, raw, peak, cat = row
    site_text = await fetch_site(site) if site else "(no site)"
    info = (f"PROJECT: {project}\nPEAK MARKET CAP: {fmt_usd(peak)}\nX: {x_url}\nSITE: {site}\nCATEGORY: {cat}\n"
            f"HOOK NOTE: {hook}\nORIGINAL POST:\n{(raw or '')[:1500]}\nSITE TEXT:\n{site_text[:2500]}\n\n"
            )
    out = await llm(core_prompt() + "\n\n---\n" + RUNNER_PROMPT, info, max_tokens=2000, temp=0.8)
    header = (f"**🚀 {project or 'Unknown'} hit {fmt_usd(peak)} mcap**\n"
              f"X: {x_url or 'n/a'} | Site: {site or 'n/a'}\nCA: `{ca}`\n")
    await discord(header + (out.strip() or "(model returned nothing)"), IDEAS_WEBHOOK)
    print(f"[runner] {project} {fmt_usd(peak)} -> #ideas")
    await update_lessons()


async def mcap_loop():
    while True:
        try:
            since = (now() - dt.timedelta(hours=TRACK_HOURS)).isoformat(timespec="seconds")
            two_h = (now() - dt.timedelta(hours=2)).isoformat(timespec="seconds")
            with db() as con:
                rows = con.execute("SELECT id, ca, project, peak_mcap, ideas_posted, last_search FROM reviews "
                                   "WHERE ts >= ?", (since,)).fetchall()
            for rid, ca, project, _, _, last in rows:
                if not ca and (not last or last <= two_h):
                    await find_ca(rid, project)
                    await asyncio.sleep(1)
            with db() as con:
                rows = con.execute("SELECT id, ca, peak_mcap, ideas_posted FROM reviews "
                                   "WHERE ts >= ? AND ca != ''", (since,)).fetchall()
            caps = await dex_batch(sorted({r[1] for r in rows}))
            for rid, ca, peak, posted in rows:
                mc = caps.get(ca.lower(), (0,))[0]
                if mc > (peak or 0):
                    with db() as con:
                        con.execute("UPDATE reviews SET peak_mcap=? WHERE id=?", (mc, rid))
                    peak = mc
                if (peak or 0) >= MIN_MCAP and not posted:
                    await post_runner(rid)
        except Exception as e:
            print(f"[mcap] {type(e).__name__}: {e}")
        await asyncio.sleep(MCAP_EVERY_MIN * 60)


async def outcome_loop():
    while True:
        try:
            changed = False
            for col, hours in (("chk24", 24), ("chk72", 72)):
                cutoff = (now() - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
                with db() as con:
                    rows = con.execute(f"SELECT id, ca, project FROM reviews WHERE {col} IS NULL AND ts <= ? "
                                       "ORDER BY ts DESC LIMIT 200", (cutoff,)).fetchall()
                for rid, ca, project in rows:
                    res = await dex(ca, project or "")
                    with db() as con:
                        con.execute(f"UPDATE reviews SET {col}=? WHERE id=?", (json.dumps(res), rid))
                    changed = True
                    await asyncio.sleep(1.2)  # stay under DexScreener rate limits
            if changed:
                await update_lessons()
        except Exception as e:
            print(f"[outcomes] {e}")
        await asyncio.sleep(CHECK_EVERY_MIN * 60)


def recent_log(days=7, limit=200, outcomes=True):
    since = (now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    with db() as con:
        rows = con.execute("SELECT ts,project,category,alerted,hook,skip_reason,chk24,chk72,peak_mcap FROM reviews "
                           "WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, limit)).fetchall()
    lines = []
    for ts, p, cat, al, hook, skip, c24, c72, peak in rows:
        line = f"{ts[:16]} | {p} | {cat} | {'ALERTED' if al else 'skipped'} | {hook or skip}"
        if outcomes:
            line += f" | peak_mcap={fmt_usd(peak) if peak else '-'} | 24h={c24 or '-'} | 72h={c72 or '-'}"
        lines.append(line)
    return "\n".join(lines) or "(no reviews yet)"


async def update_lessons():
    rules = RULES_FILE.read_text(encoding="utf-8") if RULES_FILE.exists() else "(none)"
    out = await llm(LESSONS_PROMPT, f"RUNNER THRESHOLD: {fmt_usd(MIN_MCAP)}\nJUAN'S RULES:\n{rules}\n\nCURRENT LESSONS:\n{lessons()}\n\nRECENT PROJECTS:\n{recent_log()}",
                    max_tokens=900)
    if out.strip():
        LESSONS.write_text(out.strip() + "\n", encoding="utf-8")
        print("[lessons] updated")


async def concepts(n=3, digest=False):
    ask = (f"Write a DAILY DIGEST: (1) top 3 hook types today with 1 example each, (2) what's working per the "
           f"lessons, (3) {n} coin concepts." if digest else f"Write {n} coin concepts.")
    out = await llm(core_prompt() + "\n\n---\n" + CONCEPT_PROMPT,
                    f"{ask}\n\nREVIEWED PROJECTS (last 7 days):\n{recent_log(limit=150)}",
                    max_tokens=2500, temp=0.8)
    if out.strip():
        await discord(("**📊 DAILY DIGEST**\n" if digest else "**💡 CONCEPTS**\n") + out.strip(), IDEAS_WEBHOOK)


async def digest_loop():
    while True:
        t = now()
        today = t.strftime("%Y-%m-%d")
        if t.hour >= DIGEST_HOUR and meta_get("last_digest") != today:
            meta_set("last_digest", today)
            try:
                await concepts(3, digest=True)
            except Exception as e:
                print(f"[digest] {e}")
        await asyncio.sleep(60)


# ---------------------------------------------------------------- main
async def main():
    global http
    http = httpx.AsyncClient()
    client = TelegramClient(str(BASE / "scout_session"), TG_API_ID, TG_API_HASH)
    await client.start()

    if "--list" in sys.argv:
        async for d in client.iter_dialogs():
            if d.is_group or d.is_channel:
                print(f"{d.id}\t{d.name}")
        return

    chats = []
    for g in TG_GROUPS:
        try:
            chats.append(await client.get_entity(int(g) if g.lstrip("-").isdigit() else g))
        except Exception as e:
            sys.exit(f"Can't open group '{g}': {e}\nRun: python scout.py --list and use the numeric ID.")
    names = {utils.get_peer_id(c): getattr(c, "title", str(c.id)) for c in chats}
    print("Watching:", ", ".join(names.values()))

    @client.on(events.NewMessage(chats=chats))
    async def on_msg(ev):
        asyncio.create_task(safe_handle(ev.message, names.get(ev.chat_id, "group")))

    # control commands: send these to your own Telegram "Saved Messages"
    @client.on(events.NewMessage(chats="me", pattern=r"^/(concepts|status|lessons|rules|rule|unrule)\b"))
    async def on_cmd(ev):
        cmd = ev.pattern_match.group(1)
        if cmd == "concepts":
            await ev.reply("Generating... check Discord.")
            await concepts(3)
        elif cmd == "status":
            with db() as con:
                total = con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            await ev.reply(f"Running. Reviewed {total} projects total, {alerts_today()} alerts today.")
        elif cmd == "lessons":
            await ev.reply(lessons()[:4000])
        elif cmd == "rule":
            txt = ev.raw_text.split(None, 1)[1].strip() if len(ev.raw_text.split(None, 1)) > 1 else ""
            if txt:
                with open(RULES_FILE, "a", encoding="utf-8") as f:
                    f.write(f"- {txt}\n")
                await ev.reply(f"Rule added. Applies to #ape and #ideas from now on:\n- {txt}")
        elif cmd == "rules":
            r = RULES_FILE.read_text(encoding="utf-8") if RULES_FILE.exists() else ""
            await ev.reply("\n".join(f"{i}. {l[2:]}" for i, l in enumerate(r.splitlines(), 1)) or "No rules yet.")
        elif cmd == "unrule":
            parts = ev.raw_text.split()
            lines = RULES_FILE.read_text(encoding="utf-8").splitlines() if RULES_FILE.exists() else []
            if len(parts) > 1 and parts[1].isdigit() and 1 <= int(parts[1]) <= len(lines):
                gone = lines.pop(int(parts[1]) - 1)
                RULES_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
                await ev.reply(f"Removed: {gone[2:]}")

    await discord("🟢 Scout online (#ape) - watching: " + ", ".join(names.values()))
    await discord("🟢 Scout online (#ideas) - runners over " + fmt_usd(MIN_MCAP) + " land here", IDEAS_WEBHOOK)
    asyncio.create_task(outcome_loop())
    asyncio.create_task(mcap_loop())
    asyncio.create_task(digest_loop())
    await client.run_until_disconnected()


async def safe_handle(msg, grp):
    try:
        await handle(msg, grp)
    except Exception as e:
        print(f"[handle] {type(e).__name__}: {e}")


if __name__ == "__main__":
    db().close()
    asyncio.run(main())

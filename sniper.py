"""
sniper.py - auto-buy coins from accounts your watched X accounts follow.

Flow:
  1. A Telegram tracker bot posts alerts into a chat your alt account can see, both when a
     watched account FOLLOWS someone and when it POSTS.
  2. POST alert: any Solana contract address (CA) in the post (alert text or the full tweet)
     is bought right away.
  3. FOLLOW alert: the newly followed account's bio, website and recent posts are checked for
     a CA. If none is there yet, it re-checks every few minutes for WATCH_HOURS.
  3. A CA that passes the safety checks is sent to Bloom (Solana or EVM bot), which auto-buys
     with the amount set in Bloom's own auto-buy settings.
  4. Every buy (or would-be buy in DRY_RUN) is posted to Discord.

First run (login):  venv/bin/python sniper.py --login
List chats + IDs:   venv/bin/python sniper.py --list
"""
import asyncio
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import httpx
from telethon import TelegramClient, events, utils

BASE = Path(__file__).resolve().parent


def load_env(path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env(BASE / "config.env")
E = os.environ.get
TG_API_ID, TG_API_HASH = int(E("TG_API_ID", "0")), E("TG_API_HASH", "")
TRACKER_CHATS = [c.strip() for c in E("TRACKER_CHATS", "").split(",") if c.strip()]
WATCH = {h.strip().lstrip("@").lower() for h in E("WATCH_ACCOUNTS", "").split(",") if h.strip()}
BLOOM_SOL_BOT = E("BLOOM_SOL_BOT", "")          # e.g. the Bloom Solana bot's @username
BLOOM_EVM_BOT = E("BLOOM_EVM_BOT", "")          # e.g. the Bloom EVM bot's @username
CHAINS = {c.strip().lower() for c in E("CHAINS", "solana").split(",") if c.strip()}
EVM_CHAIN_MATCH = E("EVM_CHAIN_MATCH", "robinhood").lower()  # DexScreener chainId must contain this
DRY_RUN = E("DRY_RUN", "true").lower() == "true"
MAX_BUYS_PER_HOUR = int(E("MAX_BUYS_PER_HOUR", "3"))
MAX_BUYS_PER_DAY = int(E("MAX_BUYS_PER_DAY", "10"))
WATCH_HOURS = float(E("WATCH_HOURS", "6"))
RECHECK_MIN = float(E("RECHECK_MIN", "3"))
MIN_LIQ_USD = float(E("MIN_LIQ_USD", "0"))      # 0 = allow brand-new tokens with no pool yet
MAX_MCAP_USD = float(E("MAX_MCAP_USD", "1000000"))  # skip coins already bigger than this (0 = no cap)
SOL_RPC = E("SOL_RPC", "https://api.mainnet-beta.solana.com")
WEBHOOK = E("SNIPER_WEBHOOK_URL") or E("APE_WEBHOOK_URL", "")

DB = BASE / "sniper.db"
KOL_RE = re.compile(r"\(@([A-Za-z0-9_]{1,15})\)")
HANDLE_FIELD_RE = re.compile(r"Handle\s*[:\n]\s*@?([A-Za-z0-9_]{1,15})\b")
FOLLOW_RE = re.compile(r"\b(followed|follows|is now following|now following|started following|new follow(?:ing)?)\b", re.I)
TWEET_RE = re.compile(r"(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/(\d+)", re.I)
X_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])", re.I)
AT_RE = re.compile(r"(?<![\w/])@([A-Za-z0-9_]{1,15})\b")
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
NOT_HANDLES = {"i", "home", "search", "intent", "share", "hashtag", "explore", "settings", "status"}
# well-known addresses that are never "the new coin"
IGNORE_CAS = {
    "so11111111111111111111111111111111111111112",   # wSOL
    "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v",  # USDC
    "es9vmfrzacermjfrf4h2fyd4kconky11mccce8benwnyb",  # USDT
    "0x0000000000000000000000000000000000000000",
}

http: httpx.AsyncClient = None
client: TelegramClient = None


def now():
    return dt.datetime.now(dt.timezone.utc)


def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS buys(ca TEXT PRIMARY KEY, ts TEXT, handle TEXT, chain TEXT, src TEXT, dry INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS watching(handle TEXT PRIMARY KEY, since TEXT, by_kol TEXT, done INTEGER DEFAULT 0)")
    return con


def find_cas(text):
    """Return CAs found in text as (ca, kind) with kind 'sol' or 'evm'."""
    out = []
    for m in EVM_RE.findall(text or ""):
        out.append((m, "evm"))
    for m in SOL_RE.findall(text or ""):
        # real Solana mints mix digits and upper+lower case; filters out normal words
        if any(c.isdigit() for c in m) and any(c.isupper() for c in m) and any(c.islower() for c in m):
            out.append((m, "sol"))
    seen, res = set(), []
    for ca, k in out:
        if ca.lower() not in IGNORE_CAS and ca not in seen:
            seen.add(ca)
            res.append((ca, k))
    return res


def handles_in(msg):
    text = msg.message or ""
    urls = [getattr(e, "url", "") for e in (msg.entities or []) if getattr(e, "url", None)]
    if msg.reply_markup and getattr(msg.reply_markup, "rows", None):
        urls += [b.url for r in msg.reply_markup.rows for b in r.buttons if getattr(b, "url", None)]
    blob = text + "\n" + "\n".join(urls)
    found = []
    for h in X_RE.findall(blob) + AT_RE.findall(text):
        h = h.lower()
        if h not in NOT_HANDLES and h not in found:
            found.append(h)
    return text + "\n" + "\n".join(urls), found


async def x_profile(handle):
    """Bio + website + recent posts text via the free fxtwitter API (best effort)."""
    parts = []
    try:
        r = await http.get(f"https://api.fxtwitter.com/{handle}", timeout=15)
        u = (r.json() or {}).get("user") or {}
        parts += [u.get("description") or "", (u.get("website") or {}).get("url") or "",
                  (u.get("website") or {}).get("display_url") or "", u.get("location") or ""]
    except Exception as e:
        print(f"[x] profile {handle}: {type(e).__name__}")
    try:
        r = await http.get(f"https://api.fxtwitter.com/2/profile/{handle}/statuses", timeout=15)
        data = r.json() or {}
        for st in (data.get("results") or data.get("statuses") or [])[:20]:
            parts.append(st.get("text") or (st.get("raw_text") or {}).get("text") or "")
    except Exception:
        pass  # timeline endpoint is optional; bio + alert text still work
    return "\n".join(p for p in parts if p)


async def tweet_text(tweet_id):
    """Full text of one tweet (plus any quoted tweet) via fxtwitter."""
    try:
        r = await http.get(f"https://api.fxtwitter.com/status/{tweet_id}", timeout=15)
        t = (r.json() or {}).get("tweet") or {}
        q = t.get("quote") or {}
        return "\n".join(x for x in [t.get("text") or "", q.get("text") or ""] if x)
    except Exception as e:
        print(f"[x] tweet {tweet_id}: {type(e).__name__}")
        return ""


async def is_sol_mint(ca):
    """True = confirmed token mint, False = confirmed NOT a mint (e.g. a wallet), None = couldn't check."""
    try:
        r = await http.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                           "params": [ca, {"encoding": "jsonParsed"}]}, timeout=10)
        v = (r.json().get("result") or {}).get("value")
        if v is None:
            return False  # account doesn't exist
        data = v.get("data")
        return isinstance(data, dict) and (data.get("parsed") or {}).get("type") == "mint"
    except Exception:
        return None


async def dex_info(ca):
    try:
        r = await http.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=15)
        pairs = r.json().get("pairs") or []
    except Exception:
        return None
    if not pairs:
        return {"listed": False}
    top = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return {"listed": True, "chain": top.get("chainId", ""), "liq": (top.get("liquidity") or {}).get("usd") or 0,
            "name": top["baseToken"].get("name"), "symbol": top["baseToken"].get("symbol"),
            "mcap": top.get("marketCap") or top.get("fdv") or 0, "url": top.get("url")}


async def discord(text):
    if WEBHOOK:
        try:
            await http.post(WEBHOOK, json={"content": text[:1900], "allowed_mentions": {"parse": []}}, timeout=15)
        except Exception as e:
            print(f"[discord] {e}")


def counts():
    with db() as con:
        h = con.execute("SELECT COUNT(*) FROM buys WHERE dry=? AND ts>=?",
                        (int(DRY_RUN), (now() - dt.timedelta(hours=1)).isoformat())).fetchone()[0]
        d = con.execute("SELECT COUNT(*) FROM buys WHERE dry=? AND ts>=?",
                        (int(DRY_RUN), (now() - dt.timedelta(days=1)).isoformat())).fetchone()[0]
    return h, d


async def try_buy(ca, kind, handle, src):
    with db() as con:
        if con.execute("SELECT 1 FROM buys WHERE ca=?", (ca,)).fetchone():
            return True  # already bought this CA
    info = await dex_info(ca)
    chain = "solana" if kind == "sol" else ""
    if kind == "evm":
        if not info or not info.get("listed"):
            print(f"[skip] {ca} EVM but not on DexScreener yet - can't confirm chain")
            return False
        if EVM_CHAIN_MATCH not in info["chain"].lower():
            print(f"[skip] {ca} on {info['chain']}, not {EVM_CHAIN_MATCH}")
            return True
        chain = info["chain"]
    if (chain == "solana" and "solana" not in CHAINS) or (kind == "evm" and not ({"robinhood", "evm"} & CHAINS)):
        return True
    if kind == "sol":
        mint = await is_sol_mint(ca)
        if mint is False:
            print(f"[skip] {ca} is not a token mint (wallet or other address)")
            return False
    if MAX_MCAP_USD and info and info.get("listed") and info["mcap"] > MAX_MCAP_USD:
        print(f"[skip] {ca} mcap ${info['mcap']:,.0f} > ${MAX_MCAP_USD:,.0f}")
        return True
    if info and info.get("listed") and info["liq"] < MIN_LIQ_USD:
        print(f"[skip] {ca} liquidity ${info['liq']:.0f} < ${MIN_LIQ_USD:.0f}")
        return False
    h, d = counts()
    if h >= MAX_BUYS_PER_HOUR or d >= MAX_BUYS_PER_DAY:
        await discord(f"⛔ Sniper limit hit ({h}/h, {d}/day). Skipped `{ca}` from @{handle}.")
        return True
    bot = BLOOM_SOL_BOT if kind == "sol" else BLOOM_EVM_BOT
    if not bot:
        print(f"[skip] no Bloom bot set for {kind}")
        return True
    if not DRY_RUN:
        await client.send_message(bot, ca)
    with db() as con:
        con.execute("INSERT OR IGNORE INTO buys VALUES(?,?,?,?,?,?)",
                    (ca, now().isoformat(), handle, chain, src, int(DRY_RUN)))
    name = f"{info.get('name')} (${info.get('symbol')}) " if info and info.get("listed") else ""
    await discord(f"{'🧪 DRY RUN - would buy' if DRY_RUN else '🟢 BOUGHT via Bloom'}: {name}`{ca}`\n"
                  f"Chain: {chain} | Found in: {src} of https://x.com/{handle}"
                  + (f" | mcap ${info['mcap']:,.0f}" if info and info.get("listed") else " | not on DexScreener yet")
                  + (f"\n{info['url']}" if info and info.get("url") else ""))
    print(f"[{'dry' if DRY_RUN else 'BUY'}] {ca} from @{handle} ({src})")
    return True


async def check_handle(handle, alert_text=""):
    """Look for a CA for this account. Returns True when finished (bought or rejected)."""
    for ca, kind in find_cas(alert_text):
        if await try_buy(ca, kind, handle, "tracker alert"):
            return True
    text = await x_profile(handle)
    for ca, kind in find_cas(text):
        if await try_buy(ca, kind, handle, "bio/posts"):
            return True
    return False


async def on_alert(msg):
    text, hs = handles_in(msg)
    # Tweet Catcher format: first line "Name - (@kolhandle)", then "[ New Post ]" / "[ New Following ]"
    m = KOL_RE.search(text)
    kol = m.group(1).lower() if m else next((h for h in hs if h in WATCH), hs[0] if hs else "?")
    if WATCH and kol not in WATCH:
        return  # alert about someone not on the watch list
    is_follow = bool(re.search(r"\[\s*New Follow", text, re.I) or FOLLOW_RE.search(text))

    # 1) Any CA in the alert itself or in the full tweet it links to -> buy (covers normal posts)
    post_text = text
    for tid in dict.fromkeys(TWEET_RE.findall(text)):
        post_text += "\n" + await tweet_text(tid)
    cas = find_cas(post_text)
    if cas:
        print(f"[post] @{kol}: {len(cas)} CA(s) in alert/tweet")
    for ca, kind in cas:
        await try_buy(ca, kind, kol, "follow alert" if is_follow else "post")
    if not is_follow:
        return

    # 2) FOLLOW alert: check the newly followed account (bio, website, posts)
    hm = HANDLE_FIELD_RE.search(text)
    if hm:
        target = hm.group(1).lower()
    else:
        new = [h for h in hs if h != kol and h not in WATCH]
        if not new:
            return
        target = new[-1]  # in "X followed Y" alerts the followed account comes last
    print(f"[follow] @{kol} -> @{target}")
    with db() as con:
        con.execute("INSERT OR IGNORE INTO watching(handle, since, by_kol) VALUES(?,?,?)",
                    (target, now().isoformat(), kol))
    if await check_handle(target, text):
        with db() as con:
            con.execute("UPDATE watching SET done=1 WHERE handle=?", (target,))


async def recheck_loop():
    while True:
        await asyncio.sleep(RECHECK_MIN * 60)
        cutoff = (now() - dt.timedelta(hours=WATCH_HOURS)).isoformat()
        with db() as con:
            rows = con.execute("SELECT handle FROM watching WHERE done=0 AND since>=?", (cutoff,)).fetchall()
        for (h,) in rows:
            try:
                if await check_handle(h):
                    with db() as con:
                        con.execute("UPDATE watching SET done=1 WHERE handle=?", (h,))
            except Exception as e:
                print(f"[recheck] {h}: {e}")
            await asyncio.sleep(2)


async def main():
    global http, client
    http = httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
    client = TelegramClient(str(BASE / "sniper_session"), TG_API_ID, TG_API_HASH)
    await client.start()
    if "--login" in sys.argv:
        print("Logged in as", (await client.get_me()).username)
        return
    if "--list" in sys.argv:
        async for d in client.iter_dialogs():
            print(f"{d.id}\t{d.name}")
        return
    if not TRACKER_CHATS:
        sys.exit("Set TRACKER_CHATS in config.env (run with --list to find the ID).")
    chats = [await client.get_entity(int(c) if c.lstrip("-").isdigit() else c) for c in TRACKER_CHATS]

    @client.on(events.NewMessage(chats=chats))
    async def handler(ev):
        try:
            await on_alert(ev.message)
        except Exception as e:
            print(f"[alert] {type(e).__name__}: {e}")

    db().close()
    names = ", ".join(getattr(c, "title", None) or getattr(c, "username", "") or str(c.id) for c in chats)
    await discord(f"🎯 Sniper online ({'DRY RUN' if DRY_RUN else 'LIVE'}) - tracker: {names} | "
                  f"watching {len(WATCH) or 'all'} accounts | max {MAX_BUYS_PER_HOUR}/h, {MAX_BUYS_PER_DAY}/day")
    asyncio.create_task(recheck_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

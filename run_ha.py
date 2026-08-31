#!/usr/bin/env python3
"""B6-2 均K（Heikin-Ashi）｜o3333o — 獨立進程
規格：
  1. 每根 K 線收線後 +5 秒抓 K 線，算 HA，判燈號。
  2. 進場：PRE根反轉前色 + POST根反轉後色 + POST振幅累加達門檻 -> taker 市價進場。
  3. 出場：EXIT根反向色 + 振幅累加達門檻 -> taker 市價平倉。無 TP/SL/TE。
  4. clOrdId 前綴：進場 h / 出場 y（普K 用 n / x，互不干擾）。
  5. 心跳：超過 3 根 K 線未推進燈號判定即 TG 告警。
  6. OKX 為唯一真相來源；重啟接管既有持倉；Telegram 為旁路，發送失敗不影響交易。
  7. 週期 5m/10m/15m/30m/60m/120m/240m/480m/720m/1440m；非原生者由底層 bar 合成。
     邊界以 UTC+8 對齊（與 OKX App 的 12H/1D 日界一致）。
注意：本檔完全獨立於 run_bot.py（普K），使用自己的 bot token 與 state 檔。
"""
import sys, hmac, base64, hashlib, json, time, asyncio, uuid, os, sqlite3
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN
from datetime import datetime, timezone, timedelta
import httpx
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, MessageHandler, filters
sys.path.insert(0, "/srv/1111bot")
from app.core import emoji as E
from app.strategy.ha import calc_ha

BASE = "https://www.okx.com"
ACCT = "o3333o"
TZ8 = timezone(timedelta(hours=8))
ACCOUNT_TF = "5m"
STATE_FILE = "/srv/1111bot/data/strategies_ha_o3333o.json"
HA_LAG = 5           # 收線後幾秒才抓 K 線（等 OKX 資料落定）
HA_HIST = 120        # 抓幾根歷史 K 線做 HA 遞迴
HB_BARS = 3          # 心跳超過幾根 K 線未推進就告警
HA_MAX  = 2000       # /ha 單次最多抓幾根
DB_FILE = "/srv/1111bot/data/ha_market.db"
DB_KEEP = 30         # 每個 (幣種,週期) 在 DB 保留幾根（連續均K 很少超過30）
DB_WARM = HA_HIST    # 開機補歷史抓幾根（僅供算 ATR14 暖身與連續段，仍只存 DB_KEEP 根）
DB_TICK = 15         # 收集器每幾秒巡一次
DB_GAP  = 0.15       # 每次 API 之間間隔秒數（避免打到 OKX 限流）
HA_MAX  = 2000       # /ha 單次最多抓幾根
DB_FILE = "/srv/1111bot/data/ha_market.db"
DB_KEEP = 30         # 每個 (幣種,週期) 在 DB 保留幾根（連續均K 很少超過30）
DB_WARM = HA_HIST    # 開機補歷史抓幾根（僅供算 ATR14 暖身與連續段，仍只存 DB_KEEP 根）
DB_TICK = 15         # 收集器每幾秒巡一次
DB_GAP  = 0.15       # 每次 API 之間間隔秒數（避免打到 OKX 限流）
HA_INLINE = 30       # /ha 根數 <= 此值直接顯示在 TG，超過則產 Excel 寄信

# 均K 專屬週期表（不共用原K 的 normal.TF_SEC，兩邊互不影響）
# tf -> (秒數, OKX bar, 合成倍數)  合成倍數 >1 表示該週期非交易所原生，需自行合成
HA_TF = {
    "5m":    (300,    "5m", 1),
    "10m":   (600,    "5m", 2),
    "15m":   (900,   "15m", 1),
    "30m":   (1800,  "30m", 1),
    "60m":   (3600,   "1H", 1),
    "120m":  (7200,   "2H", 1),
    "240m":  (14400,  "4H", 1),
    "480m":  (28800,  "4H", 2),
    "720m":  (43200, "12H", 1),
    "1440m": (86400,  "1D", 1),
}
TF_LIST = ["5m", "10m", "15m", "30m", "60m", "120m", "240m", "480m", "720m", "1440m"]
# OKX 的 12H/1D K 線以 UTC+8 為日界；短週期能整除 8 小時，套用位移不影響結果
ALIGN_OFF = 8 * 3600

def tf_sec(tf):
    return HA_TF[tf][0]

def next_open_epoch(now_epoch, tf):
    """下一根 K 線的開盤 epoch（秒），以 UTC+8 對齊。"""
    sec = tf_sec(tf)
    n = int(now_epoch) + ALIGN_OFF
    return ((n // sec) + 1) * sec - ALIGN_OFF

def load_env(p):
    d = {}
    for line in open(p):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); d[k] = v
    return d

ACC = load_env("/srv/1111bot/config/accounts.env")
BOTS = load_env("/srv/1111bot/config/bots.env")
TOKEN = BOTS["BOT_o3333o_HA"]
SYMS = json.load(open("/srv/1111bot/config/symbols.json"))["symbols"]

PENDING = {}; STRATS = {}; TASKS = {}; STATS = {}
CHAT_ID = None
HTTP = None
SPEC_CACHE = {}

def skey(s, d): return f"{s}_{d}"
def inst_id(s): return s.replace("USDT", "") + "-USDT-SWAP"
def now8(): return datetime.now(TZ8)
def hhmmss(): return now8().strftime("%H:%M:%S")
def today8(): return now8().strftime("%Y-%m-%d")
def pct(v): return str(Decimal(str(v)).normalize())

# ---------- 狀態持久化（原子寫入） ----------
SAVE_FIELDS = ("sym","dir","tf","lev","margin","pre","post",
               "pre_max","entry_min","exit_dd","chat",
               "pos_open","pos_px","pos_ee","pos_sz","pos_peak","last_bar")

SHUTTING_DOWN = False

def save_state(force=False):
    """force=True：寫入當下真實狀態（使用者主動停止時用，避免幽靈策略殘留存檔）。
    force=False：保留原保護，避免競態把仍在跑的策略誤清空。"""
    try:
        data = {"chat": CHAT_ID, "tf": ACCOUNT_TF, "stats": STATS, "strats": []}
        for k, S in STRATS.items():
            if S.get("alive"):
                data["strats"].append({a: S[a] for a in SAVE_FIELDS if a in S})
        if not force and STRATS and not data["strats"]:
            return
        def enc(o): return str(o) if isinstance(o, Decimal) else o
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, default=enc); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("save_state fail", e)

def bump(k, field):
    t = today8()
    if k not in STATS or STATS[k]["date"] != t:
        STATS[k] = {"date": t, "placed": 0, "entered": 0}
    STATS[k][field] += 1
    save_state()

def get_stat(k):
    t = today8()
    if k not in STATS or STATS[k]["date"] != t: return (0, 0)
    return (STATS[k]["placed"], STATS[k]["entered"])

# ---------- 交易紀錄 ----------
def trade_file(t):
    return f"/srv/1111bot/data/trades_h_{ACCT}_" + str(t).replace("-", "") + ".json"

def load_trades(t):
    try: return json.load(open(trade_file(t)))
    except Exception: return []

def log_trade(rec):
    try:
        fp = trade_file(rec.get("date"))
        try: arr = json.load(open(fp))
        except Exception: arr = []
        arr.append(rec)
        tmp = fp + ".tmp"
        with open(tmp, "w") as f:
            json.dump(arr, f, default=str); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, fp)
    except Exception as e:
        print("log_trade fail", e)

# ---------- OKX API ----------
def ts_now():
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond//1000:03d}Z"

def sign(sec, ts, m, p, b=""):
    return base64.b64encode(hmac.new(sec.encode(), f"{ts}{m}{p}{b}".encode(), hashlib.sha256).digest()).decode()

async def api(method, path, body=None):
    b = json.dumps(body) if body else ""
    ts = ts_now()
    h = {"OK-ACCESS-KEY": ACC[f"OKX_{ACCT}_API_KEY"],
         "OK-ACCESS-SIGN": sign(ACC[f"OKX_{ACCT}_SECRET"], ts, method, path, b),
         "OK-ACCESS-TIMESTAMP": ts,
         "OK-ACCESS-PASSPHRASE": ACC[f"OKX_{ACCT}_PASSPHRASE"],
         "Content-Type": "application/json"}
    try:
        r = await HTTP.request(method, BASE + path, headers=h, content=b)
        return r.json()
    except Exception as e:
        print("api fail", path, type(e).__name__, e)
        return {"code": "-1", "msg": str(e), "data": []}

async def pub(path):
    try:
        r = await HTTP.get(BASE + path)
        return r.json()
    except Exception as e:
        print("pub fail", path, type(e).__name__, e)
        return {"code": "-1", "msg": str(e), "data": []}

async def get_spec(s):
    if s in SPEC_CACHE: return SPEC_CACHE[s]
    iid = inst_id(s)
    r = await pub(f"/api/v5/public/instruments?instType=SWAP&instId={iid}")
    d = r["data"][0]
    spec = {"iid": iid, "tick": Decimal(d["tickSz"]), "lot": Decimal(d["lotSz"]),
            "minsz": Decimal(d["minSz"]), "ctval": Decimal(d["ctVal"]),
            "maxlev": Decimal(d["lever"]), "ctvalccy": d["ctValCcy"]}
    SPEC_CACHE[s] = spec
    return spec

async def get_last(iid):
    r = await pub(f"/api/v5/market/ticker?instId={iid}")
    return Decimal(r["data"][0]["last"])

def csize(m, lev, px, cv, lot):
    return ((m * lev / px) / cv / lot).to_integral_value(rounding=ROUND_DOWN) * lot

# ---------- K 線 / HA ----------
async def get_klines(iid, bar, limit=HA_HIST):
    """只取已收線（confirm=1）的 K 線，回傳舊->新。"""
    r = await pub(f"/api/v5/market/candles?instId={iid}&bar={bar}&limit={min(300, limit)}")
    if r.get("code") != "0": return []
    out = []
    for c in (r.get("data") or []):
        try:
            if len(c) >= 9 and str(c[8]) != "1": continue
            out.append({"ts": int(c[0]), "o": Decimal(c[1]), "h": Decimal(c[2]),
                        "l": Decimal(c[3]), "c": Decimal(c[4])})
        except Exception:
            continue
    out.reverse()
    return out

async def get_klines_paged(iid, bar, want):
    """翻頁抓已收線 K 線（舊->新）。
    /market/candles 只保留最近約 1440 根，翻不動時自動改用 /market/history-candles。"""
    out = {}
    after = None
    ep = "candles"
    for _ in range(40):                      # 上限保護，避免無限翻頁
        q = f"/api/v5/market/{ep}?instId={iid}&bar={bar}&limit=300"
        if after is not None:
            q += f"&after={after}"
        r = await pub(q)
        data = (r.get("data") or []) if r.get("code") == "0" else []
        if not data:
            if ep == "candles":
                ep = "history-candles"; continue
            break
        oldest = None
        for cd in data:
            try:
                ts = int(cd[0])
                oldest = ts if oldest is None else min(oldest, ts)
                if len(cd) >= 9 and str(cd[8]) != "1":
                    continue
                if ts in out:
                    continue
                out[ts] = {"ts": ts, "o": Decimal(cd[1]), "h": Decimal(cd[2]),
                           "l": Decimal(cd[3]), "c": Decimal(cd[4])}
            except Exception:
                continue
        if len(out) >= want:
            break
        if oldest is None or (after is not None and oldest >= after):
            if ep == "candles":
                ep = "history-candles"; after = oldest or after; continue
            break
        after = oldest
        await asyncio.sleep(0.15)
    kl = [out[t] for t in sorted(out)]
    return kl[-want:] if len(kl) > want else kl

async def klines_paged_for_tf(iid, tf, want):
    """依週期抓 want 根（非原生週期先抓底層 bar 再合成）。"""
    sec, bar, mul = HA_TF[tf]
    if mul == 1:
        return await get_klines_paged(iid, bar, want)
    kl = await get_klines_paged(iid, bar, want * mul + mul * 2)
    kl = align_head(kl, sec * 1000, mul)
    kl = merge_n(kl, mul)
    return kl[-want:] if len(kl) > want else kl

def align_head(kl, period_ms, n):
    """丟掉開頭沒對齊合成邊界的零星根。最多丟 n-1 根；丟完仍對不上就是資料異常，
    直接回空並記錄，避免無聲把整串資料清光。"""
    dropped = 0
    while kl and int(kl[0]["ts"]) % period_ms != 0 and dropped < n:
        kl.pop(0); dropped += 1
    if kl and int(kl[0]["ts"]) % period_ms != 0:
        print("align_head: 時間戳無法對齊合成邊界", kl[0]["ts"], period_ms)
        return []
    return kl

def merge_n(kl, n):
    """把 n 根合成 1 根（舊->新）。用於非原生週期：10m=2x5m、480m=2x4H。"""
    out = []
    for i in range(0, len(kl) - n + 1, n):
        g = kl[i:i+n]
        out.append({"ts": g[0]["ts"], "o": g[0]["o"],
                    "h": max(x["h"] for x in g), "l": min(x["l"] for x in g),
                    "c": g[-1]["c"]})
    return out

async def get_klines_paged(iid, bar, want):
    """往回翻頁抓已收線 K 線，回傳舊->新。
    candles 只保留最近約 1440 根，翻不動時自動改用 history-candles 續抓。"""
    got = {}
    after = None
    ep = "candles"
    for _ in range(40):
        if len(got) >= want: break
        q = f"/api/v5/market/{ep}?instId={iid}&bar={bar}&limit=300"
        if after: q += f"&after={after}"
        r = await pub(q)
        if r.get("code") != "0":
            if ep == "candles":
                ep = "history-candles"; continue
            break
        data = r.get("data") or []
        if not data:
            if ep == "candles":
                ep = "history-candles"; continue
            break
        new = 0
        for c in data:
            try:
                if len(c) >= 9 and str(c[8]) != "1": continue
                t = int(c[0])
                if t in got: continue
                got[t] = {"ts": t, "o": Decimal(c[1]), "h": Decimal(c[2]),
                          "l": Decimal(c[3]), "c": Decimal(c[4])}
                new += 1
            except Exception:
                continue
        after = data[-1][0]          # 該頁最舊的 ts，用來繼續往回翻
        if new == 0:
            if ep == "candles":
                ep = "history-candles"; continue
            break
        await asyncio.sleep(0.12)
    out = [got[t] for t in sorted(got)]
    return out[-want:] if len(out) > want else out

async def klines_paged_for_tf(iid, tf, want):
    """依週期翻頁抓 K 線；非原生週期先抓底層 bar 再對齊合成。"""
    sec, bar, mul = HA_TF[tf]
    if mul == 1:
        return await get_klines_paged(iid, bar, want)
    kl = await get_klines_paged(iid, bar, want * mul + mul * 4)
    pms = sec * 1000
    while kl and int(kl[0]["ts"]) % pms != 0:
        kl.pop(0)
    kl = merge_n(kl, mul)
    return kl[-want:] if len(kl) > want else kl

async def klines_and_ha(iid, tf, limit=HA_HIST):
    """回傳 (原始K線, HA序列)，兩者索引一一對應，皆為舊->新。
    非原生週期先抓底層 bar 再對齊邊界合成。"""
    sec, bar, mul = HA_TF[tf]
    if mul == 1:
        kl = await get_klines(iid, bar, limit)
    else:
        kl = await get_klines(iid, bar, min(300, limit * mul + mul * 2))
        kl = align_head(kl, sec * 1000, mul)
        kl = merge_n(kl, mul)
    if not kl: return [], []
    return kl, calc_ha(kl)

async def ha_series(iid, tf, limit=HA_HIST):
    """只要 HA 序列時用這個。"""
    kl, ha = await klines_and_ha(iid, tf, limit)
    return ha

def calc_atr(kl, period=14):
    """在【原始 K 線】上算 ATR14（Wilder 平滑）與 ATR14 ratio(%)。
    技術指標一律用正統 K 線，不使用 HA 平滑值。
    TR = max(h-l, |h-prev_c|, |l-prev_c|)
    前 period 根取 TR 簡單平均為種子，之後 ATR = (前ATR*(n-1) + TR)/n
    回傳與 kl 等長的 [(atr14, atr14_ratio), ...]，資料不足處為 (None, None)。"""
    n = len(kl)
    out = [(None, None)] * n
    if n == 0: return out
    trs = []
    for i, k in enumerate(kl):
        if i == 0:
            tr = k["h"] - k["l"]
        else:
            pc = kl[i-1]["c"]
            tr = max(k["h"] - k["l"], abs(k["h"] - pc), abs(k["l"] - pc))
        trs.append(tr)
    if n < period: return out
    prev = sum(trs[:period], Decimal(0)) / Decimal(period)
    c0 = kl[period-1]["c"]
    out[period-1] = (prev, (prev / c0 * 100) if c0 else None)
    for i in range(period, n):
        prev = (prev * Decimal(period - 1) + trs[i]) / Decimal(period)
        ci = kl[i]["c"]
        out[i] = (prev, (prev / ci * 100) if ci else None)
    return out

# ==================== 行情資料庫（決策時只讀 DB，不打 API）====================
# 設計目的：均K 是趨勢型策略，需要多根歷史才能判斷。若等到訊號當下才抓 K 線，
# 會多花數百毫秒甚至更久。收集器在每根 K 線收線後就把結果算好寫入 SQLite，
# 下單時只做一次本機讀取。
DB = None
COLLECT_LAST = {}    # (sym, tf) -> 最近已寫入的 bar ts
COLLECT_ERR = {}     # (sym, tf) -> 最近一次錯誤訊息

DDL = """
CREATE TABLE IF NOT EXISTS ha_bars (
  sym TEXT NOT NULL,
  tf  TEXT NOT NULL,
  ts  INTEGER NOT NULL,
  dt  TEXT,
  color TEXT,
  dir INTEGER,
  ha_o REAL, ha_h REAL, ha_l REAL, ha_c REAL,
  body_pct REAL,
  range_pct REAL,
  chg_pct REAL,
  o REAL, h REAL, l REAL, c REAL,
  atr14 REAL,
  atr14_ratio REAL,
  streak INTEGER,
  streak_body REAL,
  streak_range REAL,
  updated INTEGER,
  PRIMARY KEY (sym, tf, ts)
);
CREATE INDEX IF NOT EXISTS idx_ha_bars_lookup ON ha_bars (sym, tf, ts DESC);
CREATE TABLE IF NOT EXISTS ha_meta (
  sym TEXT NOT NULL,
  tf  TEXT NOT NULL,
  last_ts INTEGER,
  last_run INTEGER,
  bars INTEGER,
  err TEXT,
  PRIMARY KEY (sym, tf)
);
"""

def db_open():
    global DB
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    DB = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    DB.row_factory = sqlite3.Row
    DB.execute("PRAGMA journal_mode=WAL")      # 讀寫不互鎖
    DB.execute("PRAGMA synchronous=NORMAL")
    DB.executescript(DDL)
    DB.commit()
    n = DB.execute("SELECT COUNT(*) FROM ha_bars").fetchone()[0]
    print(f"行情DB 就緒：{DB_FILE}（現有 {n} 筆）")

def _f(v):
    try: return float(v)
    except Exception: return None

def build_rows(sym, tf, kl, ha, atrs):
    """把 K 線 / HA / ATR 併成一列列可寫入 DB 的紀錄，並算好連續段累計。"""
    rows = []
    streak = 0; sbody = 0.0; srange = 0.0; prev_color = None
    now = int(time.time())
    for i, x in enumerate(ha):
        k = kl[i]
        ho, hh, hl, hc = x["ho"], x["hh"], x["hl"], x["hc"]
        body = float((hc - ho) / ho * 100) if ho else 0.0
        rng = float((hh - hl) / k["c"] * 100) if k["c"] else 0.0
        if i == 0:
            chg = 0.0
        else:
            pc = ha[i-1]["hc"]
            chg = float((hc - pc) / pc * 100) if pc else 0.0
        if x["color"] == prev_color:
            streak += 1; sbody += body; srange += rng
        else:
            streak = 1; sbody = body; srange = rng
        prev_color = x["color"]
        a, r = atrs[i]
        dt = datetime.fromtimestamp(int(x["ts"]) / 1000, TZ8).strftime("%Y-%m-%d %H:%M")
        rows.append((sym, tf, int(x["ts"]), dt, x["color"],
                     1 if x["color"] == "G" else -1,
                     _f(ho), _f(hh), _f(hl), _f(hc),
                     body, rng, chg,
                     _f(k["o"]), _f(k["h"]), _f(k["l"]), _f(k["c"]),
                     _f(a) if a is not None else None,
                     _f(r) if r is not None else None,
                     streak, sbody, srange, now))
    return rows

def db_write(sym, tf, rows, err=None):
    if DB is None or not rows: return
    rows = rows[-DB_KEEP:]          # 前面幾根只是 ATR14/連續段的暖身，不必落地
    DB.executemany(
        "INSERT OR REPLACE INTO ha_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    DB.execute("DELETE FROM ha_bars WHERE sym=? AND tf=? AND ts NOT IN "
               "(SELECT ts FROM ha_bars WHERE sym=? AND tf=? ORDER BY ts DESC LIMIT ?)",
               (sym, tf, sym, tf, DB_KEEP))
    n = DB.execute("SELECT COUNT(*) FROM ha_bars WHERE sym=? AND tf=?", (sym, tf)).fetchone()[0]
    DB.execute("INSERT OR REPLACE INTO ha_meta VALUES (?,?,?,?,?,?)",
               (sym, tf, rows[-1][2], int(time.time()), n, err))
    DB.commit()

def db_latest(sym, tf, n=1):
    """取最近 n 根（回傳舊->新的 dict 清單）。決策時就是呼叫這個，不打 API。"""
    if DB is None: return []
    cur = DB.execute("SELECT * FROM ha_bars WHERE sym=? AND tf=? ORDER BY ts DESC LIMIT ?",
                     (sym, tf, n))
    return [dict(r) for r in cur.fetchall()][::-1]

def db_meta(sym=None, tf=None):
    if DB is None: return []
    q = "SELECT * FROM ha_meta"; a = []
    if sym: q += " WHERE sym=?"; a.append(sym)
    if sym and tf: q += " AND tf=?"; a.append(tf)
    cur = DB.execute(q + " ORDER BY sym, tf", a)
    return [dict(r) for r in cur.fetchall()]

def db_fresh(sym, tf, tol=2):
    """DB 是否新鮮：最近一根是否就是剛收線那根（容許落後 tol 根）。"""
    m = db_meta(sym, tf)
    if not m or not m[0].get("last_ts"): return False
    sec = tf_sec(tf)
    want = next_open_epoch(int(time.time()), tf) - 2 * sec
    return int(m[0]["last_ts"]) >= (want - tol * sec) * 1000

async def collect_one(sym, tf, warm=False):
    """抓一個 (幣種,週期) 並寫入 DB。回傳寫入根數，失敗回 0。"""
    try:
        spec = await get_spec(sym)
    except Exception as e:
        COLLECT_ERR[(sym, tf)] = f"spec:{type(e).__name__}"; return 0
    try:
        kl, ha = await klines_and_ha(spec["iid"], tf, DB_WARM if warm else HA_HIST)
        if not ha:
            COLLECT_ERR[(sym, tf)] = "無K線"; return 0
        atrs = calc_atr(kl, 14)
        rows = build_rows(sym, tf, kl, ha, atrs)
        db_write(sym, tf, rows)
        COLLECT_LAST[(sym, tf)] = rows[-1][2]
        COLLECT_ERR.pop((sym, tf), None)
        return len(rows)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        COLLECT_ERR[(sym, tf)] = msg
        try: db_write(sym, tf, [], err=msg)
        except Exception: pass
        print("collect fail", sym, tf, msg)
        return 0

def db_symbols():
    return [s["symbol"] for s in SYMS if s.get("enabled")]

async def collector(app):
    """背景收集器：每根 K 線收線後把該週期所有幣種算好寫入 DB。"""
    await asyncio.sleep(3)
    syms = db_symbols()
    print(f"行情收集器啟動：{len(syms)} 幣種 x {len(TF_LIST)} 週期")
    # 開機先補歷史
    ok = 0
    for tf in TF_LIST:
        for sym in syms:
            if await collect_one(sym, tf, warm=True): ok += 1
            await asyncio.sleep(DB_GAP)
    print(f"行情DB 初始化完成：{ok}/{len(syms)*len(TF_LIST)} 組")
    if CHAT_ID:
        await notify(app, CHAT_ID,
            f"{E.BOT} OKX均K｜{ACCT}\n事件：📚 行情DB 初始化完成\n"
            f"{len(syms)} 幣種 x {len(TF_LIST)} 週期 = {ok} 組就緒\n"
            f"每根收線後自動更新，下單時只讀本機不打 API\n時間：{hhmmss()}")
    while True:
        try:
            await asyncio.sleep(DB_TICK)
            now = time.time()
            for tf in TF_LIST:
                sec = tf_sec(tf)
                closed = next_open_epoch(int(now), tf) - 2 * sec   # 最後一根已收線的開盤 epoch
                if now < closed + sec + HA_LAG:                    # 還沒到可抓的時間
                    continue
                want = closed * 1000
                todo = [s for s in db_symbols() if COLLECT_LAST.get((s, tf), 0) < want]
                if not todo: continue
                for sym in todo:
                    await collect_one(sym, tf)
                    await asyncio.sleep(DB_GAP)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("collector loop error", type(e).__name__, e)
            await asyncio.sleep(5)

# ---------- Telegram（旁路） ----------
_BG = set()

async def _send_bg(app, chat, t):
    try:
        await app.bot.send_message(chat, t)
    except Exception as e:
        print("notify fail", type(e).__name__, e)

async def notify(app, chat, t):
    try:
        tk = asyncio.create_task(_send_bg(app, chat, t))
        _BG.add(tk); tk.add_done_callback(_BG.discard)
    except Exception as e:
        print("notify schedule fail", e)

async def reply(u, t):
    for i in range(2):
        try:
            await u.message.reply_text(t); return True
        except Exception as e:
            print("reply fail", i, type(e).__name__, e)
            await asyncio.sleep(2)
    return False

# ---------- OKX 事實查詢 ----------
async def okx_pos(iid, ps):
    r = await api("GET", "/api/v5/account/positions")
    if r.get("code") != "0": return None
    for p in (r.get("data") or []):
        if p.get("instId") == iid and p.get("posSide") == ps:
            try:
                if float(p.get("pos") or 0) != 0: return p
            except Exception: pass
    return None

async def okx_orders(iid=None, ps=None, prefix="h"):
    """只認均K 自己的前綴，絕不碰普K 的 n / x 單。"""
    r = await api("GET", "/api/v5/trade/orders-pending")
    if r.get("code") != "0": return []
    out = []
    for o in (r.get("data") or []):
        if iid and o.get("instId") != iid: continue
        if ps and o.get("posSide") != ps: continue
        cid = str(o.get("clOrdId") or "")
        if prefix and not cid.startswith(prefix): continue
        out.append(o)
    return out

async def sweep_h(iid=None, ps=None):
    """清掉均K 自己殘留的掛單（h/y 前綴）。市價單正常不會留單，此為保險。"""
    n = 0
    for pre in ("h", "y"):
        for o in await okx_orders(iid, ps, prefix=pre):
            cr = await api("POST", "/api/v5/trade/cancel-order",
                           {"instId": o["instId"], "ordId": o["ordId"]})
            if cr.get("code") == "0": n += 1
    return n

async def close_record(iid, ps, after_ms, tries=10):
    for i in range(tries):
        r = await api("GET", f"/api/v5/account/positions-history?instType=SWAP&instId={iid}&limit=10")
        if r.get("code") == "0":
            for p in (r.get("data") or []):
                if p.get("posSide") == ps and int(p.get("uTime") or 0) >= after_ms:
                    return p
        await asyncio.sleep(1)
    return None

# ---------- 訊號判定（資料一律來自本機 DB）----------
def disp(rows):
    """ATR14 ratio 標準化位移 = |Σ 均K實體漲跌幅| / Σ ATR14 ratio
    分子取絕對值，代表這段走了多遠；分母是同段的波動總量。
    比值大 = 單向推進；比值小 = 來回盤整。"""
    num = abs(sum(float(r.get("body_pct") or 0) for r in rows))
    den = sum(float(r.get("atr14_ratio") or 0) for r in rows)
    if den <= 0: return None
    return num / den

def judge_entry_db(rows, d, pre, post, pre_max, entry_min):
    """rows 為舊->新，至少 pre+post 根。
    進場：後 post 根同為順勢色，且
          前 pre 根位移 <= pre_max（盤整），後 post 根位移 >= entry_min（突破）。"""
    need = pre + post
    if len(rows) < need: return None
    seg = rows[-need:]
    pre_seg, post_seg = seg[:pre], seg[pre:]
    want = "G" if d == "L" else "R"
    color_ok = all(r.get("color") == want for r in post_seg)
    pd_ = disp(pre_seg); ed = disp(post_seg)
    if pd_ is None or ed is None: return None
    return {"pre_seg": pre_seg, "post_seg": post_seg, "color_ok": color_ok,
            "pre_disp": pd_, "entry_disp": ed,
            "pre_ok": pd_ <= pre_max, "entry_ok": ed >= entry_min,
            "hit": color_ok and pd_ <= pre_max and ed >= entry_min}

def judge_exit_db(close_px, entry_px, d, peak, exit_dd):
    """回吐出場：進場時利潤歸零，逐根更新最高利潤，
    當「目前利潤 − 最高利潤」跌破 exit_dd（負值）即出場。
    利潤為純價格變動%，不含槓桿。"""
    ep = float(entry_px); cp = float(close_px)
    if ep == 0: return None
    pnl = (cp - ep) / ep * 100 if d == "L" else (ep - cp) / ep * 100
    npk = max(float(peak), pnl)
    dd = pnl - npk
    return {"pnl_pct": pnl, "peak": npk, "dd": dd, "hit": dd <= float(exit_dd)}

# ---------- 進場 ----------
async def h_open(app, S, spec, iid, d, pos, info, k):
    last = await get_last(iid)
    size = csize(S["margin"], Decimal(S["lev"]), last, spec["ctval"], spec["lot"])
    if size < spec["minsz"]:
        await notify(app, S["chat"], f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 保證金不足，循環停止")
        S["alive"] = False
        return False
    r = await api("POST", "/api/v5/trade/order",
                  {"instId": iid, "tdMode": "isolated", "side": "buy" if d == "L" else "sell",
                   "posSide": pos, "ordType": "market", "sz": str(size),
                   "clOrdId": "h" + uuid.uuid4().hex[:14]})
    bump(k, "placed")
    if r.get("code") != "0":
        em = (r.get("data") or [{}])[0].get("sMsg") or r.get("msg")
        await notify(app, S["chat"], f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 進場失敗：{em}")
        return False
    oid = r["data"][0]["ordId"]
    fpx = None
    for _ in range(8):
        st = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={oid}")
        if st.get("code") == "0" and st.get("data"):
            dd = st["data"][0]
            if dd.get("state") == "filled" and dd.get("avgPx"):
                fpx = Decimal(dd["avgPx"]); break
            if dd.get("state") == "canceled":
                await notify(app, S["chat"], f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 市價單被取消")
                return False
        await asyncio.sleep(1)
    if fpx is None:
        # 查單沒回 filled 不代表沒成交；多查幾次持倉再判定，
        # 否則會出現「OKX 已有倉、程式卻以為空手」而下一根重複進場。
        p = None
        for _ in range(3):
            p = await okx_pos(iid, pos)
            if p: break
            await asyncio.sleep(2)
        if not p:
            await notify(app, S["chat"], f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 進場未確認成交，跳過本輪\n⚠ 若 OKX 實際有倉請手動處理")
            return False
        fpx = Decimal(p.get("avgPx") or await get_last(iid))
        try:
            rsz = abs(Decimal(str(p.get("pos") or "0")))
            if rsz > 0: size = rsz
        except Exception:
            pass
    bump(k, "entered")
    ee = time.time()
    S["state"] = "持倉中"; S["pos_open"] = True
    S["pos_px"] = str(fpx); S["pos_ee"] = ee; S["pos_sz"] = str(size)
    S["pos_peak"] = 0.0                      # 進場即把最高利潤歸零
    save_state()
    seq = "".join(x["color"] for x in (info.get("pre_seg", []) + info.get("post_seg", []))) if info else "-"
    await notify(app, S["chat"],
        f"{E.BOT} OKX均K｜{ACCT}\n事件：🔔 訊號進場（taker）\n"
        f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)} {S['lev']}x\n"
        f"週　　期：{S['tf']}\n進場價格：{fpx}\n下單張數：{size}\n"
        f"燈號序列：{seq}（前{S['pre']}+後{S['post']}）\n"
        f"前段位移：{info['pre_disp']:.4f} ≤ {pct(S['pre_max'])}\n"
        f"後段位移：{info['entry_disp']:.4f} ≥ {pct(S['entry_min'])}\n"
        f"出場條件：從最高利潤回吐 {pct(S['exit_dd'])}%\n"
        f"⚠ 無 TP/SL/TE，僅靠回吐出場\n"
        f"狀　　態：📌 持倉中\n時間：{hhmmss()}")
    return True

# ---------- 出場 ----------
async def h_exit(app, S, spec, iid, d, pos, size, fpx, ee, reason, k):
    cs = "sell" if d == "L" else "buy"
    p_now = await okx_pos(iid, pos)
    if not p_now:
        await notify(app, S["chat"], f"{E.BOT} {S['sym']} {E.dir_word(d)} 平倉時 OKX 已無持倉，略過")
        for a in ("pos_open", "pos_px", "pos_ee", "pos_sz", "pos_peak"):
            S.pop(a, None)
        save_state()
        return True
    try:
        real = abs(Decimal(str(p_now.get("availPos") or p_now.get("pos") or "0")))
        if real > 0:
            if real != size:
                print("size mismatch", S.get("sym"), d, "local", size, "okx", real)
            size = real
    except Exception:
        pass
    t0 = int(time.time() * 1000)
    xr = await api("POST", "/api/v5/trade/order",
                   {"instId": iid, "tdMode": "isolated", "side": cs, "posSide": pos,
                    "ordType": "market", "sz": str(size),
                    "clOrdId": "y" + uuid.uuid4().hex[:14]})
    if xr.get("code") != "0":
        em = (xr.get("data") or [{}])[0].get("sMsg") or xr.get("msg")
        await notify(app, S["chat"], f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 平倉失敗：{em}\n⚠ 倉位可能仍在，請至 OKX 確認")
        return False
    ph = await close_record(iid, pos, t0)
    src = "OKX"
    if ph:
        g = Decimal(ph.get("pnl") or "0")
        fee = Decimal(ph.get("fee") or "0") + Decimal(ph.get("fundingFee") or "0")
        net = Decimal(ph.get("realizedPnl") or "0")
        xpx = Decimal(ph.get("closeAvgPx") or "0") or await get_last(iid)
        nv = Decimal(ph.get("openAvgPx") or fpx) * Decimal(ph.get("closeTotalPos") or size) * spec["ctval"]
    else:
        src = "估算"
        xpx = await get_last(iid)
        g = (xpx - fpx) * size * spec["ctval"] if d == "L" else (fpx - xpx) * size * spec["ctval"]
        fee = Decimal(0); net = g
        nv = fpx * size * spec["ctval"]
    gp = (g / nv * 100) if nv else Decimal(0)
    fp = (fee / nv * 100) if nv else Decimal(0)
    npv = (net / nv * 100) if nv else Decimal(0)
    hs = int(time.time() - ee)
    tfs = tf_sec(S["tf"])
    log_trade({"date": today8(), "sym": S["sym"], "dir": d, "reason": reason,
               "hold_s": hs, "bars": round(hs / tfs, 1),
               "gross": str(g), "fee": str(fee), "net": str(net), "nv": str(nv),
               "src": src, "ts": hhmmss(),
               "in_ts": datetime.fromtimestamp(ee, TZ8).strftime("%H:%M:%S"),
               "tf": S["tf"], "pre": str(S["pre"]), "post": str(S["post"]),
               "pre_max": str(S["pre_max"]), "entry_min": str(S["entry_min"]),
               "exit_dd": str(S["exit_dd"]),
               "peak_pct": round(float(S.get("pos_peak") or 0), 4),
               "lev": S["lev"], "margin": str(S["margin"]),
               "in_px": str(fpx), "out_px": str(xpx)})
    await notify(app, S["chat"],
        f"{E.BOT} OKX均K｜{ACCT}\n事件：{'🟢' if net >= 0 else '🔴'} 已出場\n"
        f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n出場原因：{reason}\n"
        f"進場價：{fpx}\n出場價：{xpx}\n"
        f"持倉秒數：{hs}s（約 {hs / tfs:.1f} 根）\n"
        f"毛損益：{g:+.6f} ({gp:+.3f}%)\n手續費：{fee:+.6f} ({fp:+.3f}%)\n"
        f"淨損益：{net:+.6f} ({npv:+.3f}%) {E.pnl_emoji(net)}\n時間：{hhmmss()}")
    for a in ("pos_open", "pos_px", "pos_ee", "pos_sz", "pos_peak"):
        S.pop(a, None)
    save_state()
    return True

# ---------- 重啟接管 ----------
async def h_takeover(app, S, spec, iid, d, pos):
    p = await okx_pos(iid, pos)
    if not p:
        for a in ("pos_open", "pos_px", "pos_ee", "pos_sz", "pos_peak"):
            S.pop(a, None)
        save_state()
        return None
    fpx = Decimal(S.get("pos_px") or p.get("avgPx") or "0")
    ee = float(S.get("pos_ee") or time.time())
    size = abs(Decimal(p.get("pos") or "0"))
    S["state"] = "持倉中"; save_state()
    await notify(app, S["chat"],
        f"{E.BOT} {E.dir_emoji(d)} {S['sym']} {E.dir_word(d)} 已接管既有持倉\n"
        f"進場價 {fpx}｜{size} 張\n繼續等待反向訊號出場")
    return (size, fpx, ee)

# ---------- 主迴圈 ----------
async def wait_db_bar(sym, tf, want_ts, limit=90):
    """等收集器把剛收線那根寫進 DB；逾時就自己補抓一次。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        r = db_latest(sym, tf, 1)
        if r and int(r[-1]["ts"]) >= want_ts:
            return True
        await asyncio.sleep(1)
    n = await collect_one(sym, tf)      # 後備：自己抓，確保不漏一根
    if n:
        r = db_latest(sym, tf, 1)
        return bool(r and int(r[-1]["ts"]) >= want_ts)
    return False

async def hloop(app, chat, S):
    """均K 主迴圈：每根 K 收線後讀 DB 判燈號，決策不打 API。"""
    spec = S["spec"]; iid = spec["iid"]; d = S["dir"]
    pos = "long" if d == "L" else "short"
    k = skey(S["sym"], d)
    need = int(S["pre"]) + int(S["post"])
    size = fpx = None; ee = None
    try:
        if S.get("pos_open"):
            tk = await h_takeover(app, S, spec, iid, d, pos)
            if tk: size, fpx, ee = tk

        while S["alive"]:
            tf_sec_v = tf_sec(S["tf"])
            oe = next_open_epoch(int(time.time()), S["tf"])
            S["state"] = "持倉中" if S.get("pos_open") else "等訊號"
            save_state()
            # 分段睡眠：每 5 秒檢查一次 alive，/stop 後最多 5 秒收工
            while S["alive"]:
                w = oe + HA_LAG - time.time()
                if w <= 0: break
                await asyncio.sleep(min(5.0, w))
            if not S["alive"]: break

            want = (oe - tf_sec_v) * 1000
            ok = await wait_db_bar(S["sym"], S["tf"], want)
            S["hb"] = time.time(); S["hb_warned"] = False
            if not ok:
                await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} "
                                        f"{S['tf']} 行情DB 未更新，本輪跳過（不下單）")
                continue
            rows = db_latest(S["sym"], S["tf"], max(need, 2))
            if not rows: continue
            last = rows[-1]
            bar_ts = int(last["ts"])
            if bar_ts == S.get("last_bar"):
                continue                      # 這根已判過，不重複
            S["last_bar"] = bar_ts; save_state()

            if S.get("pos_open"):
                # --- 持倉中：回吐判斷 ---
                p = await okx_pos(iid, pos)
                if not p:
                    await notify(app, chat, f"{E.BOT} {S['sym']} {E.dir_word(d)} OKX 已無持倉（可能手動平倉），重回等訊號")
                    for a2 in ("pos_open", "pos_px", "pos_ee", "pos_sz", "pos_peak"):
                        S.pop(a2, None)
                    size = fpx = ee = None
                    save_state(); continue
                if S.get("pos_sz"):
                    size = Decimal(str(S["pos_sz"]))
                else:
                    try: size = abs(Decimal(p.get("pos") or "0"))
                    except Exception: pass
                if fpx is None: fpx = Decimal(S.get("pos_px") or p.get("avgPx") or "0")
                if ee is None: ee = float(S.get("pos_ee") or time.time())
                r = judge_exit_db(last["c"], fpx, d, S.get("pos_peak", 0.0), S["exit_dd"])
                if r is None: continue
                S["pos_peak"] = r["peak"]; save_state()
                if r["hit"]:
                    await notify(app, chat,
                        f"{E.BOT} {S['sym']} {E.dir_word(d)} 觸發回吐出場\n"
                        f"最高利潤 {r['peak']:+.4f}%｜目前 {r['pnl_pct']:+.4f}%\n"
                        f"回吐 {r['dd']:+.4f}% ≤ {pct(S['exit_dd'])}%")
                    await h_exit(app, S, spec, iid, d, pos, size, fpx, ee, "Drawdown_Exit", k)
                    size = fpx = ee = None
            else:
                # --- 空手：進場判斷 ---
                if len(rows) < need: continue
                r = judge_entry_db(rows, d, int(S["pre"]), int(S["post"]),
                                   float(S["pre_max"]), float(S["entry_min"]))
                if r and r["hit"]:
                    ok2 = await h_open(app, S, spec, iid, d, pos, r, k)
                    if ok2:
                        fpx = Decimal(S["pos_px"]); ee = float(S["pos_ee"])
                        size = Decimal(str(S["pos_sz"]))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("hloop error", S.get("sym"), S.get("dir"), type(e).__name__, e)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 循環錯誤：{type(e).__name__}: {e}")
    finally:
        S["state"] = "已停止"; S["alive"] = False
        try:
            nrm = await sweep_h(iid, pos)
            if nrm:
                await notify(app, chat, f"{E.BOT} {S['sym']} {E.dir_word(d)} 結束前清除殘留掛單 {nrm} 筆")
        except Exception as e:
            print("finally sweep fail", e)
        if not SHUTTING_DOWN:
            if STRATS.get(k) is S:
                STRATS.pop(k, None)
            try:
                me = asyncio.current_task()
            except Exception:
                me = None
            if TASKS.get(k) is None or TASKS.get(k) is me:
                TASKS.pop(k, None)
            save_state(True)
        try:
            if await okx_pos(iid, pos):
                await notify(app, chat, f"{E.BOT} ⚠ {S['sym']} {E.dir_word(d)} 已停止但仍有持倉，請至 OKX 處理")
        except Exception:
            pass

# ---------- 心跳看門狗 ----------
async def hb_watch(app):
    while True:
        await asyncio.sleep(60)
        try:
            for k, S in list(STRATS.items()):
                if not S.get("alive"): continue
                hb = S.get("hb")
                if not hb: continue
                lim = tf_sec(S["tf"]) * HB_BARS
                gap = time.time() - hb
                if gap > lim and not S.get("hb_warned"):
                    S["hb_warned"] = True
                    await notify(app, S.get("chat") or CHAT_ID,
                        f"{E.BOT} {E.LOSS} 心跳異常\n"
                        f"{E.dir_emoji(S['dir'])} {S['sym']} {E.dir_word(S['dir'])}\n"
                        f"已 {int(gap)}s（>{HB_BARS} 根 {S['tf']}）未推進燈號判定。\n"
                        f"⚠ 均K 無停損，出場依賴程式運作，請確認服務狀態\n時間：{hhmmss()}")
                elif gap <= lim and S.get("hb_warned"):
                    S["hb_warned"] = False
                    await notify(app, S.get("chat") or CHAT_ID,
                        f"{E.BOT} ✅ 心跳恢復｜{S['sym']} {E.dir_word(S['dir'])}")
        except Exception as e:
            print("hb_watch fail", e)

# ---------- 啟動接管 ----------
async def rebuild_strat(d):
    spec = await get_spec(d["sym"])
    S = {"sym": d["sym"], "dir": d["dir"], "tf": d.get("tf", ACCOUNT_TF),
         "lev": int(d["lev"]), "margin": Decimal(str(d["margin"])),
         "pre": int(d["pre"]), "post": int(d["post"]),
         "pre_max": Decimal(str(d["pre_max"])),
         "entry_min": Decimal(str(d["entry_min"])),
         "exit_dd": Decimal(str(d["exit_dd"])), "spec": spec,
         "alive": True, "state": "等訊號", "chat": d.get("chat", CHAT_ID),
         "hb": time.time(), "hb_warned": False}
    for a in ("pos_open", "pos_px", "pos_ee", "pos_sz", "pos_peak", "last_bar"):
        if a in d: S[a] = d[a]
    return S

async def startup_recover(app):
    global CHAT_ID, ACCOUNT_TF, STATS
    if not os.path.exists(STATE_FILE):
        print("無存檔"); return
    try:
        data = json.load(open(STATE_FILE))
    except Exception as e:
        print("讀存檔失敗", e); return
    CHAT_ID = data.get("chat"); ACCOUNT_TF = data.get("tf", "5m"); STATS = data.get("stats", {})
    saved = data.get("strats", [])
    if not saved:
        print("存檔無策略"); return
    rec = []
    for d in saved:
        try:
            S = await rebuild_strat(d)
            k = skey(S["sym"], S["dir"])
            if k in STRATS:
                print("存檔有重複策略，略過", k); continue
            STRATS[k] = S
            TASKS[k] = asyncio.create_task(hloop(app, S["chat"], S))
            rec.append(f"{E.dir_emoji(S['dir'])} {S['sym']} {E.dir_word(S['dir'])}")
        except Exception as e:
            print("重建失敗", d, e)
    n_pos = 0
    posr = await api("GET", "/api/v5/account/positions")
    if posr.get("code") == "0":
        n_pos = len([p for p in posr.get("data", []) if float(p.get("pos", "0")) != 0])
    print(f"已接管均K 策略 {len(rec)}｜OKX 帳戶持倉{n_pos}（含普K）")
    if CHAT_ID and rec:
        await notify(app, CHAT_ID,
            f"{E.BOT} OKX均K｜{ACCT}\n事件：🔄 重啟認領完成\n━━━━━━━━━━\n"
            f"已接管策略（{len(rec)}）：\n" + "\n".join("・" + x for x in rec) +
            f"\n循環已接管，繼續運作\n時間：{hhmmss()}")

# ---------- TG 指令 ----------
def strat_params(sym, dr):
    S = STRATS.get(skey(sym, dr))
    if not S or not S.get("alive"):
        return f"{dr}（已停止）"
    return (f"{dr} {S['tf']} {S['lev']}x {pct(S['margin'])} "
            f"{S['pre']}/{S['post']} {pct(S['pre_max'])}/{pct(S['entry_min'])} "
            f"{pct(S['exit_dd'])}%")

async def cmd_run(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args
    fmt = (f"用法：/run 商品 方向 週期 槓桿 保證金 前根數 後根數 前段位移上限 後段位移下限 回吐%\n"
           f"例：/run BTCUSDT L 5m 1 100 5 3 0.5 1.5 -2%\n"
           f"━━━━━━━━━━\n"
           f"前根數：反轉前觀察根數\n"
           f"後根數：反轉後連續同色確認根數\n"
           f"前段位移上限：該段需為盤整（位移 ≤ 此值）\n"
           f"後段位移下限：該段需為突破（位移 ≥ 此值）\n"
           f"位移 = |Σ均K實體漲跌幅| / Σ ATR14 ratio\n"
           f"回吐%：從最高利潤跌多少即出場（負值）\n"
           f"週期：" + " / ".join(TF_LIST))
    if len(a) != 10:
        await reply(u, f"{E.BOT} 參數數量錯誤（需10個，收到{len(a)}個）\n{fmt}"); return
    try:
        sym = a[0].upper(); dr = a[1].upper(); tf = a[2]
        lev = int(a[3].replace("x", "")); margin = Decimal(a[4])
        pre = int(a[5]); post = int(a[6])
        pre_max = Decimal(a[7]); entry_min = Decimal(a[8])
        exit_dd = Decimal(a[9].replace("%", "").strip())
    except Exception:
        await reply(u, f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if dr not in ("L", "S"):
        await reply(u, f"{E.BOT} 方向須 L 或 S"); return
    if tf not in HA_TF:
        await reply(u, f"{E.BOT} 週期須為：" + " / ".join(TF_LIST)); return
    for nm, v in (("前根數", pre), ("後根數", post)):
        if not 1 <= v <= DB_KEEP:
            await reply(u, f"{E.BOT} {nm} 須 1~{DB_KEEP}"); return
    if pre + post > DB_KEEP:
        await reply(u, f"{E.BOT} 前根數+後根數 不可超過 {DB_KEEP}（行情DB 保留上限）"); return
    if pre_max <= 0 or entry_min <= 0:
        await reply(u, f"{E.BOT} 位移門檻須大於 0"); return
    if exit_dd >= 0:
        await reply(u, f"{E.BOT} 回吐% 須為負值，例如 -2%"); return
    k = skey(sym, dr)
    if k in STRATS and STRATS[k].get("alive"):
        await reply(u, f"{E.BOT} {sym} {E.dir_word(dr)} 已在運行"); return
    try: spec = await get_spec(sym)
    except Exception: await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return
    op = await get_last(spec["iid"])
    size = csize(margin, Decimal(lev), op, spec["ctval"], spec["lot"])
    if size < spec["minsz"]:
        nd = spec["minsz"] * spec["ctval"] * op / Decimal(lev)
        await reply(u, f"{E.BOT} {E.LOSS} 保證金不足：算出 {size} 張 < 最小 {spec['minsz']}\n此槓桿下至少需約 {nd:.4f} USDT"); return
    # 現況預覽（讀 DB，不打 API）
    rows = db_latest(sym, tf, pre + post)
    if len(rows) < pre + post:
        await reply(u, f"{E.BOT} {E.LOSS} 行情DB 資料不足（{sym} {tf} 目前 {len(rows)} 根，需 {pre+post} 根）\n"
                       f"請稍候收集器補齊，或用 /db {sym} {tf} 查看"); return
    fresh = db_fresh(sym, tf)
    r = judge_entry_db(rows, dr, pre, post, float(pre_max), float(entry_min))
    seq = "".join(x["color"] for x in rows)
    prev = (f"目前燈號：{seq}\n"
            f"前{pre}根位移：{r['pre_disp']:.4f} {'✅' if r['pre_ok'] else '❌'} ≤ {pre_max}\n"
            f"後{post}根位移：{r['entry_disp']:.4f} {'✅' if r['entry_ok'] else '❌'} ≥ {entry_min}\n"
            f"後{post}根同色：{'✅' if r['color_ok'] else '❌'}\n"
            f"此刻是否成立：{'✅ 會進場' if r['hit'] else '❌ 不進場'}")
    ps = "long" if dr == "L" else "short"
    exist = await okx_pos(spec["iid"], ps)
    warn = f"\n⚠ OKX 上 {sym} {E.dir_word(dr)} 已有 {exist['pos']} 張持倉（倉位會被合併）" if exist else ""
    if not fresh: warn += "\n⚠ 行情DB 落後，啟動後會等資料補齊才判斷"
    PENDING[u.effective_chat.id] = {"t": time.time(), "sym": sym, "dir": dr, "tf": tf,
        "lev": lev, "margin": margin, "pre": pre, "post": post,
        "pre_max": pre_max, "entry_min": entry_min, "exit_dd": exit_dd, "spec": spec}
    await reply(u, f"{E.BOT} OKX均K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
        f"商　　品：{E.dir_emoji(dr)} {sym} {E.dir_word(dr)} {lev}x\n週　　期：{tf}\n"
        f"目前價格：{op}\n保 證 金：{margin} USDT\n預估張數：{size}\n"
        f"━━━━━━━━━━\n"
        f"進場：前{pre}根位移 ≤ {pre_max}（盤整）\n"
        f"　　　後{post}根同色且位移 ≥ {entry_min}（突破）\n"
        f"出場：從最高利潤回吐 {exit_dd}%（純價格變動，不含槓桿）\n"
        f"進出場皆 taker 市價\n━━━━━━━━━━\n{prev}\n"
        f"━━━━━━━━━━\n⚠ 無 TP/SL/TE，僅靠回吐出場{warn}\n"
        f"下一步：60秒內 /confirm\n時間：{hhmmss()}")
    asyncio.create_task(_to(c.application, u.effective_chat.id, PENDING[u.effective_chat.id]["t"]))

async def _to(app, chat, stamp):
    await asyncio.sleep(61)
    p = PENDING.get(chat)
    if p and p["t"] == stamp:
        del PENDING[chat]
        await notify(app, chat, f"{E.BOT} 參數逾時已取消，請重新 /run")

async def cmd_confirm(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    p = PENDING.get(u.effective_chat.id)
    if not p: await reply(u, f"{E.BOT} 沒有待確認的 /run"); return
    if time.time() - p["t"] > 60:
        del PENDING[u.effective_chat.id]; await reply(u, f"{E.BOT} 確認逾時"); return
    del PENDING[u.effective_chat.id]
    k = skey(p["sym"], p["dir"])
    # 同 key 若還有舊策略/舊 task，先停掉並等它收工，避免兩個 task 同時對同一倉位下單
    old_S = STRATS.get(k)
    if old_S is not None:
        old_S["alive"] = False
    old_T = TASKS.get(k)
    if old_T is not None and not old_T.done():
        old_T.cancel()
        try:
            await asyncio.wait([old_T], timeout=8)
        except Exception as e:
            print("cancel old task fail", e)
        await reply(u, f"{E.BOT} 已先停止 {p['sym']} {E.dir_word(p['dir'])} 的舊策略")
    STRATS[k] = S = {**p, "alive": True, "state": "等訊號", "chat": u.effective_chat.id,
                     "hb": time.time(), "hb_warned": False}
    TASKS[k] = asyncio.create_task(hloop(c.application, u.effective_chat.id, S))
    save_state(True)
    cnt = sum(1 for s in STRATS.values() if s.get("alive"))
    await reply(u, f"{E.BOT} ✅ 已確認，{p['sym']} {E.dir_word(p['dir'])} 啟動\n運行中策略：{cnt} 個")

async def cmd_stop(u, c):
    a = c.args
    alive = [k for k, s in STRATS.items() if s.get("alive")]
    if not alive: await reply(u, f"{E.BOT} 目前無運行中策略"); return
    if not a:
        lst = "\n".join(f"・/stop {STRATS[k]['sym']} {STRATS[k]['dir']}" for k in alive)
        await reply(u, f"{E.BOT} 請指定：\n{lst}\n或 /stopall"); return
    sym = a[0].upper()
    tg = [skey(sym, a[1].upper())] if len(a) >= 2 and skey(sym, a[1].upper()) in alive \
         else [k for k in alive if STRATS[k]["sym"] == sym]
    if not tg: await reply(u, f"{E.BOT} 找不到運行中的 {sym}"); return
    if len(tg) > 1: await reply(u, f"{E.BOT} {sym} 有多方向，請指定 /stop {sym} L 或 S"); return
    S = STRATS[tg[0]]; d = S["dir"]; iid = S["spec"]["iid"]
    ps = "long" if d == "L" else "short"
    p = await okx_pos(iid, ps)
    S["alive"] = False
    # 立刻移出並寫回存檔，不等迴圈醒來——否則重啟會把停掉的策略撈回來（幽靈策略）
    STRATS.pop(tg[0], None); TASKS.pop(tg[0], None)
    save_state(True)
    if p:
        await reply(u, f"{E.BOT} /stop（持倉中）\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                       f"⚠ 已進場不自動平倉\n倉位 {p['pos']} 張 均價 {p.get('avgPx','?')} 浮 {p.get('upl','?')}\n"
                       f"請至 OKX 手動平倉")
    else:
        await reply(u, f"{E.BOT} /stop\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n已停止")

async def cmd_stopall(u, c):
    alive = [k for k, s in STRATS.items() if s.get("alive")]
    held = []; done = []
    for k in list(alive):
        S = STRATS[k]; d = S["dir"]; iid = S["spec"]["iid"]
        ps = "long" if d == "L" else "short"
        p = await okx_pos(iid, ps)
        S["alive"] = False
        STRATS.pop(k, None); TASKS.pop(k, None)
        (held if p else done).append(f"{S['sym']} {S['dir']}")
    orphan = await sweep_h()
    save_state(True)
    m = f"{E.BOT} /stopall（均K）\n━━━━━━━━━━\n"
    if done: m += f"已停止策略（{len(done)}）：\n" + "\n".join("・" + x for x in done) + "\n"
    if orphan: m += f"另清除均K 殘留掛單：{orphan} 筆\n"
    if held: m += f"⚠ 持倉需手動平倉（{len(held)}）：\n" + "\n".join("・" + x for x in held) + "\n"
    if not done and not orphan and not held: m += "目前無策略、無殘單\n"
    m += "（普K 不受影響）\n"
    await reply(u, m + f"時間：{hhmmss()}")

async def cmd_status(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    posr = await api("GET", "/api/v5/account/positions")
    bal = await api("GET", "/api/v5/account/balance")
    eq = av = "?"
    if bal.get("code") == "0":
        x = next((d for d in bal["data"][0].get("details", []) if d["ccy"] == "USDT"), None)
        if x:
            eq = f"{Decimal(x.get('eq','0')):.4f}"
            av = f"{Decimal(x.get('availEq') or x.get('availBal') or '0'):.4f}"
    pl = [p for p in posr.get("data", []) if float(p.get("pos", "0")) != 0] if posr.get("code") == "0" else []
    alive = [s for s in STRATS.values() if s.get("alive")]
    L = [f"{E.BOT} OKX均K｜{ACCT}", "事件：現況（即時查 OKX）", "━━━━━━━━━━",
         f"USDT權益：{eq}", f"可用餘額：{av}", f"帳戶週期：{ACCOUNT_TF}",
         f"均K 策略：{len(alive)} 個"]
    for s in alive:
        k = skey(s["sym"], s["dir"]); placed, entered = get_stat(k)
        live = "持倉中" if s.get("pos_open") else "等訊號"
        hb = s.get("hb")
        age = f"{int(time.time()-hb)}s" if hb else "-"
        L.append(f"{E.dir_emoji(s['dir'])} {s['sym']}：{live}(進{entered}/{placed}) 心跳{age}")
        L.append(f"　　策略　　：{strat_params(s['sym'], s['dir'])}")
    L += ["━━━━━━━━━━", f"OKX 帳戶總持倉：{len(pl)}（含普K）"]
    for p in pl:
        d = "L" if p["posSide"] == "long" else "S"
        L.append(f"{E.dir_emoji(d)} {p['instId'].replace('-USDT-SWAP','USDT')} {d} {p['pos']}張")
    L += ["━━━━━━━━━━", f"時間：{hhmmss()} UTC+8"]
    await reply(u, "\n".join(L))

# ---------- /summary ----------
def sum_lines(rs, entered):
    L = []
    m = len(rs)
    win = [r for r in rs if Decimal(str(r.get("net") or "0")) > 0]
    los = [r for r in rs if Decimal(str(r.get("net") or "0")) < 0]
    wr = (len(win) / m * 100) if m else 0
    L.append("進場數：%d | 已出場：%d" % (entered, m))
    L.append("勝率：%.2f%%（勝 %d / 敗 %d）" % (wr, len(win), len(los)))
    if m:
        L.append("平均持倉：%d秒（約 %.1f 根）" % (
            sum(int(r.get("hold_s") or 0) for r in rs) / m,
            sum(float(r.get("bars") or 0) for r in rs) / m))
    else:
        L.append("平均持倉：-")
    if win:
        L.append("　平均獲利：%+.6f" % (sum(Decimal(str(r.get("net") or "0")) for r in win) / len(win)))
    if los:
        L.append("　平均虧損：%+.6f" % (sum(Decimal(str(r.get("net") or "0")) for r in los) / len(los)))
    L.append("━" * 10)
    tg = sum((Decimal(str(r.get("gross") or "0")) for r in rs), Decimal(0))
    tf = sum((Decimal(str(r.get("fee") or "0")) for r in rs), Decimal(0))
    tn = sum((Decimal(str(r.get("net") or "0")) for r in rs), Decimal(0))
    nv = sum((Decimal(str(r.get("nv") or "0")) for r in rs), Decimal(0))
    gp = (tg / nv * 100) if nv else Decimal(0)
    fp = (tf / nv * 100) if nv else Decimal(0)
    npc = (tn / nv * 100) if nv else Decimal(0)
    L.append("毛損益：%+.6f (%+.3f%%)" % (tg, gp))
    L.append("手續費：%+.6f (%+.3f%%)" % (tf, fp))
    L.append("淨損益：%+.6f (%+.3f%%) %s" % (tn, npc, E.pnl_emoji(tn)))
    return L

async def cmd_summary(u, c):
    t = today8(); recs = load_trades(t)
    ts = {k: v for k, v in STATS.items() if str(v.get("date")) == str(t)}
    L = [f"{E.BOT} OKX均K｜{ACCT}", f"📊📊📊 Summary {t}"]
    for dr in ("L", "S"):
        rows = [r for r in recs if r["dir"] == dr]
        en = sum(v["entered"] for k, v in ts.items() if k.endswith("_" + dr))
        L.append("━" * 10)
        L.append(f"{E.dir_emoji(dr)} {E.dir_word(dr)}")
        L += sum_lines(rows, en)
    L += ["━" * 10, f"時間：{hhmmss()}"]
    await reply(u, "\n".join(L))
    for sy in sorted({r["sym"] for r in recs}):
        D = [f"\U0001f49a\U0001f499\U0001fa75\U0001f49c {sy} {t}"]
        for dr in ("L", "S"):
            rows = [r for r in recs if r["sym"] == sy and r["dir"] == dr]
            st_ = ts.get(skey(sy, dr)) or {"placed": 0, "entered": 0}
            D.append("━" * 10)
            D.append(f"策略：{E.dir_emoji(dr)} {strat_params(sy, dr)}")
            D += sum_lines(rows, st_["entered"])
        D += ["━" * 10, f"時間：{hhmmss()}"]
        await reply(u, "\n".join(D))

def _fmt_atr(v, tick):
    """ATR 依商品 tick 決定小數位，避免大幣印一堆 0 或小幣被截斷。"""
    if v is None: return "-"
    exp = -tick.as_tuple().exponent
    q = Decimal(1).scaleb(-max(2, min(8, exp + 1)))
    return str(v.quantize(q))

def build_ha_xlsx(sym, tf, ha, atrs, tick, path):
    """明細＋統計兩張表。欄位：幣種 / 日期 / 時間 / 燈號 / ATR14 / ATR14 ratio"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active; ws.title = "明細"
    heads = ["幣種", "週期", "日期", "時間", "燈號", "ATR14", "ATR14 ratio"]
    ws.append(heads)
    hf = Font(bold=True, color="FFFFFF")
    hfill = PatternFill("solid", fgColor="404040")
    for i in range(1, len(heads) + 1):
        cc = ws.cell(row=1, column=i); cc.font = hf; cc.fill = hfill
        cc.alignment = Alignment(horizontal="center")
    exp = -tick.as_tuple().exponent
    ndp = max(2, min(8, exp + 1))
    afmt = "0." + "0" * ndp
    for i, x in enumerate(ha):
        dt = datetime.fromtimestamp(int(x["ts"]) / 1000, TZ8)
        av, rv = atrs[i]
        up = x["color"] == "G"
        ws.append([sym, tf, dt.strftime("%Y/%m/%d"), dt.strftime("%H:%M"),
                   "\U0001F7E9" if up else "\U0001F7E5",
                   float(av) if av is not None else None,
                   float(rv) if rv is not None else None])
        r = ws.max_row
        ws.cell(row=r, column=2).alignment = Alignment(horizontal="center")
        ws.cell(row=r, column=5).alignment = Alignment(horizontal="center")
        ws.cell(row=r, column=6).number_format = afmt
        ws.cell(row=r, column=7).number_format = '0.0000"%"' 
    ws.freeze_panes = "A2"
    for i, w in enumerate([12, 8, 12, 8, 8, 16, 16], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    va = [float(a) for a, _ in atrs if a is not None]
    vr = [float(r) for _, r in atrs if r is not None]
    ng = sum(1 for x in ha if x["color"] == "G")
    def stat(v):
        if not v: return ("-", "-", "-", "-")
        sv = sorted(v); m = len(sv)
        med = sv[m // 2] if m % 2 else (sv[m // 2 - 1] + sv[m // 2]) / 2
        return (sum(v) / m, med, max(v), min(v))
    aA, aM, aX, aN = stat(va)
    rA, rM, rX, rN = stat(vr)
    t0 = datetime.fromtimestamp(int(ha[0]["ts"]) / 1000, TZ8).strftime("%Y/%m/%d %H:%M")
    t1 = datetime.fromtimestamp(int(ha[-1]["ts"]) / 1000, TZ8).strftime("%Y/%m/%d %H:%M")
    w2 = wb.create_sheet("統計")
    rows = [["項目", "數值"],
            ["幣種", sym], ["週期", tf], ["根數", len(ha)],
            ["期間起", t0], ["期間迄", t1],
            ["\U0001F7E9 根數", ng], ["\U0001F7E5 根數", len(ha) - ng],
            ["\U0001F7E9 佔比", round(ng / len(ha) * 100, 2) if ha else 0],
            ["ATR14 平均", aA], ["ATR14 中位", aM], ["ATR14 最大", aX], ["ATR14 最小", aN],
            ["ATR14 ratio 平均", rA], ["ATR14 ratio 中位", rM],
            ["ATR14 ratio 最大", rX], ["ATR14 ratio 最小", rN],
            ["燈號來源", "Heikin-Ashi"], ["ATR 來源", "原始 K 線（Wilder 平滑）"],
            ["產生時間", now8().strftime("%Y/%m/%d %H:%M:%S")]]
    for r in rows: w2.append(r)
    for rr in range(2, w2.max_row + 1):
        lab = w2.cell(row=rr, column=1).value or ""
        cell = w2.cell(row=rr, column=2)
        if "ratio" in str(lab) and isinstance(cell.value, (int, float)):
            cell.number_format = '0.0000"%"'
        elif "佔比" in str(lab) and isinstance(cell.value, (int, float)):
            cell.number_format = '0.00"%"'
        elif str(lab).startswith("ATR14 ") and isinstance(cell.value, (int, float)):
            cell.number_format = afmt
    for i in range(1, 3):
        cc = w2.cell(row=1, column=i); cc.font = hf; cc.fill = hfill
    w2.column_dimensions["A"].width = 24
    w2.column_dimensions["B"].width = 24
    wb.save(path)

def send_ha_mail(path, name, sym, tf, m, aA, rA):
    """寄出均K 燈號+ATR 報表。回傳 (ok, 訊息)。"""
    import smtplib
    from email.message import EmailMessage
    env = {}
    try:
        for line in open("/srv/1111bot/.env"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        return False, "讀 .env 失敗：%s" % e
    user = env.get("GMAIL_USER")
    pwd = (env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = env.get("REPORT_TO") or user
    if not user or not pwd:
        return False, "未設定 GMAIL_USER / GMAIL_APP_PASSWORD"
    msg = EmailMessage()
    msg["Subject"] = "OKX %s 均K燈號+ATR %s %s（%d 根）" % (ACCT, sym, tf, m)
    msg["From"] = user; msg["To"] = to
    msg.set_content(
        "商品 %s\n週期 %s\n根數 %d\n"
        "ATR14 平均 %s\nATR14 ratio 平均 %s%%\n"
        "欄位：幣種 / 日期 / 時間 / 燈號 / ATR14 / ATR14 ratio\n"
        "燈號取 Heikin-Ashi，ATR 取原始 K 線\n" % (sym, tf, m, aA, rA))
    msg.add_attachment(open(path, "rb").read(),
                       maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename=name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as sv:
        sv.login(user, pwd); sv.send_message(msg)
    return True, to

async def cmd_ha(u, c):
    """均K 燈號+ATR 報表（Excel 寄信）。用法：/ha ETHUSDT [根數 3~2000]"""
    if not c.args:
        await reply(u, f"{E.BOT} 用法：/ha ETHUSDT 900\n"
                       f"根數 3~{HA_MAX}（預設 300）\n"
                       f"產生 Excel（明細＋統計）寄到信箱\n"
                       f"欄位：幣種/日期/時間/燈號/ATR14/ATR14 ratio\n"
                       f"※ 燈號取 HA，ATR 取原始 K 線\n"
                       f"目前週期：{ACCOUNT_TF}")
        return
    sym = c.args[0].upper()
    n = 300
    if len(c.args) >= 2:
        try: n = max(3, min(HA_MAX, int(c.args[1])))
        except Exception: pass
    try:
        spec = await get_spec(sym)
    except Exception:
        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return
    await reply(u, f"{E.BOT} 產生 {sym} {ACCOUNT_TF} 燈號+ATR 報表中（{n} 根），請稍候…")
    try:
        kl = await klines_paged_for_tf(spec["iid"], ACCOUNT_TF, n + 20)   # +20 給 ATR14 暖身
    except Exception as e:
        await reply(u, f"{E.LOSS} K 線取得失敗：{type(e).__name__}: {e}"); return
    if not kl:
        await reply(u, f"{E.BOT} {sym} K 線取得失敗"); return
    ha_all = calc_ha(kl)
    atr_all = calc_atr(kl, 14)
    st = max(0, len(ha_all) - n)
    ha = ha_all[st:]; atrs = atr_all[st:]
    m = len(ha)
    if m < 3:
        await reply(u, f"{E.BOT} {sym} {ACCOUNT_TF} 可用 K 線僅 {m} 根，不足以產表"); return
    day = now8().strftime("%Y%m%d")
    name = f"OKX_{ACCT}_均K_{sym}_{ACCOUNT_TF}_{m}根_{day}.xlsx"
    path = f"/srv/1111bot/data/{name}"
    try:
        build_ha_xlsx(sym, ACCOUNT_TF, ha, atrs, spec["tick"], path)
    except Exception as e:
        await reply(u, f"{E.LOSS} 產生 Excel 失敗：{type(e).__name__}: {e}"); return
    va = [a for a, _ in atrs if a is not None]
    vr = [r for _, r in atrs if r is not None]
    aA = (sum(va) / len(va)) if va else Decimal(0)
    rA = (sum(vr) / len(vr)) if vr else Decimal(0)
    aAs = _fmt_atr(aA, spec["tick"]); rAs = f"{rA:.4f}"
    try:
        ok, info = send_ha_mail(path, name, sym, ACCOUNT_TF, m, aAs, rAs)
    except Exception as e:
        await reply(u, f"{E.LOSS} 寄送失敗：{type(e).__name__}: {e}\n檔案已存於 VPS：{name}"); return
    if not ok:
        await reply(u, f"{E.LOSS} 未寄送：{info}\n檔案已存於 VPS：{name}"); return
    t0 = datetime.fromtimestamp(int(ha[0]["ts"]) / 1000, TZ8).strftime("%m/%d %H:%M")
    t1 = datetime.fromtimestamp(int(ha[-1]["ts"]) / 1000, TZ8).strftime("%m/%d %H:%M")
    ng = sum(1 for x in ha if x["color"] == "G")
    await reply(u, f"{E.BOT} ✅ 均K 報表已寄出\n"
                   f"{sym} {ACCOUNT_TF}｜{m} 根\n"
                   f"期間：{t0} ~ {t1}\n"
                   f"燈號：🟩{ng} / 🟥{m - ng}\n"
                   f"ATR14 平均 {aAs}\n"
                   f"ATR14 ratio 平均 {rAs}%\n"
                   f"時間：{hhmmss()}")

async def cmd_db(u, c):
    """行情DB 狀態。用法：/db 或 /db ETHUSDT 或 /db ETHUSDT 5m"""
    if DB is None:
        await reply(u, f"{E.BOT} 行情DB 尚未就緒"); return
    sym = c.args[0].upper() if c.args else None
    tf = c.args[1] if len(c.args) >= 2 else None
    if tf and tf not in HA_TF:
        await reply(u, f"{E.BOT} 週期須為：" + " / ".join(TF_LIST)); return
    ms = db_meta(sym, tf)
    if not ms:
        await reply(u, f"{E.BOT} 查無資料（收集器可能還在初始化）"); return
    if sym and tf:
        rows = db_latest(sym, tf, 5)
        L = [f"{E.BOT} OKX均K｜{ACCT}", f"事件：行情DB {sym} {tf}", "━" * 10,
             f"筆數：{ms[0]['bars']}｜新鮮：{'✅' if db_fresh(sym, tf) else '⚠ 落後'}",
             "━" * 10, "最近 5 根："]
        for r in rows:
            lg = "\U0001F7E9" if r["color"] == "G" else "\U0001F7E5"
            ar = f"{r['atr14_ratio']:.4f}%" if r["atr14_ratio"] is not None else "-"
            L.append(f"{r['dt']} {lg} 連{r['streak']}")
            L.append(f"　實體{r['body_pct']:+.4f}% 振幅{r['range_pct']:.4f}% ATRr {ar}")
            L.append(f"　連續段累計 實體{r['streak_body']:+.4f}% 振幅{r['streak_range']:.4f}%")
        L += ["━" * 10, f"時間：{hhmmss()}"]
        await reply(u, "\n".join(L)); return
    tot = sum(m["bars"] or 0 for m in ms)
    stale = [m for m in ms if not db_fresh(m["sym"], m["tf"])]
    errs = [m for m in ms if m.get("err")]
    L = [f"{E.BOT} OKX均K｜{ACCT}", "事件：行情DB 狀態", "━" * 10,
         f"組合數：{len(ms)}（{len(db_symbols())} 幣種 x {len(TF_LIST)} 週期）",
         f"總筆數：{tot}",
         f"新鮮：{len(ms) - len(stale)}｜落後：{len(stale)}",
         f"錯誤：{len(errs)}"]
    if sym:
        L += ["━" * 10, f"{sym} 各週期："]
        for m in ms:
            fr = "✅" if db_fresh(m["sym"], m["tf"]) else "⚠"
            last = datetime.fromtimestamp((m["last_ts"] or 0) / 1000, TZ8).strftime("%m/%d %H:%M") if m["last_ts"] else "-"
            L.append(f"{fr} {m['tf']:>6}｜{m['bars']} 根｜最新 {last}")
    else:
        if stale:
            L += ["━" * 10, "落後的組合（前 10）："]
            for m in stale[:10]:
                L.append(f"⚠ {m['sym']} {m['tf']}")
        if errs:
            L += ["━" * 10, "錯誤（前 5）："]
            for m in errs[:5]:
                L.append(f"{E.LOSS} {m['sym']} {m['tf']}：{str(m['err'])[:40]}")
        L += ["━" * 10, "細節：/db ETHUSDT 或 /db ETHUSDT 5m"]
    L += ["━" * 10, f"時間：{hhmmss()}"]
    await reply(u, "\n".join(L))

async def cmd_coins(u, c):
    on = [s["symbol"] for s in SYMS if s["enabled"]]
    L = [f"{E.BOT} OKX均K｜{ACCT}", "事件：幣種清單（即時）", "━━━━━━━━━━"]
    for sym in on:
        try:
            sp = await get_spec(sym); last = await get_last(sp["iid"])
            mm = sp["minsz"] * sp["ctval"] * last
            L.append(f"{sym}｜最小{sp['minsz']}張｜{mm:.4f}U")
        except Exception:
            L.append(f"{sym}｜查詢失敗")
    L += ["━━━━━━━━━━", f"時間：{hhmmss()}"]
    await reply(u, "\n".join(L))

async def cmd_timeframe(u, c):
    global ACCOUNT_TF
    opts = " / ".join(TF_LIST)
    if not c.args:
        L = [f"{E.BOT} 目前週期：{ACCOUNT_TF}", "━" * 10, "可選週期："]
        for t in TF_LIST:
            sec, bar, mul = HA_TF[t]
            src = f"OKX {bar}" if mul == 1 else f"{mul}x OKX {bar} 合成"
            L.append(f"・{t}（{src}）")
        L += ["━" * 10, "變更：/timeframe 60m",
              "※ 僅影響之後新建立的策略",
              "※ 720m/1440m 以 UTC+8 為日界，與 OKX App 一致"]
        await reply(u, "\n".join(L)); return
    tf = c.args[0]
    if tf not in HA_TF:
        await reply(u, f"{E.BOT} 週期須為：{opts}"); return
    ACCOUNT_TF = tf; save_state()
    await reply(u, f"{E.BOT} ✅ 預設週期已設為 {tf}\n（/run 現在自帶週期參數，此設定僅作參考預設）")

async def cmd_menu(u, c):
    await reply(u, f"{E.BOT} OKX均K｜{ACCT}\n使用說明\n━━━━━━━━━━\n"
        "/run 商品 方向 週期 槓桿 保證金 前根數 後根數 前段上限 後段下限 回吐%\n"
        "例：/run BTCUSDT L 5m 1 100 5 3 0.5 1.5 -2%\n"
        "/confirm 確認啟動\n/stop 商品 方向\n/stopall 停全部\n"
        "/status 策略現況\n/summary 當日戰報\n"
        "/ha 商品 根數  燈號+ATR 報表 Excel 寄信（3~2000根）\n"
        "/db 行情DB狀態（/db ETHUSDT 5m 看細節）\n"
        f"/timeframe 預設週期 共{len(TF_LIST)}種\n/coins 幣種\n"
        "━━━━━━━━━━\n"
        "位移 = |Σ均K實體漲跌幅| / Σ ATR14 ratio\n"
        "進場：前段位移 ≤ 上限（盤整）\n"
        "　　　後段同色且位移 ≥ 下限（突破）\n"
        "出場：從最高利潤回吐指定% 即 taker 平倉\n"
        "⚠ 無 TP/SL/TE，僅靠回吐出場\n"
        "✅ 判斷全讀本機行情DB，下單不等 API\n"
        "✅ 重啟接管持倉\n"
        "✅ 與原K 完全獨立，不互相撤單")

async def cmd_unknown(u, c):
    await reply(u, f"{E.BOT} 指令無法辨識：{u.message.text}\n請用 /menu")

# ---------- 每日自動 summary ----------
class _M:
    def __init__(self, app, chat): self._a = app; self._c = chat
    async def reply_text(self, t): await self._a.bot.send_message(self._c, t)
class _U:
    def __init__(self, app, chat): self.message = _M(app, chat)

async def job_summary(ctx):
    if not CHAT_ID: return
    try: await cmd_summary(_U(ctx.application, CHAT_ID), ctx)
    except Exception as e: print("auto summary fail", e)

# ---------- 啟動 ----------
async def _post_stop(app):
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    print("收到停止訊號，保留策略存檔供重啟接管")

async def _post_init(app):
    global HTTP
    HTTP = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), limits=httpx.Limits(max_connections=40))
    CMDS = [BotCommand("run", "建立均K策略"),
            BotCommand("stop", "停指定"), BotCommand("stopall", "停全部"),
            BotCommand("status", "現況"), BotCommand("summary", "當日戰報"),
            BotCommand("ha", "燈號+ATR14 3~2000根"), BotCommand("timeframe", "週期"),
            BotCommand("coins", "幣種"), BotCommand("menu", "說明")]
    scopes = [BotCommandScopeDefault(), BotCommandScopeAllPrivateChats()]
    try:
        saved = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
        ch = saved.get("chat")
        if ch: scopes.append(BotCommandScopeChat(ch))
    except Exception:
        pass
    for sc in scopes:
        try: await app.bot.delete_my_commands(scope=sc)
        except Exception as e: print("delete cmds fail", type(sc).__name__, e)
    await app.bot.set_my_commands(CMDS)
    print(f"左下 Menu 已更新（已清除 {len(scopes)} 個 scope 的舊指令）")
    try:
        jq = app.job_queue
        if jq:
            t2359 = datetime.strptime("23:59", "%H:%M").time().replace(tzinfo=TZ8)
            jq.run_daily(job_summary, time=t2359, name="daily_summary")
            print("已排程：每日 23:59 自動 /summary")
    except Exception as e:
        print("schedule fail", e)
    try:
        db_open()
    except Exception as e:
        print("行情DB 開啟失敗", type(e).__name__, e)
    await startup_recover(app)
    hb = asyncio.create_task(hb_watch(app))
    _BG.add(hb); hb.add_done_callback(_BG.discard)
    print("心跳看門狗已啟動")
    col = asyncio.create_task(collector(app))
    _BG.add(col); col.add_done_callback(_BG.discard)

def main():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    print(f"啟動 {ACCT} 均K B6-2（token ...{TOKEN[-6:]}）")
    app = (Application.builder().token(TOKEN).post_init(_post_init).post_stop(_post_stop)
           .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0)
           .pool_timeout(30.0).get_updates_read_timeout(40.0)
           .get_updates_connect_timeout(30.0).build())
    for cmd, fn in [(["menu", "start"], cmd_menu), ("run", cmd_run), ("confirm", cmd_confirm),
                    ("stop", cmd_stop), ("stopall", cmd_stopall), ("status", cmd_status),
                    ("summary", cmd_summary), ("ha", cmd_ha), ("db", cmd_db),
                    ("timeframe", cmd_timeframe), ("coins", cmd_coins)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.run_polling()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""B6-2 均K（Heikin-Ashi）｜o3333o — 獨立進程
規格：
  1. 每根 K 線收線後 +5 秒抓 K 線，算 HA，判燈號。
  2. 進場：PRE根反轉前色 + POST根反轉後色 + POST振幅累加達門檻 -> taker 市價進場。
  3. 出場：EXIT根反向色 + 振幅累加達門檻 -> taker 市價平倉。無 TP/SL/TE。
  4. clOrdId 前綴：進場 h / 出場 y（普K 用 n / x，互不干擾）。
  5. 心跳：超過 3 根 K 線未推進燈號判定即 TG 告警。
  6. OKX 為唯一真相來源；重啟接管既有持倉；Telegram 為旁路，發送失敗不影響交易。
注意：本檔完全獨立於 run_bot.py（普K），使用自己的 bot token 與 state 檔。
"""
import sys, hmac, base64, hashlib, json, time, asyncio, uuid, os
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN
from datetime import datetime, timezone, timedelta
import httpx
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, MessageHandler, filters
sys.path.insert(0, "/srv/1111bot")
from app.core import emoji as E
from app.strategy.normal import next_open_epoch, TF_SEC
from app.strategy.ha import calc_ha, merge_5m_to_10m
from app.strategy.heikin import judge_entry, judge_exit

BASE = "https://www.okx.com"
ACCT = "o3333o"
TZ8 = timezone(timedelta(hours=8))
ACCOUNT_TF = "5m"
STATE_FILE = "/srv/1111bot/data/strategies_ha_o3333o.json"
HA_LAG = 5           # 收線後幾秒才抓 K 線（等 OKX 資料落定）
HA_HIST = 120        # 抓幾根歷史 K 線做 HA 遞迴
HB_BARS = 3          # 心跳超過幾根 K 線未推進就告警

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
SAVE_FIELDS = ("sym","dir","tf","lev","margin","pre","post","exitn","amp","chat",
               "pos_open","pos_px","pos_ee","pos_sz","last_bar")

def save_state():
    try:
        data = {"chat": CHAT_ID, "tf": ACCOUNT_TF, "stats": STATS, "strats": []}
        for k, S in STRATS.items():
            if S.get("alive"):
                data["strats"].append({a: S[a] for a in SAVE_FIELDS if a in S})
        if STRATS and not data["strats"]:
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

async def klines_and_ha(iid, tf, limit=HA_HIST):
    """回傳 (原始K線, HA序列)，兩者索引一一對應，皆為舊->新。
    10m 由 5m 合成並對齊 10 分鐘邊界。"""
    if tf == "10m":
        kl = await get_klines(iid, "5m", limit * 2 + 10)
        while kl and int(kl[0]["ts"]) % 600000 != 0:
            kl.pop(0)
        if len(kl) % 2: kl = kl[:-1]
        kl = merge_5m_to_10m(kl)
    else:
        kl = await get_klines(iid, tf, limit)
    if not kl: return [], []
    return kl, calc_ha(kl)

async def ha_series(iid, tf, limit=HA_HIST):
    """只要 HA 序列時用這個。"""
    kl, ha = await klines_and_ha(iid, tf, limit)
    return ha

def calc_atr(kl, period=14):
    """在【原始 K 線】上算 ATR14（Wilder 平滑）與 ATR ratio(%)。
    技術指標一律用正統 K 線，不使用 HA 平滑值。
    TR = max(h-l, |h-prev_c|, |l-prev_c|)
    前 period 根取 TR 簡單平均為種子，之後 ATR = (前ATR*(n-1) + TR)/n
    回傳與 kl 等長的 [(atr, atr_ratio), ...]，資料不足處為 (None, None)。"""
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
        p = await okx_pos(iid, pos)
        if not p:
            await notify(app, S["chat"], f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 進場未確認成交，跳過本輪")
            return False
        fpx = Decimal(p.get("avgPx") or await get_last(iid))
    bump(k, "entered")
    ee = time.time()
    S["state"] = "持倉中"; S["pos_open"] = True
    S["pos_px"] = str(fpx); S["pos_ee"] = ee; S["pos_sz"] = str(size)
    save_state()
    seq = "".join(x["color"] for x in info["seg"]) if info else "-"
    await notify(app, S["chat"],
        f"{E.BOT} OKX均K｜{ACCT}\n事件：🔔 訊號進場（taker）\n"
        f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)} {S['lev']}x\n"
        f"週　　期：{S['tf']}\n進場價格：{fpx}\n下單張數：{size}\n"
        f"燈號序列：{seq}（PRE{S['pre']}+POST{S['post']}）\n"
        f"振幅累加：{info['amp_sum']:.4f}% ≥ {pct(S['amp'])}%\n"
        f"出場條件：EXIT{S['exitn']} 根反向 + 振幅 ≥ {pct(S['amp'])}%\n"
        f"⚠ 無 TP/SL/TE，僅靠反向訊號出場\n"
        f"狀　　態：📌 持倉中\n時間：{hhmmss()}")
    return True

# ---------- 出場 ----------
async def h_exit(app, S, spec, iid, d, pos, size, fpx, ee, reason, k):
    cs = "sell" if d == "L" else "buy"
    p_now = await okx_pos(iid, pos)
    if not p_now:
        await notify(app, S["chat"], f"{E.BOT} {S['sym']} {E.dir_word(d)} 平倉時 OKX 已無持倉，略過")
        for a in ("pos_open", "pos_px", "pos_ee", "pos_sz"):
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
    log_trade({"date": today8(), "sym": S["sym"], "dir": d, "reason": reason,
               "hold_s": hs, "bars": round(hs / TF_SEC[S["tf"]], 1),
               "gross": str(g), "fee": str(fee), "net": str(net), "nv": str(nv),
               "src": src, "ts": hhmmss(),
               "in_ts": datetime.fromtimestamp(ee, TZ8).strftime("%H:%M:%S"),
               "tf": S["tf"], "pre": str(S["pre"]), "post": str(S["post"]),
               "exitn": str(S["exitn"]), "amp": str(S["amp"]),
               "in_px": str(fpx), "out_px": str(xpx)})
    await notify(app, S["chat"],
        f"{E.BOT} OKX均K｜{ACCT}\n事件：{'🟢' if net >= 0 else '🔴'} 已出場\n"
        f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n出場原因：{reason}\n"
        f"進場價：{fpx}\n出場價：{xpx}\n"
        f"持倉秒數：{hs}s（約 {hs / TF_SEC[S['tf']]:.1f} 根）\n"
        f"毛損益：{g:+.6f} ({gp:+.3f}%)\n手續費：{fee:+.6f} ({fp:+.3f}%)\n"
        f"淨損益：{net:+.6f} ({npv:+.3f}%) {E.pnl_emoji(net)}\n時間：{hhmmss()}")
    for a in ("pos_open", "pos_px", "pos_ee", "pos_sz"):
        S.pop(a, None)
    save_state()
    return True

# ---------- 重啟接管 ----------
async def h_takeover(app, S, spec, iid, d, pos):
    p = await okx_pos(iid, pos)
    if not p:
        for a in ("pos_open", "pos_px", "pos_ee", "pos_sz"):
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
async def hloop(app, chat, S):
    spec = S["spec"]; iid = spec["iid"]; d = S["dir"]
    pos = "long" if d == "L" else "short"
    k = skey(S["sym"], d)
    need = max(int(S["pre"]) + int(S["post"]), int(S["exitn"]))
    size = fpx = None; ee = None
    try:
        if S.get("pos_open"):
            tk = await h_takeover(app, S, spec, iid, d, pos)
            if tk: size, fpx, ee = tk

        while S["alive"]:
            tf_sec = TF_SEC[S["tf"]]
            oe = next_open_epoch(int(time.time()), S["tf"])
            S["state"] = "持倉中" if S.get("pos_open") else "等訊號"
            save_state()
            w = oe + HA_LAG - time.time()
            if w > 0: await asyncio.sleep(w)
            if not S["alive"]: break

            want = (oe - tf_sec) * 1000
            ha = []
            for _ in range(6):
                ha = await ha_series(iid, S["tf"])
                if ha and int(ha[-1]["ts"]) >= want: break
                await asyncio.sleep(2)
            S["hb"] = time.time(); S["hb_warned"] = False
            if len(ha) < need:
                print("歷史K不足", S["sym"], len(ha), "<", need)
                continue
            bar_ts = int(ha[-1]["ts"])
            if bar_ts == S.get("last_bar"):
                continue
            S["last_bar"] = bar_ts; save_state()
            idx = len(ha) - 1

            if S.get("pos_open"):
                p = await okx_pos(iid, pos)
                if not p:
                    await notify(app, chat, f"{E.BOT} {S['sym']} {E.dir_word(d)} OKX 已無持倉（可能手動平倉），重回等訊號")
                    for a in ("pos_open", "pos_px", "pos_ee", "pos_sz"):
                        S.pop(a, None)
                    size = fpx = ee = None
                    save_state(); continue
                try:
                    size = abs(Decimal(p.get("pos") or "0"))
                except Exception:
                    pass
                if fpx is None: fpx = Decimal(S.get("pos_px") or p.get("avgPx") or "0")
                if ee is None: ee = float(S.get("pos_ee") or time.time())
                r = judge_exit(ha, d, int(S["exitn"]), S["amp"], idx)
                if r and r["hit"]:
                    await h_exit(app, S, spec, iid, d, pos, size, fpx, ee, "Signal_Exit", k)
                    size = fpx = ee = None
            else:
                r = judge_entry(ha, d, int(S["pre"]), int(S["post"]), S["amp"], idx)
                if r and r["hit"]:
                    ok = await h_open(app, S, spec, iid, d, pos, r, k)
                    if ok:
                        fpx = Decimal(S["pos_px"]); ee = float(S["pos_ee"])
                        size = Decimal(str(S["pos_sz"]))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("hloop error", S.get("sym"), S.get("dir"), type(e).__name__, e)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 循環錯誤：{type(e).__name__}: {e}")
    finally:
        S["state"] = "已停止"; S["alive"] = False
        STRATS.pop(k, None); TASKS.pop(k, None); save_state()
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
                lim = TF_SEC[S["tf"]] * HB_BARS
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
         "pre": int(d["pre"]), "post": int(d["post"]), "exitn": int(d["exitn"]),
         "amp": Decimal(str(d["amp"])), "spec": spec,
         "alive": True, "state": "等訊號", "chat": d.get("chat", CHAT_ID),
         "hb": time.time(), "hb_warned": False}
    for a in ("pos_open", "pos_px", "pos_ee", "pos_sz", "last_bar"):
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
    return (f"{dr} {S['lev']}x {pct(S['margin'])} "
            f"P{S['pre']}/{S['post']}/E{S['exitn']} amp{pct(S['amp'])}")

async def cmd_run(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args
    fmt = (f"用法：/run 商品 方向 槓桿 保證金 PRE POST EXIT 振幅\n"
           f"例：/run ETHUSDT L 1x 3 2 3 2 0.3\n"
           f"PRE=反轉前色根數 POST=反轉後色根數\nEXIT=反向出場根數 振幅=%\n"
           f"（週期依 /timeframe，目前 {ACCOUNT_TF}）")
    if len(a) != 8: await reply(u, f"{E.BOT} 參數數量錯誤（需8個）\n{fmt}"); return
    try:
        sym = a[0].upper(); dr = a[1].upper(); lev = int(a[2].replace("x", ""))
        margin = Decimal(a[3]); pre = int(a[4]); post = int(a[5])
        exitn = int(a[6]); amp = Decimal(a[7])
    except Exception:
        await reply(u, f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if dr not in ("L", "S"): await reply(u, f"{E.BOT} 方向須 L 或 S"); return
    for nm, v in (("PRE", pre), ("POST", post), ("EXIT", exitn)):
        if not 1 <= v <= 20: await reply(u, f"{E.BOT} {nm} 須 1~20"); return
    if amp < 0: await reply(u, f"{E.BOT} 振幅不可為負"); return
    k = skey(sym, dr)
    if k in STRATS and STRATS[k].get("alive"):
        await reply(u, f"{E.BOT} {sym} {E.dir_word(dr)} 已在運行"); return
    try: spec = await get_spec(sym)
    except Exception: await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return
    op = await get_last(spec["iid"])
    size = csize(margin, Decimal(lev), op, spec["ctval"], spec["lot"])
    if size < spec["minsz"]:
        need = spec["minsz"] * spec["ctval"] * op / Decimal(lev)
        await reply(u, f"{E.BOT} {E.LOSS} 保證金不足：算出 {size} 張 < 最小 {spec['minsz']}\n此槓桿下至少需約 {need:.4f} USDT"); return
    try:
        ha = await ha_series(spec["iid"], ACCOUNT_TF)
        seq = "".join(x["color"] for x in ha[-(pre + post):]) if len(ha) >= pre + post else "-"
        amps = sum((x["amp"] for x in ha[-post:]), Decimal(0)) if len(ha) >= post else Decimal(0)
        prev = f"目前燈號：{seq}\n近{post}根振幅：{amps:.4f}%"
    except Exception as e:
        prev = f"燈號預覽失敗：{type(e).__name__}"
    ps = "long" if dr == "L" else "short"
    exist = await okx_pos(spec["iid"], ps)
    warn = f"\n⚠ OKX 上 {sym} {E.dir_word(dr)} 已有 {exist['pos']} 張持倉\n　（可能是普K 或手動單，倉位會被合併）" if exist else ""
    PENDING[u.effective_chat.id] = {"t": time.time(), "sym": sym, "dir": dr, "tf": ACCOUNT_TF,
        "lev": lev, "margin": margin, "pre": pre, "post": post, "exitn": exitn, "amp": amp, "spec": spec}
    await reply(u, f"{E.BOT} OKX均K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
        f"商　　品：{E.dir_emoji(dr)} {sym} {E.dir_word(dr)} {lev}x\n週　　期：{ACCOUNT_TF}\n"
        f"目前價格：{op}\n保 證 金：{margin} USDT\n預估張數：{size}\n"
        f"━━━━━━━━━━\n"
        f"進場：{pre} 根反轉前色 + {post} 根反轉後色\n"
        f"　　　且後 {post} 根振幅累加 ≥ {amp}%\n"
        f"出場：{exitn} 根反向色 + 振幅累加 ≥ {amp}%\n"
        f"進出場皆 taker 市價\n{prev}\n"
        f"━━━━━━━━━━\n⚠ 無 TP/SL/TE，僅靠反向訊號出場{warn}\n"
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
    S = {**p, "alive": True, "state": "等訊號", "chat": u.effective_chat.id,
         "hb": time.time(), "hb_warned": False}
    STRATS[k] = S
    TASKS[k] = asyncio.create_task(hloop(c.application, u.effective_chat.id, S))
    save_state()
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
    save_state()
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
        (held if p else done).append(f"{S['sym']} {S['dir']}")
    orphan = await sweep_h()
    save_state()
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

async def _reply_long(u, head, lines, tail):
    """把長清單拆成多則訊息送出（Telegram 單則上限 4096 字元）。"""
    LIM = 3500
    buf = list(head); msgs = []
    for ln in lines:
        if sum(len(x) + 1 for x in buf) + len(ln) + 1 > LIM and len(buf) > len(head):
            msgs.append("\n".join(buf)); buf = list(head)
        buf.append(ln)
    buf += tail
    msgs.append("\n".join(buf))
    for i, m in enumerate(msgs):
        if len(msgs) > 1:
            m = m.replace("事件：燈號檢視", f"事件：燈號檢視（{i+1}/{len(msgs)}）", 1)
        await reply(u, m)

def _fmt_atr(v, tick):
    """ATR 依商品 tick 決定小數位，避免大幣印一堆 0 或小幣被截斷。"""
    if v is None: return "-"
    exp = -tick.as_tuple().exponent
    q = Decimal(1).scaleb(-max(2, min(8, exp + 1)))
    return str(v.quantize(q))

async def cmd_ha(u, c):
    """燈號(HA) + ATR14/ATR ratio(原始K線)。用法：/ha ETHUSDT [根數 3~300]"""
    if not c.args:
        await reply(u, f"{E.BOT} 用法：/ha ETHUSDT 30\n"
                       f"根數範圍 3~300（預設 20）\n"
                       f"欄位：漲跌燈號(HA)｜ATR14｜ATR ratio\n"
                       f"※ ATR 以原始 K 線計算，非 HA\n"
                       f"週期依 /timeframe，目前 {ACCOUNT_TF}"); return
    sym = c.args[0].upper()
    n = 20
    if len(c.args) >= 2:
        try: n = max(3, min(300, int(c.args[1])))
        except Exception: pass
    try: spec = await get_spec(sym)
    except Exception: await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return
    kl, ha = await klines_and_ha(spec["iid"], ACCOUNT_TF, limit=min(300, max(HA_HIST, n + 20)))
    if not ha: await reply(u, f"{E.BOT} {sym} K 線取得失敗"); return
    atrs = calc_atr(kl, 14)
    st = max(0, len(ha) - n)
    head = [f"{E.BOT} OKX均K｜{ACCT}",
            f"事件：燈號檢視 {sym} {ACCOUNT_TF}",
            f"共 {len(ha) - st} 根（舊→新）｜ATR14 / ATR ratio",
            "※ ATR 取原始 K 線，燈號取 HA",
            "━" * 10]
    lines = []
    for i in range(st, len(ha)):
        x = ha[i]
        tt = datetime.fromtimestamp(int(x["ts"]) / 1000, TZ8).strftime("%m/%d %H:%M")
        lg = "🟩" if x["color"] == "G" else "🟥"
        av, rv = atrs[i]
        avs = _fmt_atr(av, spec["tick"])
        rvs = f"{rv:.4f}%" if rv is not None else "-"
        lines.append(f"{tt} {lg} {avs} | {rvs}")
    tail = ["━" * 10, f"時間：{hhmmss()}"]
    await _reply_long(u, head, lines, tail)

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
    if not c.args:
        await reply(u, f"{E.BOT} 目前週期：{ACCOUNT_TF}\n可選 3m/5m/10m/15m\n變更：/timeframe 10m"); return
    tf = c.args[0]
    if tf not in TF_SEC: await reply(u, f"{E.BOT} 週期須 3m/5m/10m/15m"); return
    ACCOUNT_TF = tf; save_state()
    await reply(u, f"{E.BOT} ✅ 週期已設為 {tf}\n（僅影響之後新建立的策略）")

async def cmd_menu(u, c):
    await reply(u, f"{E.BOT} OKX均K｜{ACCT}\n使用說明\n━━━━━━━━━━\n"
        "/run 商品 方向 槓桿 保證金 PRE POST EXIT 振幅\n"
        f"例：/run ETHUSDT L 1x 3 2 3 2 0.3\n"
        f"　週期依 /timeframe（目前 {ACCOUNT_TF}）\n"
        "/stop 商品 方向\n/stopall 停全部\n"
        "/status 策略現況\n/summary 當日戰報\n"
        "/ha 商品 根數  燈號+ATR14+ATRratio（3~300根）\n"
        "/timeframe 查看/設定週期\n/coins 幣種\n"
        "━━━━━━━━━━\n"
        "進場：PRE根反轉前色 + POST根反轉後色 + 振幅達標\n"
        "出場：EXIT根反向色 + 振幅達標\n"
        "進出場皆 taker 市價\n"
        "⚠ 無 TP/SL/TE，僅靠反向訊號出場\n"
        "✅ 重啟接管持倉\n"
        "✅ 與普K 完全獨立，不互相撤單")

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
async def _post_init(app):
    global HTTP
    HTTP = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), limits=httpx.Limits(max_connections=40))
    CMDS = [BotCommand("run", "建立均K策略"),
            BotCommand("stop", "停指定"), BotCommand("stopall", "停全部"),
            BotCommand("status", "現況"), BotCommand("summary", "當日戰報"),
            BotCommand("ha", "燈號+ATR 3~300根"), BotCommand("timeframe", "週期"),
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
    await startup_recover(app)
    hb = asyncio.create_task(hb_watch(app))
    _BG.add(hb); hb.add_done_callback(_BG.discard)
    print("心跳看門狗已啟動")

def main():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    print(f"啟動 {ACCT} 均K B6-2（token ...{TOKEN[-6:]}）")
    app = (Application.builder().token(TOKEN).post_init(_post_init)
           .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0)
           .pool_timeout(30.0).get_updates_read_timeout(40.0)
           .get_updates_connect_timeout(30.0).build())
    for cmd, fn in [(["menu", "start"], cmd_menu), ("run", cmd_run), ("confirm", cmd_confirm),
                    ("stop", cmd_stop), ("stopall", cmd_stopall), ("status", cmd_status),
                    ("summary", cmd_summary), ("ha", cmd_ha),
                    ("timeframe", cmd_timeframe), ("coins", cmd_coins)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.run_polling()

if __name__ == "__main__":
    main()

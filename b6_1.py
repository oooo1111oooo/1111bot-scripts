#!/usr/bin/env python3
"""B6-1 普K｜o3333o — 核心重寫版
規格：
  1. 每根 K 線開盤即掛限價埋伏單；未成交於收線前 3 秒撤單。
  2. 遲到一律立刻補掛，除非距離收線不足 30 秒（避免與下一根碰撞）才跳過。
  3. 一根 K 線只掛一次。進場後依 TP/SL/TE 出場，出場後等下一根開盤。
  4. OKX 為唯一真相來源：撤單、持倉、損益一律回查 OKX 確認。
  5. 重啟時接管 OKX 上的既有持倉與掛單，不留孤兒。
  6. Telegram 為旁路：發送失敗絕不影響交易流程。
"""
import sys, hmac, base64, hashlib, json, time, asyncio, uuid, os
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN
from datetime import datetime, timezone, timedelta
import httpx
from telegram import BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters
sys.path.insert(0, "/srv/1111bot")
from app.core import emoji as E
from app.strategy.normal import next_open_epoch, TF_SEC

BASE = "https://www.okx.com"
ACCT = "o3333o"
TZ8 = timezone(timedelta(hours=8))
ACCOUNT_TF = "5m"
STATE_FILE = "/srv/1111bot/data/strategies_o3333o.json"
CANCEL_LEAD = 3      # 收線前幾秒撤單
MIN_ROOM = 30        # 距收線不足幾秒就放棄本輪

def load_env(p):
    d = {}
    for line in open(p):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); d[k] = v
    return d

ACC = load_env("/srv/1111bot/config/accounts.env")
BOTS = load_env("/srv/1111bot/config/bots.env")
TOKEN = BOTS["BOT_o3333o_NORMAL"]
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
SAVE_FIELDS = ("sym","dir","tf","lev","margin","offset","tp","sl","te","chat",
               "pos_open","pos_px","pos_tp","pos_sl","pos_ee","pos_pt","last_open")

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
    return STATE_FILE.replace("strategies_", "trades_").replace(".json", "_" + str(t).replace("-", "") + ".json")

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

# ---------- OKX API（全非同步，不阻塞事件迴圈） ----------
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

def align(px, tick, d):
    return (px / tick).to_integral_value(rounding=ROUND_FLOOR if d == "L" else ROUND_CEILING) * tick

def csize(m, lev, px, cv, lot):
    return ((m * lev / px) / cv / lot).to_integral_value(rounding=ROUND_DOWN) * lot

# ---------- Telegram（旁路：永不阻塞交易） ----------
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
    """指令回覆：失敗重試一次，再失敗只記錄，不拋出。"""
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

async def okx_orders(iid=None, ps=None, prefix="n"):
    r = await api("GET", "/api/v5/trade/orders-pending")
    if r.get("code") != "0": return []
    out = []
    for o in (r.get("data") or []):
        if iid and o.get("instId") != iid: continue
        if ps and o.get("posSide") != ps: continue
        if prefix and not str(o.get("clOrdId") or "").startswith(prefix): continue
        out.append(o)
    return out

async def cancel_verified(iid, oid, tries=4):
    """撤單並回查 OKX 確認。回傳 canceled / filled / fail"""
    for i in range(tries):
        await api("POST", "/api/v5/trade/cancel-order", {"instId": iid, "ordId": oid})
        await asyncio.sleep(0.8)
        st = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={oid}")
        if st.get("code") == "0" and st.get("data"):
            s2 = st["data"][0].get("state")
            if s2 == "canceled": return "canceled"
            if s2 == "filled": return "filled"
    return "fail"

async def sweep(iid, pos, keep=None):
    """清掉本 bot 在該幣種該方向的所有殘留掛單。"""
    n = 0
    for o in await okx_orders(iid, pos):
        if keep and o.get("ordId") == keep: continue
        await api("POST", "/api/v5/trade/cancel-order", {"instId": iid, "ordId": o["ordId"]})
        n += 1
    return n

async def close_record(iid, ps, after_ms, tries=10):
    """出場後取 OKX 真實平倉紀錄。"""
    for i in range(tries):
        r = await api("GET", f"/api/v5/account/positions-history?instType=SWAP&instId={iid}&limit=10")
        if r.get("code") == "0":
            for p in (r.get("data") or []):
                if p.get("posSide") == ps and int(p.get("uTime") or 0) >= after_ms:
                    return p
        await asyncio.sleep(1)
    return None

# ---------- 出場處理（共用：正常出場與重啟接管都走這裡） ----------
async def do_exit(app, S, spec, iid, d, pos, size, fpx, tp, sl, ee, pt, reason, k):
    cs = "sell" if d == "L" else "buy"
    # 平倉張數一律以 OKX 實際持倉為準（避免重複掛單造成倉位加倍時只平掉一半）
    p_now = await okx_pos(iid, pos)
    if p_now:
        try:
            real = abs(Decimal(str(p_now.get("availPos") or p_now.get("pos") or "0")))
            if real > 0:
                if real != size:
                    print("size mismatch", S.get("sym"), d, "local", size, "okx", real)
                size = real
        except Exception:
            pass
    else:
        await notify(app, S["chat"], f"{E.BOT} {S['sym']} {E.dir_word(d)} 平倉時 OKX 已無持倉，略過")
        for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt"):
            S.pop(a, None)
        save_state()
        return True
    t0 = int(time.time() * 1000)
    xr = await api("POST", "/api/v5/trade/order",
                   {"instId": iid, "tdMode": "isolated", "side": cs, "posSide": pos,
                    "ordType": "market", "sz": str(size),
                    "clOrdId": "x" + uuid.uuid4().hex[:14]})
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
               "ambush_s": round(ee - pt) if pt else 0, "hold_s": hs,
               "gross": str(g), "fee": str(fee), "net": str(net), "nv": str(nv),
               "src": src, "ts": hhmmss(),
               "in_ts": datetime.fromtimestamp(ee, TZ8).strftime("%H:%M:%S"),
               "tf": S["tf"], "te": str(S["te"]) + "s",
               "in_px": str(fpx), "out_px": str(xpx)})
    await notify(app, S["chat"],
        f"{E.BOT} OKX普K｜{ACCT}\n事件：{'🟢' if net >= 0 else '🔴'} 已出場\n"
        f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n出場原因：{reason}\n"
        f"進場價：{fpx}\n出場價：{xpx}\n持倉秒數：{hs}s\n"
        f"毛損益：{g:+.6f} ({gp:+.3f}%)\n手續費：{fee:+.6f} ({fp:+.3f}%)\n"
        f"淨損益：{net:+.6f} ({npv:+.3f}%) {E.pnl_emoji(net)}\n時間：{hhmmss()}")
    for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt"):
        S.pop(a, None)
    save_state()
    return True

# ---------- 持倉監控 ----------
async def monitor(app, S, spec, iid, d, pos, size, fpx, tp, sl, ee, pt, k):
    chk = 0
    while S["alive"]:
        await asyncio.sleep(2)
        chk += 1
        held = time.time() - ee
        last = await get_last(iid)
        reason = None
        if d == "L":
            if last >= tp: reason = "Take_Profit"
            elif last <= sl: reason = "Stop_Loss"
        else:
            if last <= tp: reason = "Take_Profit"
            elif last >= sl: reason = "Stop_Loss"
        if not reason and held >= S["te"]: reason = "Time_Exit"
        if not reason and chk % 8 == 0:
            p_chk = await okx_pos(iid, pos)
            if p_chk:
                try:
                    rs = abs(Decimal(str(p_chk.get("pos") or "0")))
                    if rs > 0 and rs != size:
                        print("position size changed", S["sym"], d, size, "->", rs)
                        size = rs
                except Exception:
                    pass
            if not p_chk:
                await notify(app, S["chat"], f"{E.BOT} {S['sym']} {E.dir_word(d)} OKX 已無持倉（可能手動平倉），本輪結束")
                for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt"):
                    S.pop(a, None)
                save_state(); return
        if reason:
            await do_exit(app, S, spec, iid, d, pos, size, fpx, tp, sl, ee, pt, reason, k)
            return
    # 策略被停止但仍持倉
    if await okx_pos(iid, pos):
        await notify(app, S["chat"], f"{E.BOT} {S['sym']} {E.dir_word(d)} 策略停止但仍有持倉，請至 OKX 處理")

# ---------- 主迴圈：一根 K 線一輪 ----------
async def loop(app, chat, S):
    spec = S["spec"]; iid = spec["iid"]; d = S["dir"]
    pos = "long" if d == "L" else "short"
    k = skey(S["sym"], d)
    try:
        # 重啟接管：OKX 上已有持倉 -> 直接進監控
        if S.get("pos_open"):
            p = await okx_pos(iid, pos)
            if p:
                fpx = Decimal(S.get("pos_px") or p.get("avgPx") or "0")
                tp = Decimal(S["pos_tp"]); sl = Decimal(S["pos_sl"])
                ee = float(S.get("pos_ee") or time.time())
                pt0 = float(S["pos_pt"]) if S.get("pos_pt") else None
                size = abs(Decimal(p.get("pos") or "0"))
                S["state"] = "持倉中"; save_state()
                await notify(app, chat, f"{E.BOT} {E.dir_emoji(d)} {S['sym']} {E.dir_word(d)} 已接管既有持倉，恢復 TP/SL/TE 監控")
                await monitor(app, S, spec, iid, d, pos, size, fpx, tp, sl, ee, pt0, k)
            else:
                for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt"):
                    S.pop(a, None)
                save_state()

        while S["alive"]:
            tf_sec = TF_SEC[S["tf"]]
            now = time.time()
            cur = int(now // tf_sec) * tf_sec
            room = cur + tf_sec - now
            if cur != S.get("last_open") and room >= MIN_ROOM:
                oe = cur                          # 本根尚未掛過且還有時間 -> 立刻掛
            else:
                S["state"] = "等下輪"; save_state()
                oe = next_open_epoch(int(time.time()), S["tf"])
                w = oe - time.time()
                if w > 0: await asyncio.sleep(w)
                if not S["alive"]: break
            S["last_open"] = oe; save_state()

            # 開盤取價 -> 埋伏價 -> 張數
            op = await get_last(iid)
            amb = align(op * (1 - S["offset"] / 100) if d == "L" else op * (1 + S["offset"] / 100), spec["tick"], d)
            size = csize(S["margin"], Decimal(S["lev"]), amb, spec["ctval"], spec["lot"])
            if size < spec["minsz"]:
                await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 保證金不足，循環停止")
                break

            await sweep(iid, pos)                 # 掛新單前先清乾淨
            r = await api("POST", "/api/v5/trade/order",
                          {"instId": iid, "tdMode": "isolated", "side": "buy" if d == "L" else "sell",
                           "posSide": pos, "ordType": "limit", "px": str(amb), "sz": str(size),
                           "clOrdId": "n" + uuid.uuid4().hex[:14]})
            if r.get("code") != "0":
                em = (r.get("data") or [{}])[0].get("sMsg") or r.get("msg")
                await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 掛單失敗：{em}")
                await asyncio.sleep(3); continue
            oid = r["data"][0]["ordId"]
            S["state"] = "委託中"; bump(k, "placed")
            pt = time.time()
            # 掛單後確認同幣同向只剩這一張，撤掉任何重複單
            dup = await sweep(iid, pos, keep=oid)
            if dup:
                print("dup orders cleared", S["sym"], d, dup)
                await notify(app, chat, f"{E.BOT} {S['sym']} {E.dir_word(d)} 已清除殘留掛單 {dup} 筆")

            # 輪詢成交，直到收線前 CANCEL_LEAD 秒
            deadline = oe + tf_sec - CANCEL_LEAD
            filled = False; fpx = None
            while S["alive"] and time.time() < deadline:
                await asyncio.sleep(2)
                st = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={oid}")
                if st.get("code") == "0" and st.get("data"):
                    s2 = st["data"][0]["state"]
                    if s2 == "filled":
                        filled = True; fpx = Decimal(st["data"][0]["avgPx"]); break
                    if s2 == "canceled":
                        break

            if not filled:
                rc = await cancel_verified(iid, oid)
                if rc == "filled":
                    st = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={oid}")
                    try: fpx = Decimal(st["data"][0]["avgPx"]); filled = True
                    except Exception: pass
                elif rc != "canceled":
                    await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 撤單未確認，本策略停止以免重複掛單")
                    S["alive"] = False; break
            if not S["alive"]: break
            if not filled: continue

            # 成交 -> 算 TP/SL -> 監控
            bump(k, "entered")
            if d == "L":
                tp = align(fpx * (1 + S["tp"] / 100), spec["tick"], "S")
                sl = align(fpx * (1 - S["sl"] / 100), spec["tick"], "L")
            else:
                tp = align(fpx * (1 - S["tp"] / 100), spec["tick"], "L")
                sl = align(fpx * (1 + S["sl"] / 100), spec["tick"], "S")
            ee = time.time()
            S["state"] = "持倉中"
            S["pos_open"] = True; S["pos_px"] = str(fpx)
            S["pos_tp"] = str(tp); S["pos_sl"] = str(sl); S["pos_ee"] = ee; S["pos_pt"] = pt
            save_state()
            await notify(app, chat,
                f"{E.BOT} OKX普K｜{ACCT}\n事件：🔔 已進場成交\n"
                f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                f"進場價格：{fpx} ({pct(S['offset'])}%)\n"
                f"止盈 TP：{tp} ({pct(S['tp'])}%)\n止損 SL：{sl} ({pct(S['sl'])}%)\n"
                f"持倉 TE：{S['te']}s\n埋伏秒數：{int(ee - pt)}s\n"
                f"狀　　態：📌 持倉中\n時間：{hhmmss()}")
            await monitor(app, S, spec, iid, d, pos, size, fpx, tp, sl, ee, pt, k)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("loop error", S.get("sym"), S.get("dir"), type(e).__name__, e)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 循環錯誤：{type(e).__name__}: {e}")
    finally:
        S["state"] = "已停止"; S["alive"] = False
        STRATS.pop(k, None); TASKS.pop(k, None); save_state()

# ---------- 啟動接管 ----------
async def rebuild_strat(d):
    spec = await get_spec(d["sym"])
    S = {"sym": d["sym"], "dir": d["dir"], "tf": d.get("tf", ACCOUNT_TF),
         "lev": int(d["lev"]), "margin": Decimal(str(d["margin"])),
         "offset": Decimal(str(d["offset"])), "tp": Decimal(str(d["tp"])),
         "sl": Decimal(str(d["sl"])), "te": int(d["te"]), "spec": spec,
         "alive": True, "state": "等下輪", "chat": d.get("chat", CHAT_ID)}
    for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt", "last_open"):
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
            TASKS[k] = asyncio.create_task(loop(app, S["chat"], S))
            rec.append(f"{E.dir_emoji(S['dir'])} {S['sym']} {E.dir_word(S['dir'])}")
        except Exception as e:
            print("重建失敗", d, e)
    pend = await api("GET", "/api/v5/trade/orders-pending")
    posr = await api("GET", "/api/v5/account/positions")
    n_ord = len(pend.get("data", [])) if pend.get("code") == "0" else 0
    n_pos = len([p for p in posr.get("data", []) if float(p.get("pos", "0")) != 0]) if posr.get("code") == "0" else 0
    print(f"已接管策略 {len(rec)}｜OKX 掛單{n_ord} 持倉{n_pos}")
    if CHAT_ID and rec:
        await notify(app, CHAT_ID,
            f"{E.BOT} OKX普K｜{ACCT}\n事件：🔄 重啟認領完成\n━━━━━━━━━━\n"
            f"已接管策略（{len(rec)}）：\n" + "\n".join("・" + x for x in rec) +
            f"\nOKX 現況：掛單{n_ord} 持倉{n_pos}\n循環已接管，繼續運作\n時間：{hhmmss()}")

# ---------- TG 指令 ----------
async def cmd_run(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args
    fmt = f"用法：/run 商品 方向 槓桿 保證金 埋伏 TP SL TE\n例：/run ETHUSDT L 1x 3 0.5 0.5 0.5 180\n（週期依 /timeframe，目前 {ACCOUNT_TF}）"
    if len(a) != 8: await reply(u, f"{E.BOT} 參數數量錯誤（需8個）\n{fmt}"); return
    try:
        sym = a[0].upper(); dr = a[1].upper(); lev = int(a[2].replace("x", ""))
        margin = Decimal(a[3]); offset = Decimal(a[4]); tp = Decimal(a[5])
        sl = Decimal(a[6]); te = int(a[7])
    except Exception:
        await reply(u, f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if dr not in ("L", "S"): await reply(u, f"{E.BOT} 方向須 L 或 S"); return
    if not 1 <= te <= 900: await reply(u, f"{E.BOT} TE 須 1~900 秒"); return
    k = skey(sym, dr)
    if k in STRATS and STRATS[k].get("alive"):
        await reply(u, f"{E.BOT} {sym} {E.dir_word(dr)} 已在運行"); return
    try: spec = await get_spec(sym)
    except Exception: await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return
    op = await get_last(spec["iid"])
    amb = align(op * (1 - offset / 100) if dr == "L" else op * (1 + offset / 100), spec["tick"], dr)
    size = csize(margin, Decimal(lev), amb, spec["ctval"], spec["lot"])
    if size < spec["minsz"]:
        need = spec["minsz"] * spec["ctval"] * op / Decimal(lev)
        await reply(u, f"{E.BOT} {E.LOSS} 保證金不足：算出 {size} 張 < 最小 {spec['minsz']}\n此槓桿下至少需約 {need:.4f} USDT"); return
    PENDING[u.effective_chat.id] = {"t": time.time(), "sym": sym, "dir": dr, "tf": ACCOUNT_TF,
        "lev": lev, "margin": margin, "offset": offset, "tp": tp, "sl": sl, "te": te, "spec": spec}
    await reply(u, f"{E.BOT} OKX普K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
        f"商　　品：{E.dir_emoji(dr)} {sym} {E.dir_word(dr)} {lev}x\n週　　期：{ACCOUNT_TF}\n"
        f"開盤估價：{op}\n埋伏距離：{offset}%\n埋伏價格：{amb}\n"
        f"止盈 TP：{tp}%\n止損 SL：{sl}%\n持倉 TE：{te}s\n保 證 金：{margin} USDT\n下單張數：{size}\n"
        f"━━━━━━━━━━\n⚠ 確認後真實循環交易\n下一步：60秒內 /confirm\n時間：{hhmmss()}")
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
    S = {**p, "alive": True, "state": "等下輪", "chat": u.effective_chat.id}
    STRATS[k] = S
    TASKS[k] = asyncio.create_task(loop(c.application, u.effective_chat.id, S))
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
    tg = [skey(sym, a[1].upper())] if len(a) >= 2 and skey(sym, a[1].upper()) in alive else [k for k in alive if STRATS[k]["sym"] == sym]
    if not tg: await reply(u, f"{E.BOT} 找不到運行中的 {sym}"); return
    if len(tg) > 1: await reply(u, f"{E.BOT} {sym} 有多方向，請指定 /stop {sym} L 或 S"); return
    S = STRATS[tg[0]]; d = S["dir"]; iid = S["spec"]["iid"]
    ps = "long" if d == "L" else "short"
    p = await okx_pos(iid, ps)
    S["alive"] = False
    n = await sweep(iid, ps)
    save_state()
    if p:
        await reply(u, f"{E.BOT} /stop（持倉中）\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                       f"⚠ 已進場不自動平倉\n倉位 {p['pos']} 張 均價 {p.get('avgPx','?')} 浮 {p.get('upl','?')}\n"
                       f"已撤掛單 {n} 筆\n請至 OKX 手動平倉")
    else:
        await reply(u, f"{E.BOT} /stop\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n已停止，撤掉掛單 {n} 筆")

async def cmd_stopall(u, c):
    alive = [k for k, s in STRATS.items() if s.get("alive")]
    held = []; done = []
    for k in list(alive):
        S = STRATS[k]; d = S["dir"]; iid = S["spec"]["iid"]
        ps = "long" if d == "L" else "short"
        p = await okx_pos(iid, ps)
        S["alive"] = False
        await sweep(iid, ps)
        (held if p else done).append(f"{S['sym']} {S['dir']}")
    orphan = 0
    for o in await okx_orders(prefix="n"):
        cr = await api("POST", "/api/v5/trade/cancel-order", {"instId": o["instId"], "ordId": o["ordId"]})
        if cr.get("code") == "0": orphan += 1
    save_state()
    m = f"{E.BOT} /stopall\n━━━━━━━━━━\n"
    if done: m += f"已停止策略（{len(done)}）：\n" + "\n".join("・" + x for x in done) + "\n"
    if orphan: m += f"另清除殘留掛單：{orphan} 筆\n"
    if held: m += f"⚠ 持倉需手動平倉（{len(held)}）：\n" + "\n".join("・" + x for x in held) + "\n"
    if not done and not orphan and not held: m += "目前無策略、無殘單\n"
    await reply(u, m + f"時間：{hhmmss()}")

async def cmd_status(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    posr = await api("GET", "/api/v5/account/positions")
    pe = await api("GET", "/api/v5/trade/orders-pending")
    bal = await api("GET", "/api/v5/account/balance")
    eq = av = "?"
    if bal.get("code") == "0":
        x = next((d for d in bal["data"][0].get("details", []) if d["ccy"] == "USDT"), None)
        if x:
            eq = f"{Decimal(x.get('eq','0')):.4f}"
            av = f"{Decimal(x.get('availEq') or x.get('availBal') or '0'):.4f}"
    pl = [p for p in posr.get("data", []) if float(p.get("pos", "0")) != 0] if posr.get("code") == "0" else []
    pdl = pe.get("data", []) if pe.get("code") == "0" else []
    alive = [s for s in STRATS.values() if s.get("alive")]
    okxp = {(p["instId"], p["posSide"]) for p in pl}
    okxo = {(o["instId"], o.get("posSide")) for o in pdl}
    L = [f"{E.BOT} OKX普K｜{ACCT}", "事件：現況（即時查 OKX）", "━━━━━━━━━━",
         f"USDT權益：{eq}", f"可用餘額：{av}", f"帳戶週期：{ACCOUNT_TF}",
         f"運行中策略：{len(alive)} 個"]
    for s in alive:
        k = skey(s["sym"], s["dir"]); placed, entered = get_stat(k)
        key = (s["spec"]["iid"], "long" if s["dir"] == "L" else "short")
        live = "持倉中" if key in okxp else ("委託中" if key in okxo else "等下輪")
        L.append(f"　{E.dir_emoji(s['dir'])} {s['sym']} {E.dir_word(s['dir'])}：{live}(掛{placed}/進{entered})")
    L.append(f"掛單數：{len(pdl)}")
    L.append(f"持倉數：{len(pl)}")
    for p in pl:
        d = "L" if p["posSide"] == "long" else "S"
        L.append(f"{E.dir_emoji(d)} {p['instId'].replace('-USDT-SWAP','USDT')} {d}")
    L += ["━━━━━━━━━━", f"時間：{hhmmss()} UTC+8"]
    await reply(u, "\n".join(L))

# ---------- /summary ----------
def sum_lines(rs, placed, entered):
    L = []
    m = len(rs)
    hit = (entered / placed * 100) if placed else 0
    L.append("委託次數：%d" % placed)
    L.append("進場數：%d | 命中率：%.2f%%" % (entered, hit))
    if m:
        L.append("平均埋伏秒數：%d秒" % (sum(int(r.get("ambush_s") or 0) for r in rs) / m))
    else:
        L.append("平均埋伏秒數：-")
    NAME = {"Take_Profit": "TP", "Stop_Loss": "SL", "Time_Exit": "TE"}
    for lab, cats in (("獲利", ("Take_Profit", "Time_Exit")), ("虧損", ("Stop_Loss", "Time_Exit"))):
        if lab == "獲利":
            sub = [r for r in rs if Decimal(str(r.get("net") or "0")) > 0]
        else:
            sub = [r for r in rs if Decimal(str(r.get("net") or "0")) < 0]
        L.append("━" * 10)
        L.append("%s數：%d" % (lab, len(sub)))
        ps = []; ss = []
        for cn in cats:
            gg = [r for r in sub if r.get("reason") == cn]
            ps.append("%s:%d" % (NAME[cn], len(gg)))
            if gg:
                ss.append("%s:%d秒" % (NAME[cn], sum(int(r.get("hold_s") or 0) for r in gg) / len(gg)))
            else:
                ss.append("%s:-" % NAME[cn])
        L.append("　" + " | ".join(ps))
        L.append("　平均秒數 " + " | ".join(ss))
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

def strat_params(sym, dr):
    S = STRATS.get(skey(sym, dr))
    if not S or not S.get("alive"):
        return f"{dr}（已停止）"
    return (f"{dr} {S['lev']}x {pct(S['margin'])} {pct(S['offset'])} "
            f"{pct(S['tp'])} {pct(S['sl'])} {S['te']}")

async def cmd_summary(u, c):
    t = today8(); recs = load_trades(t)
    ts = {k: v for k, v in STATS.items() if str(v.get("date")) == str(t)}
    L = [f"{E.BOT} OKX普K｜{ACCT}", f"📊📊📊 Summary {t}"]
    for dr in ("L", "S"):
        rows = [r for r in recs if r["dir"] == dr]
        pa = sum(v["placed"] for k, v in ts.items() if k.endswith("_" + dr))
        en = sum(v["entered"] for k, v in ts.items() if k.endswith("_" + dr))
        L.append("━" * 10)
        L.append(f"{E.dir_emoji(dr)} {E.dir_word(dr)}")
        L += sum_lines(rows, pa, en)
    L += ["━" * 10, f"時間：{hhmmss()}"]
    await reply(u, "\n".join(L))
    for sy in sorted({r["sym"] for r in recs}):
        D = [f"\U0001f49a\U0001f499\U0001fa75\U0001f49c {sy} {t}"]
        for dr in ("L", "S"):
            rows = [r for r in recs if r["sym"] == sy and r["dir"] == dr]
            st_ = ts.get(skey(sy, dr)) or {"placed": 0, "entered": 0}
            D.append("━" * 10)
            D.append(f"策略：{E.dir_emoji(dr)} {strat_params(sy, dr)}")
            D += sum_lines(rows, st_["placed"], st_["entered"])
        D += ["━" * 10, f"時間：{hhmmss()}"]
        await reply(u, "\n".join(D))

async def cmd_coins(u, c):
    on = [s["symbol"] for s in SYMS if s["enabled"]]
    L = [f"{E.BOT} OKX普K｜{ACCT}", "事件：幣種清單（即時）", "━━━━━━━━━━"]
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
    await reply(u, f"{E.BOT} ✅ 帳戶週期已設為 {tf}\n（僅影響之後新建立的策略）")

async def cmd_menu(u, c):
    await reply(u, f"{E.BOT} OKX普K｜{ACCT}\n使用說明\n━━━━━━━━━━\n"
        "/run 商品 方向 槓桿 保證金 埋伏 TP SL TE\n"
        f"例：/run ETHUSDT L 1x 3 0.5 0.5 0.5 180\n　週期依 /timeframe（目前 {ACCOUNT_TF}）\n"
        "/confirm 確認啟動\n/stop 商品 方向\n/stopall 停全部+清殘單\n"
        "/status 所有策略現況\n/summary 當日戰報\n/timeframe 查看/設定週期\n/coins 幣種\n"
        "━━━━━━━━━━\n⚠ 真實下單，循環交易\n✅ 重啟接管持倉與掛單")

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
    await app.bot.delete_my_commands()
    await app.bot.set_my_commands([
        BotCommand("run", "建立策略"), BotCommand("confirm", "確認啟動"),
        BotCommand("stop", "停指定"), BotCommand("stopall", "停全部"),
        BotCommand("status", "現況"), BotCommand("summary", "當日戰報"),
        BotCommand("timeframe", "週期"), BotCommand("coins", "幣種"),
        BotCommand("menu", "說明")])
    print("左下 Menu 已更新")
    try:
        jq = app.job_queue
        if jq:
            t2359 = datetime.strptime("23:59", "%H:%M").time().replace(tzinfo=TZ8)
            jq.run_daily(job_summary, time=t2359, name="daily_summary")
            print("已排程：每日 23:59 自動 /summary")
    except Exception as e:
        print("schedule fail", e)
    await startup_recover(app)

def main():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    print(f"啟動 o3333o 普K B6-1 核心重寫版（token ...{TOKEN[-6:]}）")
    app = (Application.builder().token(TOKEN).post_init(_post_init)
           .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0)
           .pool_timeout(30.0).get_updates_read_timeout(40.0)
           .get_updates_connect_timeout(30.0).build())
    for cmd, fn in [(["menu", "start"], cmd_menu), ("run", cmd_run), ("confirm", cmd_confirm),
                    ("stop", cmd_stop), ("stopall", cmd_stopall), ("status", cmd_status),
                    ("summary", cmd_summary), ("timeframe", cmd_timeframe), ("coins", cmd_coins)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.run_polling()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""B6-1 原K｜多帳戶 — 核心重寫版
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
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, MessageHandler, filters
sys.path.insert(0, "/srv/1111bot")
from app.core import emoji as E
from app.strategy.normal import next_open_epoch as _noe_unused, TF_SEC as _TFS_unused

# 原K 專用時間框架（皆整除 60 分鐘，起訖時刻自然對齊整點）
TF_SEC = {"3m": 180, "4m": 240, "5m": 300, "6m": 360, "10m": 600,
          "12m": 720, "15m": 900, "20m": 1200, "30m": 1800, "60m": 3600}

def next_open_epoch(now_epoch, tf):
    sec = TF_SEC[tf]
    return ((now_epoch // sec) + 1) * sec

BASE = "https://www.okx.com"
ACCT = os.environ.get("ACCT", "o3333o")  # 由 systemd 注入
TZ8 = timezone(timedelta(hours=8))
ACCOUNT_TF = "5m"
STATE_FILE = f"/srv/1111bot/data/strategies_{ACCT}.json"
ENTRY_CUTOFF = 60    # TF 剩餘不足幾秒就放棄進場（撤掉未成交單、也不補掛）
MOVE_TICK = 1.0      # frame_mover 心跳（秒）
FORCE_MV_INTERVAL = 60  # 每 N 秒固定推格一次（定時止損調整）
FEE_RATE = Decimal("0.001")  # 手續費率 0.1%：獲利超過此值時 SL 緊貼現價（距離=FEE_RATE）

def load_env(p):
    d = {}
    for line in open(p):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); d[k] = v
    return d

ACC = load_env("/srv/1111bot/config/accounts.env")
BOTS = load_env("/srv/1111bot/config/bots.env")
TOKEN = BOTS[f"BOT_{ACCT}_NORMAL"]
SYMS = json.load(open("/srv/1111bot/config/symbols.json"))["symbols"]

PENDING = {}; STRATS = {}; TASKS = {}; STATS = {}
CHAT_ID = None
SHUTTING_DOWN = False
HTTP = None
SPEC_CACHE = {}

def skey(s, d): return f"{s}_{d}"
def inst_id(s): return s.replace("USDT", "") + "-USDT-SWAP"
def now8(): return datetime.now(TZ8)
def hhmmss(): return now8().strftime("%H:%M:%S")
def today8(): return now8().strftime("%Y-%m-%d")
def pct(v):
    """去尾零但不用科學記號：10 -> "10"（非 "1E+1"），0.50 -> "0.5"。"""
    d = Decimal(str(v)).normalize()
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return str(d)

# ---------- 狀態持久化（原子寫入） ----------
SAVE_FIELDS = ("sym","dir","tf","lev","margin","offset","tp","sl","move_pct","interval","chat",
               "pos_open","pos_px","pos_tp","pos_sl","pos_ee","pos_pt","last_open","catchup",
               "algo_id","tp_px","sl_px","frame_base","move_n","move_hist","last_move",
               "last_force_mv_t","martin","martin_orders","martin_paused")

def save_state(_open=open, _replace=os.replace, _fsync=os.fsync, _dump=json.dump):
    # 關閉流程中絕不寫檔：此時 loop() 的 finally 會逐一 pop 掉 STRATS，
    # 若照常寫入就會把存檔覆蓋成空的，導致重啟後策略全滅。
    if SHUTTING_DOWN:
        return
    try:
        data = {"chat": CHAT_ID, "tf": ACCOUNT_TF, "stats": STATS, "strats": []}
        for k, S in STRATS.items():
            if S.get("alive"):
                data["strats"].append({a: S[a] for a in SAVE_FIELDS if a in S})
        def enc(o): return str(o) if isinstance(o, Decimal) else o
        tmp = STATE_FILE + ".tmp"
        with _open(tmp, "w") as f:
            _dump(data, f, default=enc); f.flush(); _fsync(f.fileno())
        _replace(tmp, STATE_FILE)
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

# ---------- OKX algo 單（OCO 止盈止損） ----------
async def place_algo(iid, pos, d, size, tp, sl):
    """掛 OCO algo 單，回傳 algoId 或 None。"""
    cs = "sell" if d == "L" else "buy"
    r = await api("POST", "/api/v5/trade/order-algo",
                  {"instId": iid, "tdMode": "isolated", "side": cs, "posSide": pos,
                   "ordType": "oco", "sz": str(size),
                   "tpTriggerPx": str(tp), "tpOrdPx": "-1",
                   "slTriggerPx": str(sl), "slOrdPx": "-1",
                   "clOrdId": "y" + uuid.uuid4().hex[:14]})
    if r.get("code") == "0" and r.get("data"):
        return r["data"][0].get("algoId")
    print("place_algo fail", iid, d, r.get("msg"))
    return None

async def cancel_frame(iid, algo_id):
    """撤銷 OCO algo 單。"""
    if not algo_id:
        return
    await api("POST", "/api/v5/trade/cancel-algos",
              [{"instId": iid, "algoId": algo_id}])

async def amend_frames(items):
    """批次修改 algo 單止損價。items: [(instId, algoId, tp, sl)]
    OKX 一次最多 10 筆，超過自動分批。回傳成功筆數。"""
    ok = 0
    for i in range(0, len(items), 10):
        batch = items[i:i+10]
        body = [{"instId": a, "algoId": b,
                 "newTpTriggerPx": str(c), "newSlTriggerPx": str(d2)}
                for a, b, c, d2 in batch]
        r = await api("POST", "/api/v5/trade/amend-algos", body)
        if r.get("code") == "0":
            ok += len(batch)
        else:
            print("amend_frames fail", r.get("msg"))
    return ok

def sl_shift(S, px, d):
    """依現價追蹤 SL；TP 永遠不動。
    方案D：不管獲利與否，一律緊貼現價 0.1%（FEE_RATE）。
    SL 只能往有利方向移，不能後退。
    收盤推格由 frame_mover 另行處理，此函式只處理現價追蹤。"""
    try:
        fpx = Decimal(str(S.get("pos_px") or "0"))
        cur = Decimal(str(px))
        tp = Decimal(str(S["tp_px"]))
        sl = Decimal(str(S["sl_px"]))
    except Exception:
        return None
    if fpx <= 0:
        return None
    tick = S["spec"]["tick"]

    # 方案D：一律緊貼現價，距離固定 FEE_RATE（0.1%）
    if d == "L":
        nsl = align(cur * (Decimal("1") - FEE_RATE), tick, "S")
        if nsl <= sl:
            return None  # SL 不能後退
    else:
        nsl = align(cur * (Decimal("1") + FEE_RATE), tick, "L")
        if nsl >= sl:
            return None  # SL 不能後退

    gain = float((cur - fpx) / fpx * 100) if d == "L" else float((fpx - cur) / fpx * 100)
    return tp, nsl, cur, gain

async def close_bookkeeping(app, S, reason):
    """偵測到倉位已不在（止盈或止損觸發）後的收尾：取真實損益、撤殘單、發通知。"""
    spec = S["spec"]; iid = spec["iid"]; d = S["dir"]
    pos = "long" if d == "L" else "short"
    try:
        fpx = Decimal(str(S.get("pos_px") or "0"))
    except Exception:
        fpx = Decimal(0)
    ee = float(S.get("pos_ee") or time.time())
    size = Decimal(str(S.get("pos_sz") or "0"))
    t0 = int(ee * 1000)
    try:
        await cancel_frame(iid, S.get("algo_id"))
    except Exception as e:
        print("cancel_frame fail", e)
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
    pt = float(S.get("pos_pt") or ee)
    tp_px = Decimal(str(S.get("tp_px") or S.get("pos_tp") or "0"))
    sl_px = Decimal(str(S.get("sl_px") or S.get("pos_sl") or "0"))
    mn = S.get("move_n", 0)
    log_trade({"date": today8(), "sym": S["sym"], "dir": d, "reason": reason,
               "ambush_s": round(ee - pt) if pt else 0, "hold_s": hs,
               "gross": str(g), "fee": str(fee), "net": str(net), "nv": str(nv),
               "src": src, "ts": hhmmss(),
               "in_ts": datetime.fromtimestamp(ee, TZ8).strftime("%H:%M:%S"),
               "tf": S["tf"], "margin": str(S["margin"]),
               "move_pct": str(S.get("move_pct", 0)), "interval": S.get("interval", 0),
               "move_n": mn, "tp_px": str(tp_px), "sl_px": str(sl_px),
               "in_px": str(fpx), "out_px": str(xpx)})
    ico = "🟢" if net >= 0 else "🔴"
    mhist = S.get("move_hist") or []
    martin = int(S.get("martin") or 1)
    martin_orders = S.get("martin_orders") or []
    # 判斷是第幾單出場
    order_label = ""
    if martin >= 2 and martin_orders:
        filled_idx = next((i for i, p in enumerate(martin_orders) if p.get("filled")), None)
        if filled_idx is not None:
            order_label = f"第{filled_idx+1}單 "
    # 判斷後續連動說明
    next_note = ""
    if martin >= 2 and martin_orders:
        filled_count = sum(1 for p in martin_orders if p.get("filled"))
        total = len(martin_orders)
        if filled_count < total:
            next_note = f"\n第{filled_count+1}單連動進場中"
        else:
            next_note = "\n全部出場，等下輪TF"
    # 組 SL 移動明細行
    def _sl_lines(mhist, mn):
        lines = []
        if mn > 0 and mhist:
            lines.append(f"━━━━━━━━━━\nSL移動 {mn} 次")
            for mrec in mhist:
                tp_label = "收" if mrec.get("type") == "定時" else "現"
                lines.append(f"{mrec.get('t','')} | {tp_label} | {mrec.get('px','')} | 止{mrec.get('sl','')}")
        return lines
    sl_detail = _sl_lines(mhist, mn)
    sl_block = ("\n" + "\n".join(sl_detail)) if sl_detail else ""
    # 出場原因對應顯示
    reason_label = {"Take_Profit": "TP", "Stop_Loss": "SL", "Frame_Exit": "SL"}.get(reason, reason)
    # 出場主通知（含 SL 移動明細）
    await notify(app, S["chat"],
        f"{E.BOT} OKX原K｜{ACCT}\n事件：{ico} {order_label}已出場\n"
        f"━━━━━━━━━━\n"
        f"商品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n出場原因：{reason_label}\n"
        f"━━━━━━━━━━\n"
        f"進場：{fpx}({pct(S['offset'])}%) | {datetime.fromtimestamp(ee, TZ8).strftime('%H:%M:%S')}\n"
        f"止盈TP：{tp_px}({pct(S['tp'])}%)\n止損SL：{sl_px}({pct(S['sl'])}%)\n"
        f"出場：{xpx}({gp:+.3f}%) | {hhmmss()}\n"
        f"━━━━━━━━━━\n毛損益：{g:+.6f} ({gp:+.3f}%)\n手續費：{fee:+.6f} ({fp:+.3f}%)\n"
        f"淨損益：{net:+.6f} ({npv:+.3f}%) {E.pnl_emoji(net)}"
        f"{sl_block}{next_note}\n時間：{hhmmss()}")
    # SL 移動超過20筆才分頁補發（主通知已含第一批）
    if mn > 20 and mhist:
        PAGE = 20
        pages = [mhist[i:i+PAGE] for i in range(20, len(mhist), PAGE)]
        total_pages = len(pages) + 1
        for pi, page in enumerate(pages, 2):
            lines = [f"SL移動 {mn} 次（{pi}/{total_pages}）"]
            for mrec in page:
                tp_label = "收" if mrec.get("type") == "定時" else "現"
                lines.append(f"{mrec.get('t','')} | {tp_label} | {mrec.get('px','')} | 止{mrec.get('sl','')}")
            await notify(app, S["chat"], "\n".join(lines))
    for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt", "pos_sz",
              "algo_id", "tp_px", "sl_px", "frame_base", "move_n", "move_hist", "last_move",
              "last_force_mv_t", "closing"):
        S.pop(a, None)
    S["state"] = "等下輪"
    save_state()

async def frame_mover(app):
    """每秒對齊整秒；到了各策略的間隔就查一次全部持倉，
    需要移動 SL 的批次送出 amend-algos。同時偵測倉位消失（止盈/止損已觸發）。"""
    await asyncio.sleep(5)
    print("原K止損移動任務已啟動")
    while True:
        try:
            await asyncio.sleep(MOVE_TICK - (time.time() % MOVE_TICK))
            now = int(time.time())
            held = [S for S in list(STRATS.values())
                    if S.get("alive") and S.get("pos_open") and S.get("algo_id")]
            if not held:
                continue
            due = [S for S in held if now % max(1, int(S.get("interval", 5))) == 0]
            if not due:
                continue
            r = await api("GET", "/api/v5/account/positions")
            if r.get("code") != "0":
                continue
            live = {}
            for p in (r.get("data") or []):
                try:
                    if float(p.get("pos") or 0) == 0:
                        continue
                except Exception:
                    continue
                sy = p["instId"].replace("-USDT-SWAP", "USDT")
                live[(sy, "L" if p["posSide"] == "long" else "S")] = p
            amends = []
            for S in due:
                key = (S["sym"], S["dir"])
                p = live.get(key)
                if p is None:
                    # 倉位不在了 -> 止盈或止損已觸發
                    if S.get("closing"):
                        continue  # 已有另一處在處理，跳過
                    S["closing"] = True
                    try:
                        await close_bookkeeping(app, S, "Frame_Exit")
                    except Exception as e:
                        print("close_bookkeeping fail", S["sym"], type(e).__name__, e)
                    continue
                px = p.get("last") or p.get("markPx")
                if not px or not S.get("algo_id"):
                    continue

                # --- 定時推格（每 FORCE_MV_INTERVAL 秒固定推一格，與現價追蹤互斥）---
                force_mv = None
                now_t = time.time()
                last_force = S.get("last_force_mv_t", 0)
                if now_t - last_force >= FORCE_MV_INTERVAL:
                    S["last_force_mv_t"] = now_t
                    try:
                        sl_f = Decimal(str(S["sl_px"]))
                        tick = S["spec"]["tick"]
                        move_min = Decimal(str(S["move_pct"])) / 100
                        d_f = S["dir"]
                        if d_f == "L":
                            sl_f = align(sl_f * (Decimal("1") + move_min), tick, "S")
                        else:
                            sl_f = align(sl_f * (Decimal("1") - move_min), tick, "L")
                        force_mv = (Decimal(str(S["tp_px"])), sl_f)
                    except Exception as e:
                        print("force_shift calc fail", S["sym"], e)

                # --- 現價追蹤（定時推格那輪跳過，兩套互不干擾）---
                mv = None if force_mv else sl_shift(S, px, S["dir"])

                if force_mv:
                    ntp, nsl = force_mv
                    nbase = S.get("frame_base") or S.get("pos_px")
                    gain = float(Decimal(str(S["move_pct"])))
                    mtype = "定時"
                elif mv:
                    ntp, nsl, nbase, gain = mv
                    mtype = "現價"
                else:
                    continue

                S["_pending"] = (str(ntp), str(nsl), str(nbase), gain, mtype, str(px))
                amends.append((S["spec"]["iid"], S["algo_id"], ntp, nsl, S))

            if amends:
                items = [(a, b, c, d2) for a, b, c, d2, _ in amends]
                okn = await amend_frames(items)
                for a, b, c, d2, S in amends:
                    if okn:
                        ntp, nsl, nbase, gain, mtype, _mpx = S.pop("_pending")
                        S["tp_px"] = ntp; S["sl_px"] = nsl
                        S["frame_base"] = nbase
                        S["move_n"] = int(S.get("move_n", 0)) + 1
                        mh = S.get("move_hist")
                        if not isinstance(mh, list):
                            mh = []; S["move_hist"] = mh
                        mh.append({"t": hhmmss(), "type": mtype,
                                   "px": str(nbase), "sl": str(nsl)})
                        if len(mh) > 50:
                            S["move_hist"] = mh[-50:]
                        save_state()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("frame_mover error", type(e).__name__, e)
            await asyncio.sleep(5)

# ---------- 出場處理（共用：正常出場與重啟接管都走這裡） ----------
# ---------- 持倉監控（TP/SL 由 OKX algo 單負責；此處只防手動平倉） ----------
async def monitor(app, S, spec, iid, d, pos, size, fpx, tp, sl, ee, pt, k):
    """TP/SL 觸發由 frame_mover 偵測倉位消失後的 close_bookkeeping 處理。
    monitor 只負責每 16 秒確認倉位仍存在（防手動平倉孤兒）。"""
    chk = 0
    while S["alive"]:
        await asyncio.sleep(1)
        chk += 1
        if chk % 16 == 0:
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
                if S.get("closing"):
                    return  # 已有另一處在處理，跳過
                S["closing"] = True
                await close_bookkeeping(app, S, "TP/SL")
                return
    # 策略被停止但仍持倉
    if await okx_pos(iid, pos):
        await notify(app, S["chat"], f"{E.BOT} {S['sym']} {E.dir_word(d)} 策略停止但仍有持倉，請至 OKX 處理")

MARTIN_CHECK_INTERVAL = 30  # 馬丁模式：每 30 秒輪詢 OKX 確認是否全數出場

async def _martin_place_order(iid, pos, d, amb, size_margin, lev, spec, prefix="n"):
    """掛一張限價單，回傳 ordId 或 None。"""
    sz = csize(size_margin, Decimal(str(lev)), amb, spec["ctval"], spec["lot"])
    if sz < spec["minsz"]:
        return None, sz
    r = await api("POST", "/api/v5/trade/order",
                  {"instId": iid, "tdMode": "isolated",
                   "side": "buy" if d == "L" else "sell",
                   "posSide": pos, "ordType": "limit", "px": str(amb), "sz": str(sz),
                   "clOrdId": prefix + uuid.uuid4().hex[:14]})
    if r.get("code") == "0" and r.get("data"):
        return r["data"][0]["ordId"], sz
    return None, sz

async def _martin_all_clear(iid, pos):
    """回傳 True 代表 OKX 上該幣種該方向：無持倉 且 無本 bot 掛單。"""
    p = await okx_pos(iid, pos)
    if p:
        return False
    orders = await okx_orders(iid, pos, prefix="n")
    return len(orders) == 0

async def _martin_cancel_pending(iid, pos, placed_oids):
    """撤銷所有尚未成交的馬丁掛單。"""
    for oid in placed_oids:
        st = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={oid}")
        if st.get("code") == "0" and st.get("data"):
            state = st["data"][0].get("state", "")
            if state in ("live", "partially_filled"):
                await api("POST", "/api/v5/trade/cancel-order", {"instId": iid, "ordId": oid})

async def loop_martin(app, chat, S, spec, iid, d, pos, k):
    """馬丁模式主迴圈（M=2/3）：
    1. 等 TF 開盤 → 一次掛 M 張限價單
    2. 暫停下單，監控各單狀態
    3. 任何單 TP 或手動平倉 → 取消所有掛單 → 重新開始
    4. SL 連動：第1單SL出場 → 第2單進場 → 第3單等待
    5. 所有單出場後（30s 輪詢確認）→ 重新下一輪
    """
    martin = int(S.get("martin", 2))
    first_round = True
    try:
        while S["alive"]:
            # ── 等待下一根 TF 開盤 ──
            tf_sec = TF_SEC[S["tf"]]
            S["state"] = "等下輪（馬丁）"; save_state()
            oe = next_open_epoch(int(time.time()), S["tf"])
            w = oe - time.time()
            if w > 0: await asyncio.sleep(w)
            if not S["alive"]: break

            # ── 開盤取現價，計算各單埋伏價與尺寸 ──
            op = await get_last(iid)
            lev = Decimal(str(S["lev"]))
            margin = Decimal(str(S["margin"]))
            sl_pct = Decimal(str(S["sl"])) / 100
            tp_pct = Decimal(str(S["tp"])) / 100
            tick = spec["tick"]

            # 第1單埋伏價（同現行邏輯）
            amb1 = align(op * (1 - Decimal(str(S["offset"])) / 100) if d == "L"
                         else op * (1 + Decimal(str(S["offset"])) / 100), tick, d)

            # 第1單 SL/TP（以 amb1 為基準）
            if d == "L":
                sl1 = align(amb1 * (1 - sl_pct), tick, "L")
                tp1 = align(amb1 * (1 + tp_pct), tick, "S")
            else:
                sl1 = align(amb1 * (1 + sl_pct), tick, "S")
                tp1 = align(amb1 * (1 - tp_pct), tick, "L")

            # 第2單：埋伏價 = sl1，TP/SL 以 sl1 為基準
            amb2 = sl1
            if d == "L":
                sl2 = align(amb2 * (1 - sl_pct), tick, "L")
                tp2 = align(amb2 * (1 + tp_pct), tick, "S")
            else:
                sl2 = align(amb2 * (1 + sl_pct), tick, "S")
                tp2 = align(amb2 * (1 - tp_pct), tick, "L")

            # 第3單：埋伏價 = sl2，TP/SL 以 sl2 為基準
            if martin >= 3:
                amb3 = sl2
                if d == "L":
                    sl3 = align(amb3 * (1 - sl_pct), tick, "L")
                    tp3 = align(amb3 * (1 + tp_pct), tick, "S")
                else:
                    sl3 = align(amb3 * (1 + sl_pct), tick, "S")
                    tp3 = align(amb3 * (1 - tp_pct), tick, "L")
            else:
                amb3 = sl3 = tp3 = None

            # ── 清殘單，一次掛出全部馬丁單 ──
            await sweep(iid, pos)

            placed = []  # [(ordId, amb, sz, tp, sl, margin_x)]

            oid1, sz1 = await _martin_place_order(iid, pos, d, amb1, margin, lev, spec)
            if not oid1:
                await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 馬丁第1單掛單失敗，跳過本輪")
                await asyncio.sleep(3); continue
            placed.append({"oid": oid1, "amb": str(amb1), "sz": str(sz1),
                            "tp": str(tp1), "sl": str(sl1), "margin_x": 1, "algo_id": None, "filled": False})
            bump(k, "placed")

            if martin >= 2:
                oid2, sz2 = await _martin_place_order(iid, pos, d, amb2, margin * 2, lev, spec)
                if oid2:
                    placed.append({"oid": oid2, "amb": str(amb2), "sz": str(sz2),
                                    "tp": str(tp2), "sl": str(sl2), "margin_x": 2, "algo_id": None, "filled": False})
                    bump(k, "placed")

            if martin >= 3 and amb3:
                oid3, sz3 = await _martin_place_order(iid, pos, d, amb3, margin * 4, lev, spec)
                if oid3:
                    placed.append({"oid": oid3, "amb": str(amb3), "sz": str(sz3),
                                    "tp": str(tp3), "sl": str(sl3), "margin_x": 4, "algo_id": None, "filled": False})
                    bump(k, "placed")

            S["martin_orders"] = placed
            S["martin_paused"] = True
            S["state"] = "馬丁委託中"; save_state()

            labels = {1: "第1單", 2: "第2單", 3: "第3單"}
            if first_round:
                order_info = "\n".join(
                    f"{labels.get(i+1, f'第{i+1}單')}：埋伏{p['amb']} SL{p['sl']} TP{p['tp']}（{p['margin_x']}份）"
                    for i, p in enumerate(placed))
                await notify(app, chat,
                    f"{E.BOT} OKX原K｜{ACCT}\n事件：🎯 馬丁x{martin} 已掛出 {len(placed)} 張單\n"
                    f"━━━━━━━━━━\n商品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                    f"{order_info}\n━━━━━━━━━━\n下單策略已暫停，等待連動\n時間：{hhmmss()}")
                first_round = False

            # ── 監控：等待全部出場（或手動平倉/TP） ──
            active_algo_map = {}  # oid -> algo_id（已進場的單）
            filled_set = set()    # 已成交的 oid

            while S["alive"]:
                await asyncio.sleep(MARTIN_CHECK_INTERVAL)

                # 查詢 OKX 目前掛單（本 bot 的 n prefix）
                pending_orders = await okx_orders(iid, pos, prefix="n")
                pending_oids = {o["ordId"] for o in pending_orders}

                # 查詢 OKX 目前持倉
                cur_pos = await okx_pos(iid, pos)
                has_position = cur_pos is not None

                # 逐一檢查各單狀態
                for p in placed:
                    oid = p["oid"]
                    if p["filled"]:
                        continue  # 已知已成交，跳過
                    if oid in pending_oids:
                        continue  # 仍在掛單中

                    # 不在掛單 -> 查詢該單狀態
                    st = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={oid}")
                    if st.get("code") == "0" and st.get("data"):
                        order_state = st["data"][0].get("state", "")
                        if order_state == "filled":
                            if oid not in filled_set:
                                filled_set.add(oid)
                                p["filled"] = True
                                fpx_i = Decimal(st["data"][0].get("avgPx") or p["amb"])
                                sz_i = Decimal(p["sz"])
                                tp_i = Decimal(p["tp"])
                                sl_i = Decimal(p["sl"])
                                # 掛 algo OCO 單
                                algo_id_i = await place_algo(iid, pos, d, sz_i, tp_i, sl_i)
                                if algo_id_i:
                                    p["algo_id"] = algo_id_i
                                    active_algo_map[oid] = algo_id_i
                                bump(k, "entered")
                                # 設定停損追蹤（覆寫 S，frame_mover 會跟進）
                                ee_i = time.time()
                                S["pos_open"] = True
                                S["pos_px"] = str(fpx_i); S["pos_sz"] = str(sz_i)
                                S["pos_tp"] = str(tp_i); S["pos_sl"] = str(sl_i)
                                S["pos_ee"] = ee_i; S["pos_pt"] = ee_i
                                S["tp_px"] = str(tp_i); S["sl_px"] = str(sl_i)
                                S["frame_base"] = str(fpx_i)
                                S["move_n"] = 0; S["move_hist"] = []
                                S["last_move"] = ee_i; S["last_force_mv_t"] = 0
                                if algo_id_i:
                                    S["algo_id"] = algo_id_i
                                save_state()
                                label_i = labels.get(placed.index(p) + 1, "第?單")
                                await notify(app, chat,
                                    f"{E.BOT} OKX原K｜{ACCT}\n事件：🔔 {label_i} 已進場成交\n"
                                    f"━━━━━━━━━━\n商品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                                    f"進場：{fpx_i}（{label_i}）\n"
                                    f"止盈TP：{tp_i}({pct(S['tp'])}%)\n止損SL：{sl_i}({pct(S['sl'])}%)\n"
                                    f"移動SL：每{S['interval']}s追蹤 | 收盤推{pct(S['move_pct'])}%\n"
                                    f"時間：{hhmmss()}")

                # 判斷是否提前出場（TP 或手動平倉）：
                # 有單已成交 但 OKX 現在無持倉 且 已成交的單的 algo 也不在了
                any_filled = any(p["filled"] for p in placed)
                if any_filled and not has_position:
                    # 確認是否所有已成交的單都已出場（algo 已消失）
                    algo_ids_active = [aid for aid in active_algo_map.values() if aid]
                    all_algo_gone = True
                    if algo_ids_active:
                        r_algo = await api("GET", "/api/v5/trade/orders-algo-pending?ordType=oco")
                        pending_algos = {o.get("algoId") for o in (r_algo.get("data") or [])}
                        all_algo_gone = not any(aid in pending_algos for aid in algo_ids_active)

                    if all_algo_gone:
                        # 取消所有尚未成交的掛單
                        pending_oids_now = {p["oid"] for p in placed if not p["filled"]}
                        await _martin_cancel_pending(iid, pos, pending_oids_now)
                        # 清理 S 的持倉狀態
                        for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt",
                                  "pos_sz", "algo_id", "tp_px", "sl_px", "frame_base",
                                  "move_n", "move_hist", "last_move", "last_force_mv_t"):
                            S.pop(a, None)
                        S["martin_paused"] = False
                        S["state"] = "等下輪（馬丁）"; save_state()
                        await notify(app, chat,
                            f"{E.BOT} OKX原K｜{ACCT}\n事件：✅ 馬丁全部出場\n"
                            f"商品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                            f"已進場：{len(filled_set)} 單｜已取消掛單：{len(pending_oids_now)} 單\n"
                            f"下一輪等待下根TF開盤\n時間：{hhmmss()}")
                        break  # 跳出監控迴圈，進入下一輪

                # 全部單都未成交（第一單還沒進場）且 TF 已過期 → 撤單放棄本輪
                if not any_filled:
                    tf_end = oe + tf_sec
                    if time.time() > tf_end - ENTRY_CUTOFF:
                        not_filled_oids = [p["oid"] for p in placed if not p["filled"]]
                        await _martin_cancel_pending(iid, pos, not_filled_oids)
                        for a in ("pos_open", "martin_paused"):
                            S.pop(a, None)
                        S["state"] = "等下輪（馬丁）"; save_state()
                        break  # 跳出監控迴圈，進入下一輪

    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("loop_martin error", S.get("sym"), S.get("dir"), type(e).__name__, e)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 馬丁循環錯誤：{type(e).__name__}: {e}")
    finally:
        if SHUTTING_DOWN:
            S["state"] = "已停止"
        else:
            try:
                left = await sweep(iid, pos)
                if left:
                    await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 馬丁結束，已清除殘留掛單 {left} 筆")
            except Exception as e:
                print("loop_martin finally sweep fail", S.get("sym"), d, e)
            S["state"] = "已停止"; S["alive"] = False
            if STRATS.get(k) is S:
                STRATS.pop(k, None)
            try:
                if TASKS.get(k) is asyncio.current_task():
                    TASKS.pop(k, None)
            except Exception:
                pass
            save_state()

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
                await notify(app, chat, f"{E.BOT} {E.dir_emoji(d)} {S['sym']} {E.dir_word(d)} 已接管既有持倉，恢復 TP/SL 監控")
                await monitor(app, S, spec, iid, d, pos, size, fpx, tp, sl, ee, pt0, k)
            else:
                for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt"):
                    S.pop(a, None)
                save_state()

        # ── 馬丁模式（M=2/3）：一次掛多單，暫停下單直到全部出場 ──
        martin = int(S.get("martin") or 1)
        if martin >= 2:
            await loop_martin(app, chat, S, spec, iid, d, pos, k)
            return

        while S["alive"]:
            tf_sec = TF_SEC[S["tf"]]
            now = time.time()
            cur = int(now // tf_sec) * tf_sec
            room = cur + tf_sec - now
            # 僅在「上一輪未成交、剛撤完單」的情況下才允許盤中補掛；
            # 新建策略與出場後一律等下一根 K 線開盤，嚴守一根 K 線一輪。
            if S.get("catchup") and cur != S.get("last_open") and room >= ENTRY_CUTOFF:
                oe = cur
            else:
                S["state"] = "等下輪"; save_state()
                oe = next_open_epoch(int(time.time()), S["tf"])
                w = oe - time.time()
                if w > 0: await asyncio.sleep(w)
                if not S["alive"]: break
            S["last_open"] = oe; S["catchup"] = False; save_state()

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

            # 輪詢成交，直到 TF 剩餘不足 ENTRY_CUTOFF 秒
            tf_end = oe + tf_sec
            deadline = tf_end - ENTRY_CUTOFF   # 剩 60 秒就不再等成交
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
            if not filled:
                S["catchup"] = True
                continue

            # 成交 -> 算 TP/SL -> 掛 algo OCO 單 -> 監控
            bump(k, "entered")
            if d == "L":
                tp = align(fpx * (1 + S["tp"] / 100), spec["tick"], "S")
                sl = align(fpx * (1 - S["sl"] / 100), spec["tick"], "L")
            else:
                tp = align(fpx * (1 - S["tp"] / 100), spec["tick"], "L")
                sl = align(fpx * (1 + S["sl"] / 100), spec["tick"], "S")
            ee = time.time()
            S["state"] = "持倉中"
            S["pos_open"] = True; S["pos_px"] = str(fpx); S["pos_sz"] = str(size)
            S["pos_tp"] = str(tp); S["pos_sl"] = str(sl); S["pos_ee"] = ee; S["pos_pt"] = pt
            S["tp_px"] = str(tp); S["sl_px"] = str(sl)
            S["frame_base"] = str(fpx); S["move_n"] = 0; S["move_hist"] = []
            S["last_move"] = ee; S["last_force_mv_t"] = 0
            save_state()
            # 掛 OKX algo OCO 單
            algo_id = await place_algo(iid, pos, d, size, tp, sl)
            if algo_id:
                S["algo_id"] = algo_id
                save_state()
            else:
                await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} algo 單掛失敗，TP/SL 改用本地監控")
            await notify(app, chat,
                f"{E.BOT} OKX原K｜{ACCT}\n事件：🔔 已進場成交\n"
                f"━━━━━━━━━━\n"
                f"商品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                f"進場：{fpx}({pct(S['offset'])}%) | {datetime.fromtimestamp(ee, TZ8).strftime('%H:%M:%S')}\n"
                f"止盈TP：{tp}({pct(S['tp'])}%)\n止損SL：{sl}({pct(S['sl'])}%)\n"
                f"━━━━━━━━━━\n"
                f"移動SL：每{S['interval']}s追蹤 | 收盤推{pct(S['move_pct'])}%\n"
                f"獲利≥0.1%→緊貼現價0.1%\n"
                f"狀態：📌 持倉中\n時間：{hhmmss()}")
            await monitor(app, S, spec, iid, d, pos, size, fpx, tp, sl, ee, pt, k)
            # 出場後允許補掛：出場流程（查 OKX 真實損益）可能耗時數秒而跨進新 TF，
            # 若新 TF 尚未掛過且剩餘 >= ENTRY_CUTOFF 就立刻掛，避免整輪被跳過。
            # 若仍在同一個 TF（cur == last_open），迴圈頂端會照常睡到下一個 TF 開始。
            S["catchup"] = True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("loop error", S.get("sym"), S.get("dir"), type(e).__name__, e)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 循環錯誤：{type(e).__name__}: {e}")
    finally:
        if SHUTTING_DOWN:
            # 服務關閉：保留 STRATS 與存檔原狀，讓重啟後能完整認領
            S["state"] = "已停止"
        else:
            # 策略結束前先清掉自己掛在 OKX 上的單，否則會變成沒人認領的孤兒單
            try:
                left = await sweep(iid, pos)
                if left:
                    await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 策略結束，已清除自身殘留掛單 {left} 筆")
            except Exception as e:
                print("finally sweep fail", S.get("sym"), d, e)
            S["state"] = "已停止"; S["alive"] = False
            # 身分檢查：若 STRATS[k] 已被新策略取代，絕不能誤刪，
            # 否則新策略會從清單消失卻仍在背景掛單（幽靈策略）。
            if STRATS.get(k) is S:
                STRATS.pop(k, None)
            try:
                if TASKS.get(k) is asyncio.current_task():
                    TASKS.pop(k, None)
            except Exception:
                pass
            save_state()

# ---------- 啟動接管 ----------
async def rebuild_strat(d):
    spec = await get_spec(d["sym"])
    S = {"sym": d["sym"], "dir": d["dir"], "tf": d.get("tf", ACCOUNT_TF),
         "lev": int(d["lev"]), "margin": Decimal(str(d["margin"])),
         "offset": Decimal(str(d["offset"])), "tp": Decimal(str(d["tp"])),
         "sl": Decimal(str(d["sl"])),
         "move_pct": Decimal(str(d.get("move_pct", "0"))),
         "interval": int(d.get("interval", 5)),
         "spec": spec,
         "alive": True, "state": "等下輪", "chat": d.get("chat", CHAT_ID)}
    for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt",
              "last_open", "catchup", "algo_id", "tp_px", "sl_px",
              "frame_base", "move_n", "move_hist", "last_move", "last_force_mv_t",
              "pos_sz", "martin", "martin_orders", "martin_paused"):
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
    rec = []; failed = []
    for d in saved:
        try:
            S = await rebuild_strat(d)
            k = skey(S["sym"], S["dir"])
            STRATS[k] = S
            TASKS[k] = asyncio.create_task(loop(app, S["chat"], S))
            rec.append(f"{E.dir_emoji(S['dir'])} {S['sym']} {E.dir_word(S['dir'])}")
        except Exception as e:
            print("重建失敗", d, e)
            failed.append(f"{d.get('sym')} {d.get('dir')}：{type(e).__name__}")
    pend = await api("GET", "/api/v5/trade/orders-pending")
    posr = await api("GET", "/api/v5/account/positions")
    n_ord = len(pend.get("data", [])) if pend.get("code") == "0" else 0
    n_pos = len([p for p in posr.get("data", []) if float(p.get("pos", "0")) != 0]) if posr.get("code") == "0" else 0
    print(f"已接管策略 {len(rec)}｜OKX 掛單{n_ord} 持倉{n_pos}")
    if failed:
        print("重建失敗清單:", failed)
        if CHAT_ID:
            await notify(app, CHAT_ID, f"{E.BOT} {E.LOSS} 重啟時有 {len(failed)} 個策略重建失敗：\n" +
                         "\n".join("・" + x for x in failed) + "\n⚠ 這些策略已消失，請確認 OKX 是否有殘留掛單")
    if CHAT_ID and rec and n_ord > len(rec):
        await notify(app, CHAT_ID, f"{E.BOT} {E.LOSS} OKX 掛單 {n_ord} 筆 > 策略 {len(rec)} 個，可能有孤兒單，請查 /status")
    if CHAT_ID and rec:
        await notify(app, CHAT_ID,
            f"{E.BOT} OKX原K｜{ACCT}\n事件：🔄 重啟認領完成\n━━━━━━━━━━\n"
            f"已接管策略（{len(rec)}）：\n" + "\n".join("・" + x for x in rec) +
            f"\nOKX 現況：掛單{n_ord} 持倉{n_pos}\n循環已接管，繼續運作\n時間：{hhmmss()}")

# ---------- TG 指令 ----------
# ---------- K 線 / 振幅（/amp 用） ----------
NATIVE_BARS = {"3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "1H"}

async def get_klines(iid, bar, limit=300):
    """只取已收線（confirm=1）的 K 線，回傳舊->新。"""
    r = await pub(f"/api/v5/market/candles?instId={iid}&bar={bar}&limit={min(300, limit)}")
    if r.get("code") != "0":
        return []
    out = []
    for c in (r.get("data") or []):
        try:
            if len(c) >= 9 and str(c[8]) != "1":
                continue
            out.append({"ts": int(c[0]), "o": Decimal(c[1]), "h": Decimal(c[2]),
                        "l": Decimal(c[3]), "c": Decimal(c[4])})
        except Exception:
            continue
    out.reverse()
    return out

def _merge2(kl5):
    """兩根 5m 合成一根 10m，強制對齊 10 分鐘邊界。"""
    out = []
    i = 0
    n = len(kl5)
    while i < n:
        a = kl5[i]
        ts_a = int(a["ts"])
        if ts_a % 600000 != 0:
            i += 1; continue
        if i + 1 >= n:
            break
        b = kl5[i + 1]
        if int(b["ts"]) - ts_a != 300000:
            i += 1; continue
        out.append({"ts": ts_a, "o": a["o"], "h": max(a["h"], b["h"]),
                    "l": min(a["l"], b["l"]), "c": b["c"]})
        i += 2
    return out

async def klines_for_tf(iid, tf, want=300):
    """依 TF 取原始 K 線。10m 由兩根 5m 合成；其餘須為 OKX 原生週期。"""
    if tf == "10m":
        return _merge2(await get_klines(iid, "5m", 300))
    bar = NATIVE_BARS.get(tf)
    if not bar:
        return None
    return await get_klines(iid, bar, want)

def calc_amp(kl):
    """每根回傳 (振幅%, 漲跌幅%)。
    振幅% = (高-低) / 前一根收盤 * 100（恆正）
    漲跌幅% = (收盤-前一根收盤) / 前一根收盤 * 100（帶正負）
    第一根無前收，以本根開盤價替代。"""
    out = []
    for i, k in enumerate(kl):
        base = kl[i-1]["c"] if i > 0 else k["o"]
        if not base:
            out.append((Decimal(0), Decimal(0))); continue
        amp = (k["h"] - k["l"]) / base * 100
        chg = (k["c"] - base) / base * 100
        out.append((amp, chg))
    return out

async def _reply_long(u, head, lines, tail):
    """長清單拆多則送出（Telegram 單則上限 4096 字元）。"""
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
            m = m.replace("事件：振幅檢視", "事件：振幅檢視（%d/%d）" % (i+1, len(msgs)), 1)
        await reply(u, m)

def strat_params(sym, dr):
    S = STRATS.get(skey(sym, dr))
    if not S or not S.get("alive"):
        return f"{dr}（已停止）"
    return (f"{dr} {S['lev']}x {pct(S['margin'])} {pct(S['offset'])} "
            f"{pct(S['tp'])} {pct(S['sl'])} {pct(S.get('move_pct',0))}% {S.get('interval',0)}s")

async def cmd_run(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args
    fmt = (f"用法：/run 商品 方向 槓桿 保證金 進場距離% TP% SL% 移動門檻% 間隔秒 馬丁(1/2/3)\n"
           f"例：/run ETHUSDT L 1x 3 0.5 0.5 0.5 0.05 5 1\n"
           f"馬丁=1單注；2=連下2單(1+2份)；3=連下3單(1+2+4份)\n"
           f"週期依 /timeframe，目前 {ACCOUNT_TF}")
    if len(a) != 10: await reply(u, f"{E.BOT} 參數數量錯誤（需10個）\n{fmt}"); return
    try:
        sym = a[0].upper(); dr = a[1].upper(); lev = int(a[2].replace("x", ""))
        margin = Decimal(a[3]); offset = Decimal(a[4])
        tp = Decimal(a[5].rstrip("%")); sl = Decimal(a[6].rstrip("%"))
        move_pct = Decimal(a[7].rstrip("%")); interval = int(a[8])
        martin = int(a[9])
    except Exception:
        await reply(u, f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if dr not in ("L", "S"): await reply(u, f"{E.BOT} 方向須 L 或 S"); return
    for nm, v in (("移動門檻", move_pct), ("TP", tp), ("SL", sl)):
        if v < 0: await reply(u, f"{E.BOT} {nm} 不可為負數"); return
    if not 1 <= interval <= 300:
        await reply(u, f"{E.BOT} 間隔秒須介於 1~300"); return
    if martin not in (1, 2, 3):
        await reply(u, f"{E.BOT} 馬丁參數須為 1、2 或 3"); return
    k = skey(sym, dr)
    if k in STRATS and STRATS[k].get("alive"):
        await reply(u, f"{E.BOT} {sym} {E.dir_word(dr)} 已在運行"); return
    try: spec = await get_spec(sym)
    except Exception: await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return
    op = await get_last(spec["iid"])
    amb = align(op * (1 - offset / 100) if dr == "L" else op * (1 + offset / 100), spec["tick"], dr)
    # 預覽：計算第一單的 SL，進而推算後續各單的埋伏價
    if dr == "L":
        sl1 = align(amb * (1 - sl / 100), spec["tick"], "L")
        sl2 = align(sl1 * (1 - sl / 100), spec["tick"], "L") if martin >= 2 else None
    else:
        sl1 = align(amb * (1 + sl / 100), spec["tick"], "S")
        sl2 = align(sl1 * (1 + sl / 100), spec["tick"], "S") if martin >= 2 else None
    size1 = csize(margin, Decimal(lev), amb, spec["ctval"], spec["lot"])
    if size1 < spec["minsz"]:
        need = spec["minsz"] * spec["ctval"] * op / Decimal(lev)
        await reply(u, f"{E.BOT} {E.LOSS} 保證金不足：算出 {size1} 張 < 最小 {spec['minsz']}\n此槓桿下至少需約 {need:.4f} USDT"); return
    total_margin = margin * (1 if martin == 1 else 3 if martin == 2 else 7)
    PENDING[u.effective_chat.id] = {"kind": "run", "t": time.time(), "sym": sym, "dir": dr, "tf": ACCOUNT_TF,
        "lev": lev, "margin": margin, "offset": offset, "tp": tp, "sl": sl,
        "move_pct": move_pct, "interval": interval, "spec": spec, "martin": martin}
    martin_label = {1: "單注模式", 2: "馬丁x2（1+2份）", 3: "馬丁x3（1+2+4份）"}[martin]
    preview = (f"{E.BOT} OKX原K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
        f"商品：{E.dir_emoji(dr)} {sym} {E.dir_word(dr)} {lev}x\n週期：{ACCOUNT_TF}\n"
        f"模式：{martin_label}\n"
        f"開盤估價：{op}\n進場距離：{offset}%\n"
        f"第1單埋伏：{amb}（保證金{margin}）\n")
    if martin >= 2:
        preview += f"第2單埋伏：{sl1}（保證金{margin*2}，即第1單SL）\n"
    if martin >= 3:
        preview += f"第3單埋伏：{sl2}（保證金{margin*4}，即第2單SL）\n"
    preview += (f"止盈TP：{tp}%\n止損SL：{sl}%\n"
        f"所需總保證金：{total_margin} USDT\n"
        f"移動SL：每{interval}s追蹤 | 收盤推{pct(move_pct)}%\n"
        f"獲利≥0.1%→緊貼現價0.1%\n"
        f"━━━━━━━━━━\n⚠ 確認後真實循環交易\n下一步：60秒內 /confirm\n時間：{hhmmss()}")
    await reply(u, preview)
    asyncio.create_task(_to(c.application, u.effective_chat.id, PENDING[u.effective_chat.id]["t"]))

async def _to(app, chat, stamp):
    await asyncio.sleep(61)
    p = PENDING.get(chat)
    if p and p["t"] == stamp:
        k = p.get("kind", "run")
        del PENDING[chat]
        await notify(app, chat, f"{E.BOT} /{k} 逾時未確認，已取消")

async def cmd_confirm(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    p = PENDING.get(u.effective_chat.id)
    if not p: await reply(u, f"{E.BOT} 沒有待確認的指令"); return
    if time.time() - p["t"] > 60:
        del PENDING[u.effective_chat.id]; await reply(u, f"{E.BOT} 確認逾時"); return
    kind = p.get("kind", "run")
    if kind == "stop":
        del PENDING[u.effective_chat.id]
        await do_stop(u, p["key"]); return
    if kind == "stopall":
        del PENDING[u.effective_chat.id]
        await do_stopall(u); return
    del PENDING[u.effective_chat.id]
    k = skey(p["sym"], p["dir"])
    # 先確保同 key 沒有殘存的舊 task 還在跑（幽靈策略防護）
    old_t = TASKS.get(k)
    if old_t and not old_t.done():
        old_s = STRATS.get(k)
        if old_s: old_s["alive"] = False
        old_t.cancel()
        # 不用 await wait_for 阻塞 TG update 處理，直接讓舊 task 背景結束
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
    S = STRATS[tg[0]]
    PENDING[u.effective_chat.id] = {"kind": "stop", "t": time.time(), "key": tg[0]}
    await reply(u, f"{E.BOT} 將停止 {E.dir_emoji(S['dir'])} {S['sym']} {E.dir_word(S['dir'])}\n"
                   f"60秒內 /confirm 確認")
    asyncio.create_task(_to(c.application, u.effective_chat.id, PENDING[u.effective_chat.id]["t"]))

async def do_stop(u, key):
    S = STRATS.get(key)
    if not S or not S.get("alive"):
        await reply(u, f"{E.BOT} 策略已不存在"); return
    d = S["dir"]; iid = S["spec"]["iid"]
    ps = "long" if d == "L" else "short"
    p = await okx_pos(iid, ps)
    S["alive"] = False
    n = await sweep(iid, ps)
    save_state()
    tail = f"\n⚠ 持倉 {p['pos']} 張，請至 OKX 平倉" if p else ""
    await reply(u, f"{E.BOT} 已停止 {E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}｜撤單 {n}{tail}")

async def cmd_stopall(u, c):
    alive = [k for k, s in STRATS.items() if s.get("alive")]
    if not alive:
        await reply(u, f"{E.BOT} 目前無運行中策略"); return
    PENDING[u.effective_chat.id] = {"kind": "stopall", "t": time.time()}
    await reply(u, f"{E.BOT} ⚠ 將停止全部 {len(alive)} 個策略\n60秒內 /confirm 確認")
    asyncio.create_task(_to(c.application, u.effective_chat.id, PENDING[u.effective_chat.id]["t"]))

async def do_stopall(u):
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
    m = f"{E.BOT} 已停止 {len(done)} 個策略｜清殘單 {orphan}"
    if held: m += "\n⚠ 持倉需手動平倉：" + "、".join(held)
    await reply(u, m)

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
    L = [f"{E.BOT} OKX原K｜{ACCT}", "事件：現況（即時查OKX）", "━━━━━━━━━━",
         f"USDT權益：{eq}", f"可用餘額：{av}", f"帳戶週期：{ACCOUNT_TF}",
         f"運行中策略：{len(alive)}個"]
    for s in alive:
        k = skey(s["sym"], s["dir"]); placed, entered = get_stat(k)
        key = (s["spec"]["iid"], "long" if s["dir"] == "L" else "short")
        live = "持倉中" if key in okxp else ("委託中" if key in okxo else "等下輪")
        martin = int(s.get("martin") or 1)
        martin_label = f" 🎯馬丁x{martin}" if martin >= 2 else ""
        L.append("━━━━━━━━━━")
        L.append(f"{E.dir_emoji(s['dir'])} {s['sym']}：{live}(掛{placed}/進{entered}){martin_label}")
        L.append(f"參數：{strat_params(s['sym'], s['dir'])}")
        # 馬丁模式：顯示各單狀態
        if martin >= 2 and s.get("martin_orders"):
            for i, p in enumerate(s["martin_orders"]):
                tag = "✅已進場" if p.get("filled") else "⏳等待中"
                L.append(f"第{i+1}單({p.get('margin_x','')}份)：埋伏{p.get('amb','')} SL{p.get('sl','')} TP{p.get('tp','')}｜{tag}")
        if s.get("pos_open"):
            fpx_s = s.get("pos_px", "-")
            tp_s = s.get("tp_px") or s.get("pos_tp", "-")
            sl_s = s.get("sl_px") or s.get("pos_sl", "-")
            mn = s.get("move_n", 0)
            L.append(f"進場價：{fpx_s}")
            L.append(f"止盈TP：{tp_s}(固定)")
            L.append(f"止損SL：{sl_s}(目前)")
            L.append(f"SL移動：{mn}次")
            mhist = s.get("move_hist") or []
            for mrec in mhist:
                tp_label = "收" if mrec.get("type") == "定時" else "現"
                L.append(f"  {mrec.get('t','')} | {tp_label} | {mrec.get('px','')} | 止{mrec.get('sl','')}")
    L.append("━━━━━━━━━━")
    L.append(f"掛單數：{len(pdl)}｜持倉數：{len(pl)}")
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
    amb = ("%d秒" % (sum(int(r.get("ambush_s") or 0) for r in rs) / m)) if m else "-"
    L.append("次數:%d | %d(%s) | %.2f%%" % (placed, entered, amb, hit))
    NAME = {"Take_Profit": "TP", "Stop_Loss": "SL", "Frame_Exit": "SL"}
    for lab, cats in (("獲利", ("Take_Profit",)), ("虧損", ("Stop_Loss", "Frame_Exit"))):
        if lab == "獲利":
            sub = [r for r in rs if Decimal(str(r.get("net") or "0")) > 0]
        else:
            sub = [r for r in rs if Decimal(str(r.get("net") or "0")) < 0]
        ps = []
        for cn in cats:
            gg = [r for r in sub if r.get("reason") == cn]
            if gg:
                sec = "%d秒" % (sum(int(r.get("hold_s") or 0) for r in gg) / len(gg))
            else:
                sec = "0秒"
            ps.append("%s:%d(%s)" % (NAME[cn], len(gg), sec))
        L.append("%s數:%d | %s" % (lab, len(sub), " | ".join(ps)))
    tg = sum((Decimal(str(r.get("gross") or "0")) for r in rs), Decimal(0))
    tf = sum((Decimal(str(r.get("fee") or "0")) for r in rs), Decimal(0))
    tn = sum((Decimal(str(r.get("net") or "0")) for r in rs), Decimal(0))
    nv = sum((Decimal(str(r.get("nv") or "0")) for r in rs), Decimal(0))
    gp = (tg / nv * 100) if nv else Decimal(0)
    fp = (tf / nv * 100) if nv else Decimal(0)
    npc = (tn / nv * 100) if nv else Decimal(0)
    L.append("毛損益:%+.6f (%+.3f%%)" % (tg, gp))
    L.append("手續費:%+.6f (%+.3f%%)" % (tf, fp))
    L.append("淨損益:%+.6f (%+.3f%%) %s" % (tn, npc, E.pnl_emoji(tn)))
    return L

async def cmd_summary(u, c):
    t = today8(); recs = load_trades(t)
    ts = {k: v for k, v in STATS.items() if str(v.get("date")) == str(t)}
    L = [f"{E.BOT} OKX原K｜{ACCT}", f"📊📊📊 Summary {t}"]
    for dr in ("L", "S"):
        rows = [r for r in recs if r["dir"] == dr]
        pa = sum(v["placed"] for k, v in ts.items() if k.endswith("_" + dr))
        en = sum(v["entered"] for k, v in ts.items() if k.endswith("_" + dr))
        L.append(f"{E.dir_emoji(dr)} {E.dir_word(dr)}")
        L += sum_lines(rows, pa, en)
    L.append(f"時間:{hhmmss()}")
    await reply(u, "\n".join(L))
    for sy in sorted({r["sym"] for r in recs}):
        D = [f"\U0001f49a\U0001f499\U0001fa75\U0001f49c {sy} {t}"]
        for dr in ("L", "S"):
            rows = [r for r in recs if r["sym"] == sy and r["dir"] == dr]
            st_ = ts.get(skey(sy, dr)) or {"placed": 0, "entered": 0}
            D.append(f"策略:{E.dir_emoji(dr)} {strat_params(sy, dr)}")
            D += sum_lines(rows, st_["placed"], st_["entered"])
        D.append(f"時間:{hhmmss()}")
        await reply(u, "\n".join(D))

# ---------- /amp 振幅報表（Excel + Email） ----------
AMP_MAX = 2000       # 單次最多抓幾根
AMP_BINS = [Decimal(str(x)) for x in
            ("0.1","0.2","0.3","0.4","0.5","0.6","0.7","0.8","0.9","1.0","1.2","1.5",
             "2.0","2.5","3.0","3.5","4.0","5.0")]

async def get_klines_paged(iid, bar, want):
    """分頁往前抓 K 線（OKX 單次上限 300），回傳舊->新、只含已收線。"""
    out = []
    after = ""
    ep = "candles"          # 近期用 candles，翻不動時自動切 history-candles
    for _ in range(40):
        q = f"/api/v5/market/{ep}?instId={iid}&bar={bar}&limit=300"
        if after:
            q += f"&after={after}"
        r = await pub(q)
        if r.get("code") != "0":
            break
        batch = r.get("data") or []
        if not batch:
            if ep == "candles" and after:
                ep = "history-candles"      # candles 只保留近 ~1440 根，改用歷史端點續抓
                continue
            break
        got = []
        for c in batch:
            try:
                if len(c) >= 9 and str(c[8]) != "1":
                    continue
                got.append({"ts": int(c[0]), "o": Decimal(c[1]), "h": Decimal(c[2]),
                            "l": Decimal(c[3]), "c": Decimal(c[4])})
            except Exception:
                continue
        if not got:
            break
        out.extend(got)                      # OKX 回傳為新->舊
        after = str(min(int(x["ts"]) for x in got))
        if len(out) >= want + 10:
            break
        await asyncio.sleep(0.15)
    out.sort(key=lambda x: x["ts"])          # 轉成舊->新
    seen = set(); uniq = []
    for k in out:
        if k["ts"] in seen:
            continue
        seen.add(k["ts"]); uniq.append(k)
    return uniq[-want:] if want else uniq

async def klines_paged_for_tf(iid, tf, want):
    """依 TF 分頁取 K 線。10m 由兩根 5m 合成。"""
    if tf == "10m":
        return _merge2(await get_klines_paged(iid, "5m", want * 2 + 4))
    bar = NATIVE_BARS.get(tf)
    if not bar:
        return None
    return await get_klines_paged(iid, bar, want)

def build_amp_xlsx(results, tf, path):
    """振幅分析報表（多幣種，每幣一個 sheet，sheet name = 幣種）。
    results = [(sym, kl, amps, tick), ...]  tick = OKX tickSz（Decimal）
    欄位：幣種/週期/日期/時間/漲跌/開/高/低/收/漲跌幅%/振幅%/開到高/開到高%/開到低/開到低%
    右側分析表：開到高% / 開到低% 門檻統計（≥0.1% ~ ≥5.0%）
    header 底色：開到高群組 theme6+tint0.8（淡藍）、開到低群組 theme9+tint0.8（淡橘）
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Color
    from openpyxl.utils import get_column_letter

    FONT = "蘋方-繁 標準體"
    THRESHOLDS = [
        ("≥ 0.1%", 0.001), ("≥ 0.2%", 0.002), ("≥ 0.3%", 0.003),
        ("≥ 0.4%", 0.004), ("≥ 0.5%", 0.005), ("≥ 0.6%", 0.006),
        ("≥ 0.7%", 0.007), ("≥ 0.8%", 0.008), ("≥ 0.9%", 0.009),
        ("≥ 1.0%", 0.01),  ("≥ 1.2%", 0.012), ("≥ 1.5%", 0.015),
        ("≥ 2.0%", 0.02),  ("≥ 2.5%", 0.025), ("≥ 3.0%", 0.03),
        ("≥ 3.5%", 0.035), ("≥ 4.0%", 0.04),  ("≥ 5.0%", 0.05),
    ]
    # header 底色：theme6=淡藍（開到高群組 B~T）、theme9=淡橘（開到低群組 V~X）
    TINT = 0.7999816888943144
    fill_blue = PatternFill("solid", fgColor=Color(theme=6, tint=TINT, type="theme"))
    fill_orng = PatternFill("solid", fgColor=Color(theme=9, tint=TINT, type="theme"))

    wb = Workbook()
    wb.remove(wb.active)

    for sym, kl, amps, tick in results:
        # 依 OKX tickSz 決定價格小數位數
        dp = max(0, -tick.as_tuple().exponent)
        price_fmt = "0" if dp == 0 else "0." + "0" * dp

        ws = wb.create_sheet(title=sym)
        hdr = Font(name=FONT, bold=True, size=11)
        dat = Font(name=FONT, size=11)
        lft = Alignment(horizontal="left")
        cen = Alignment(horizontal="center")

        # 主欄標頭（row 2，col B~P，淡藍底色）
        main_heads = ["幣種", "週期", "日期", "時間", "漲跌",
                      "開", "高", "低", "收", "漲跌幅%", "振幅%",
                      "開到高", "開到高%", "開到低", "開到低%"]
        for ci, h in enumerate(main_heads, start=2):
            c = ws.cell(row=2, column=ci, value=h)
            c.font = hdr; c.alignment = cen; c.fill = fill_blue

        # 分析表標頭（row 2）：R/S/T 淡藍，V/W/X 淡橘
        for col, label, fill in [(18, "開到高%門檻", fill_blue), (19, "根數", fill_blue),
                                 (20, "佔比", fill_blue), (22, "開到低%門檻", fill_orng),
                                 (23, "根數", fill_orng), (24, "佔比", fill_orng)]:
            c = ws.cell(row=2, column=col, value=label)
            c.font = hdr; c.alignment = lft; c.fill = fill

        # 資料行（row 3 起）
        for i, (k, (amp, chg)) in enumerate(zip(kl, amps)):
            r = i + 3
            dt = datetime.fromtimestamp(int(k["ts"]) / 1000, TZ8)
            o = float(k["o"]); h = float(k["h"])
            lo = float(k["l"]); cl = float(k["c"])
            chg_pct = (cl - o) / o if o else 0
            amp_pct = (h - lo) / o if o else 0
            h2o = h - o; h2o_pct = h2o / o if o else 0
            l2o = o - lo; l2o_pct = l2o / o if o else 0
            flag = "🟩" if cl >= o else "🟥"
            vals = [sym, tf, dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"),
                    flag, o, h, lo, cl, chg_pct, amp_pct,
                    round(h2o, dp), h2o_pct, round(l2o, dp), l2o_pct]
            for ci, v in enumerate(vals, start=2):
                ws.cell(row=r, column=ci, value=v).font = dat
            # 開/高/低/收/開到高/開到低：依 tickSz 小數位
            for ci in [7, 8, 9, 10, 13, 15]:
                ws.cell(r, ci).number_format = price_fmt
            # 漲跌幅%：4位小數%（負數紅色）
            ws.cell(r, 11).number_format = "0.0000%;[Red]\\-0.0000%"
            # 振幅%/開到高%/開到低%：4位小數%
            ws.cell(r, 12).number_format = "0.0000%"
            ws.cell(r, 14).number_format = "0.0000%"
            ws.cell(r, 16).number_format = "0.0000%"

        # 分析表（row 1 SUM，row 3+ 門檻，佔比 2 位小數）
        last_r = len(kl) + 2
        ws.cell(1, 19, f"=COUNT($N3:$N{last_r})").font = Font(name=FONT, bold=True, size=11)
        ws.cell(1, 23, f"=COUNT($P3:$P{last_r})").font = Font(name=FONT, bold=True, size=11)
        for ti, (label, dec) in enumerate(THRESHOLDS):
            tr = ti + 3
            ps = f">={dec}"
            ws.cell(tr, 18, label).font = dat; ws.cell(tr, 18).alignment = lft
            ws.cell(tr, 19, f'=COUNTIF({sym}!$N3:$N{last_r},"{ps}")').font = dat
            c = ws.cell(tr, 20, f"=S{tr}/S$1"); c.font = dat; c.number_format = "0.00%"
            ws.cell(tr, 22, label).font = dat; ws.cell(tr, 22).alignment = lft
            ws.cell(tr, 23, f'=COUNTIF({sym}!$P3:$P{last_r},"{ps}")').font = dat
            c = ws.cell(tr, 24, f"=W{tr}/W$1"); c.font = dat; c.number_format = "0.00%"

        # Row/Col 字體（防 Excel 用系統預設）
        for rd in ws.row_dimensions.values():
            rd.font = Font(name=FONT, size=11)
        for cd in ws.column_dimensions.values():
            cd.font = Font(name=FONT, size=11)

        # 欄寬（依你修改後的版本）
        col_widths = {
            1: 4.4,   2: 12.8,  3: 6.8,   4: 16.8,  5: 11.4,
            6: 6.8,   7: 11.2,  11: 13.6, 12: 12.2, 13: 9.4,
            14: 12.2, 15: 9.0,  16: 12.2, 17: 3.0,  18: 16.0,
            19: 6.8,  20: 13.0, 21: 3.0,  22: 16.0, 23: 6.8,
            24: 13.0,
        }
        for ci, w in col_widths.items():
            ws.column_dimensions[get_column_letter(ci)].width = w
        ws.freeze_panes = "B3"

    wb.save(path)

def send_amp_mail(path, name, subject, body):
    """寄出振幅報表。回傳 (ok, 訊息)。"""
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
    msg["Subject"] = subject
    msg["From"] = user; msg["To"] = to
    msg.set_content(body)
    msg.add_attachment(open(path, "rb").read(),
                       maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename=name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as sv:
        sv.login(user, pwd); sv.send_message(msg)
    return True, to

async def cmd_amp(u, c):
    """原K 振幅分析報表（多幣種 Excel 寄信）。
    用法：/amp <根數> <幣種1> [幣種2 ... 幣種20]
    例：/amp 2000 BTCUSDT ETHUSDT DOGEUSDT
    根數 3~2000，幣種最多 20 個，TF 依當前設定。
    """
    if not c.args or len(c.args) < 2:
        await reply(u, f"{E.BOT} 用法：/amp <根數> <幣種1> [幣種2 ... 幣種20]\n"
                       f"例：/amp 2000 BTCUSDT ETHUSDT DOGEUSDT\n"
                       f"根數 3~{AMP_MAX}｜幣種最多 20 個｜TF 依當前設定（{ACCOUNT_TF}）\n"
                       f"產生 Excel（每幣一個 sheet）寄到信箱")
        return
    try:
        n = max(3, min(AMP_MAX, int(c.args[0])))
    except (ValueError, TypeError):
        await reply(u, f"{E.LOSS} 第一個參數必須是根數（整數），例：/amp 2000 BTCUSDT"); return
    raw_syms = [a.upper() for a in c.args[1:]]
    if len(raw_syms) > 20:
        await reply(u, f"{E.LOSS} 幣種最多 20 個，你輸入了 {len(raw_syms)} 個"); return
    tf = ACCOUNT_TF
    await reply(u, f"{E.BOT} 振幅分析報表中…\n"
                   f"幣種：{' / '.join(raw_syms)}\n"
                   f"TF：{tf}｜根數：{n}\n請稍候…")
    results = []; failed = []
    for sym in raw_syms:
        try:
            spec = await get_spec(sym)
        except Exception:
            failed.append(f"{sym}（找不到商品）"); continue
        try:
            kl = await klines_paged_for_tf(spec["iid"], tf, n)
        except Exception as e:
            failed.append(f"{sym}（K 線失敗：{type(e).__name__}）"); continue
        if not kl:
            failed.append(f"{sym}（K 線為空）"); continue
        kl = kl[-n:]
        amps = calc_amp(kl)
        results.append((sym, kl, amps, spec["tick"]))
    if not results:
        await reply(u, f"{E.LOSS} 全部幣種取得失敗：{', '.join(failed)}"); return
    day = now8().strftime("%Y%m%d")
    sym_tag = results[0][0] if len(results) == 1 else f"{len(results)}coins"
    name = f"OKX.{sym_tag}.{tf}{n}K.{day}.xlsx"
    path = f"/srv/1111bot/data/{name}"
    try:
        build_amp_xlsx(results, tf, path)
    except Exception as e:
        await reply(u, f"{E.LOSS} 產生 Excel 失敗：{type(e).__name__}: {e}"); return
    bars_info = " / ".join(f"{s}({len(kl)}根)" for s, kl, _, __ in results)
    subject = f"OKX 振幅分析 {tf} {n}根 {day}（{', '.join(s for s,_,_,__ in results)}）"
    body = f"TF：{tf}｜根數上限：{n}\n{bars_info}\n產生時間：{now8().strftime('%Y/%m/%d %H:%M:%S')}\n"
    if failed:
        body += f"\n取得失敗：{', '.join(failed)}\n"
    try:
        ok, info = send_amp_mail(path, name, subject, body)
    except Exception as e:
        await reply(u, f"{E.LOSS} 寄送失敗：{type(e).__name__}: {e}\n檔案已存於 VPS：{name}"); return
    if not ok:
        await reply(u, f"{E.LOSS} 未寄送：{info}\n檔案已存於 VPS：{name}"); return
    msg = (f"{E.BOT} ✅ 振幅分析報表已寄出\n"
           f"TF：{tf}｜根數：{n}\n{bars_info}\n")
    if failed:
        msg += f"⚠️ 失敗：{', '.join(failed)}\n"
    msg += f"時間：{hhmmss()}"
    await reply(u, msg)

async def cmd_coins(u, c):
    on = sorted([s["symbol"] for s in SYMS if s["enabled"]])
    L = [f"{E.BOT} OKX原K｜{ACCT}", "事件：幣種清單（即時）", "━━━━━━━━━━"]
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
        await reply(u, f"{E.BOT} 目前週期：{ACCOUNT_TF}\n可選：" + "/".join(TF_SEC.keys()) + "\n變更：/timeframe 10m"); return
    tf = c.args[0]
    if tf not in TF_SEC: await reply(u, f"{E.BOT} 週期須為：" + "/".join(TF_SEC.keys())); return
    ACCOUNT_TF = tf; save_state()
    await reply(u, f"{E.BOT} ✅ 帳戶週期已設為 {tf}\n（僅影響之後新建立的策略）")

async def cmd_menu(u, c):
    await reply(u, f"{E.BOT} OKX原K｜{ACCT}\n使用說明\n━━━━━━━━━━\n"
        "/run 商品 方向 槓桿 保證金 進場距離% TP% SL% 移動門檻% 間隔秒\n"
        f"例：/run ETHUSDT L 1x 3 0.5 0.5 0.5 0.05 5\n週期依 /timeframe（目前 {ACCOUNT_TF}）\n"
        "/confirm 確認啟動\n/stop 商品 方向\n/stopall 停全部+清殘單\n"
        "/status 所有策略現況\n/summary 當日戰報\n"
        "/amp 商品 根數  振幅報表 Excel 寄信（3~2000根）\n"
        "/timeframe 查看/設定週期\n/coins 幣種\n"
        "━━━━━━━━━━\n"
        f"一個 TF 一輪：TF 開始埋伏\n"
        f"未成交且剩餘不足 {ENTRY_CUTOFF}s → 撤單放棄本輪\n"
        "已進場 → OKX algo OCO 單守 TP/SL\n"
        "每根收盤推 SL（移動門檻%）｜每 N 秒現價追蹤 SL\n"
        "獲利≥0.1% → SL 緊貼現價 0.1%｜否則跟移 delta\n"
        "出場只有 TP / SL，無 TF 強平\n"
        "⚠ 真實下單，循環交易\n✅ 重啟接管持倉與掛單")

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
    CMDS = [BotCommand("status", "現況"),
            BotCommand("summary", "當日戰報"),
            BotCommand("coins", "幣種"),
            BotCommand("amp", "振幅報表 Excel"),
            BotCommand("stopall", "停全部"),
            BotCommand("stop", "停指定"),
            BotCommand("run", "建立策略"),
            BotCommand("timeframe", "週期"),
            BotCommand("menu", "說明")]
    # 清除所有 scope 的舊指令（ThisChat/AllPrivateChats 優先權高於 Default，
    # 只刪 Default 會被舊清單蓋住，導致左下 Menu 卡在舊版）
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
    asyncio.create_task(frame_mover(app))
    await startup_recover(app)

async def _post_stop(app):
    global SHUTTING_DOWN
    save_state()          # 關閉前最後一次完整存檔（此時 STRATS 仍完整）
    SHUTTING_DOWN = True
    print("關閉中：已保存狀態，停止後續寫檔")

def main():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    print(f"啟動 {ACCT} 原K B6-1 核心重寫版（token ...{TOKEN[-6:]}）")
    app = (Application.builder().token(TOKEN).post_init(_post_init).post_stop(_post_stop)
           .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0)
           .pool_timeout(30.0).get_updates_read_timeout(40.0)
           .get_updates_connect_timeout(30.0).build())
    for cmd, fn in [(["menu", "start"], cmd_menu), ("run", cmd_run), ("confirm", cmd_confirm),
                    ("stop", cmd_stop), ("stopall", cmd_stopall), ("status", cmd_status),
                    ("summary", cmd_summary), ("amp", cmd_amp),
                    ("timeframe", cmd_timeframe), ("coins", cmd_coins)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.run_polling()

if __name__ == "__main__":
    main()

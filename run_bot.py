#!/usr/bin/env python3
# 設計此腳本的目的在於用bot取代我在交易所app上的一切手動行為，切記
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
TF_SEC = {"5m": 300, "6m": 360, "8m": 480, "10m": 600,
          "12m": 720, "15m": 900, "20m": 1200, "25m": 1500, "30m": 1800}

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
NAKED_ALERT_SEC = 15  # 守門狗：有倉但未掛止盈止損超過 N 秒 → 只發一次 TG 告警（絕不自動平倉）

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
SAVE_FIELDS = ("sym","dir","lev","margin","offset","back_offset","tp","sl","move_pct","interval","chat",
               "locked_dir","pair_state",
               "front_oid","front_px","front_static_sl","front_tp_px","front_sl_px",
               "front_filled","front_ee","front_sz","front_move_n","front_move_hist",
               "back_algo_id","back_algo2_id","back_amb_px","back_px","back_static_sl",
               "back_tp_px","back_sl_px","back_filled","back_ee","back_sz","back_d",
               "algo_id","round_date","round_today","enter_today")

def save_state(_open=open, _replace=os.replace, _fsync=os.fsync, _dump=json.dump):
    # 關閉流程中絕不寫檔：此時 loop() 的 finally 會逐一 pop 掉 STRATS，
    # 若照常寫入就會把存檔覆蓋成空的，導致重啟後策略全滅。
    if SHUTTING_DOWN:
        return
    try:
        data = {"chat": CHAT_ID, "tf": ACCOUNT_TF, "stats": STATS, "strats": []}
        for k, S in STRATS.items():
            if S.get("pair_state", "idle") != "idle":
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
    """清掉本 bot 在該幣種該方向的所有殘留限價掛單。"""
    n = 0
    for o in await okx_orders(iid, pos):
        if keep and o.get("ordId") == keep: continue
        await api("POST", "/api/v5/trade/cancel-order", {"instId": iid, "ordId": o["ordId"]})
        n += 1
    return n

async def sweep_algos(iid, pos_side):
    """清掉該幣種該方向所有未觸發的計劃委託（trigger algo）。"""
    n = 0
    try:
        r = await api("GET", f"/api/v5/trade/orders-algo-pending?ordType=trigger&instId={iid}")
        if r.get("code") == "0":
            for o in (r.get("data") or []):
                if o.get("posSide") != pos_side:
                    continue
                algo_id = o.get("algoId")
                if not algo_id:
                    continue
                cr = await api("POST", "/api/v5/trade/cancel-algos",
                               [{"instId": iid, "algoId": algo_id}])
                if cr.get("code") == "0":
                    n += 1
    except Exception as e:
        print("sweep_algos error", iid, pos_side, type(e).__name__, e)
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
    """批次修改 algo 單止損價。items: [(instId, algoId, sl)]
    只修改 SL，TP 固定不動。
    OKX 一次最多 10 筆，超過自動分批。回傳成功筆數。"""
    ok = 0
    for i in range(0, len(items), 10):
        batch = items[i:i+10]
        body = [{"instId": iid, "algoId": aid, "newSlTriggerPx": str(sl)}
                for iid, aid, sl in batch]
        r = await api("POST", "/api/v5/trade/amend-algos", body)
        if r.get("code") == "0":
            ok += len(batch)
        else:
            print("amend_frames fail", r.get("msg"),
                  [(d.get("sCode"), d.get("sMsg")) for d in (r.get("data") or [])])
    return ok

def sl_shift(S, px, d):
    """依現價追蹤 SL；TP 永遠不動。
    兩段邏輯：
      ① 當下浮動獲利 ≥ FEE_RATE（0.1%）→ SL 緊貼現價，距離固定 FEE_RATE
      ② 否則 → 原邏輯：現價超過上次基準才跟移 delta U
    收盤推格由 frame_mover 另行處理，此函式只處理現價追蹤。"""
    try:
        fpx = Decimal(str(S.get("pos_px") or "0"))
        base = Decimal(str(S.get("frame_base") or fpx))
        cur = Decimal(str(px))
        tp = Decimal(str(S["tp_px"]))
        sl = Decimal(str(S["sl_px"]))
    except Exception:
        return None
    if fpx <= 0 or base <= 0:
        return None
    tick = S["spec"]["tick"]

    # 計算當下浮動獲利%
    if d == "L":
        profit_pct = (cur - fpx) / fpx
    else:
        profit_pct = (fpx - cur) / fpx

    if profit_pct >= FEE_RATE:
        # ① 獲利 ≥ 0.1%：SL 緊貼現價，距離 = FEE_RATE
        if d == "L":
            nsl = align(cur * (Decimal("1") - FEE_RATE), tick, "S")
        else:
            nsl = align(cur * (Decimal("1") + FEE_RATE), tick, "L")
        # SL 只能往有利方向移，不能後退
        if d == "L" and nsl <= sl:
            return None
        if d == "S" and nsl >= sl:
            return None
        gain = float(profit_pct * 100)
        return tp, nsl, cur, gain
    else:
        # ② 獲利 < 0.1%：原邏輯，現價超過上次基準才跟移
        if d == "L":
            delta = cur - base
            if delta <= 0:
                return None
            nsl = align(sl + delta, tick, "S")
        else:
            delta = base - cur
            if delta <= 0:
                return None
            nsl = align(sl - delta, tick, "L")
        if nsl == sl:
            return None
        gain = float(abs(delta) / base * 100)
        return tp, nsl, cur, gain

async def _place_limit(iid, pos_side, d, amb, sz, tp, sl, prefix="n"):
    """掛限價單（必帶 TP/SL），回傳 ordId 或 None。
    【下單鐵則】tp/sl 為必填。委託單掛出時就帶止盈止損，成交當下即生效，
    無裸倉空窗。這是下單的定義，不是選項 —— 寧可不下單，也不下沒有保護的單。"""
    if tp is None or sl is None:
        print("_place_limit 缺少 TP/SL，拒絕下單", iid, pos_side)
        return None
    r = await api("POST", "/api/v5/trade/order", {
        "instId": iid, "tdMode": "isolated",
        "side": "buy" if d == "L" else "sell",
        "posSide": pos_side,
        "ordType": "limit", "px": str(amb), "sz": str(sz),
        "clOrdId": prefix + uuid.uuid4().hex[:14],
        "attachAlgoOrds": [{
            "tpTriggerPx": str(tp), "tpOrdPx": "-1", "tpTriggerPxType": "last",
            "slTriggerPx": str(sl), "slOrdPx": "-1", "slTriggerPxType": "last",
        }]
    })
    if r.get("code") == "0" and r.get("data"):
        return r["data"][0]["ordId"]
    return None

async def _cancel_order(iid, oid):
    """撤銷單張限價掛單。"""
    await api("POST", "/api/v5/trade/cancel-order", {"instId": iid, "ordId": oid})


async def _place_trigger(iid, pos_side, d, trigger_px, sz, tp, sl, prefix="b"):
    """掛計劃委託（觸發後市價進場，taker，必帶 TP/SL），回傳 algoId 或 None。
    【下單鐵則】tp/sl 為必填。委託單掛出時就帶止盈止損，觸發成交當下即生效，
    無裸倉空窗。這是下單的定義，不是選項 —— 寧可不下單，也不下沒有保護的單。"""
    if tp is None or sl is None:
        print("_place_trigger 缺少 TP/SL，拒絕下單", iid, pos_side)
        return None
    r = await api("POST", "/api/v5/trade/order-algo", {
        "instId": iid, "tdMode": "isolated",
        "side": "buy" if d == "L" else "sell",
        "posSide": pos_side,
        "ordType": "trigger",
        "sz": str(sz),
        "triggerPx": str(trigger_px),
        "orderPx": "-1",           # -1 = 市價
        "triggerPxType": "last",   # 以最新成交價觸發
        "algoClOrdId": prefix + uuid.uuid4().hex[:14],
        "attachAlgoOrds": [{
            "tpTriggerPx": str(tp), "tpOrdPx": "-1", "tpTriggerPxType": "last",
            "slTriggerPx": str(sl), "slOrdPx": "-1", "slTriggerPxType": "last",
        }]
    })
    if r.get("code") == "0" and r.get("data"):
        return r["data"][0]["algoId"]
    print("_place_trigger fail", iid, d, r.get("msg"), (r.get("data") or [{}])[0].get("sMsg"))
    return None

async def _cancel_trigger(iid, algo_id):
    """撤銷計劃委託。"""
    await api("POST", "/api/v5/trade/cancel-algos",
              [{"instId": iid, "algoId": algo_id}])


async def _order_state(iid, oid):
    """查單張訂單狀態，回傳 (state, avgPx) 或 (None, None)。"""
    r = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={oid}")
    if r.get("code") == "0" and r.get("data"):
        d = r["data"][0]
        return d.get("state"), d.get("avgPx")
    return None, None

async def _okx_slot_check(iid):
    """查 OKX 三層：持倉 + 限價掛單 + trigger計劃委託，
    回傳 (long_count, short_count)，各方向上限為1。"""
    long_n = short_n = 0
    try:
        # 層1：持倉
        r = await api("GET", f"/api/v5/account/positions?instId={iid}")
        for p in (r.get("data") or []):
            ps = p.get("posSide", "")
            sz = Decimal(str(p.get("pos") or "0"))
            if ps == "long"  and sz > 0: long_n  += 1
            if ps == "short" and sz > 0: short_n += 1
        # 層2：限價掛單
        r2 = await api("GET", f"/api/v5/trade/orders-pending?instId={iid}")
        for o in (r2.get("data") or []):
            ps = o.get("posSide", "")
            if ps == "long":  long_n  += 1
            if ps == "short": short_n += 1
        # 層3：計劃委託 trigger
        _, _algos3 = await list_all_orders(iid)     # 涵蓋 trigger + oco
        for o in _algos3:
            ps = o.get("posSide", "")
            if ps == "long":  long_n  += 1
            if ps == "short": short_n += 1
    except Exception as e:
        print("_okx_slot_check error", type(e).__name__, e)
    return long_n, short_n


async def _place_pair(S, iid, chat, app, label="新一輪"):
    """掛出前後單一對，更新 S 的掛單欄位。輪次 +1，日期歸零判斷。"""
    # 輪次計數（日期歸零）
    today = today8()
    if S.get("round_date") != today:
        S["round_today"] = 0
        S["enter_today"] = 0
        S["round_date"]  = today
    S["round_today"] = int(S.get("round_today", 0)) + 1
    d    = S["dir"]

    # ── 距下一根TF不足120秒，等到開盤再掛 ──
    tf_sec = TF_SEC.get(S.get("tf", ACCOUNT_TF), 300)
    while True:
        secs_left = (int(time.time() // tf_sec) + 1) * tf_sec - time.time()
        if secs_left >= 120:
            break
        if S.get("pair_state") == "idle":
            return False   # 等待期間被/stop，放棄掛單
        await asyncio.sleep(5)

    # ── 掛單前完整檢查：掛單 + 持倉 都必須為 0 才准部署新戰役 ──
    # 有掛單→撤掉重檢；有持倉→等它自己出場（絕不平倉），重複同一SOP直到清空。
    _iid0 = S["spec"]["iid"]
    for _ in range(60):
        await cancel_all_orders(_iid0)
        clear, n_ord, n_pos = await _field_is_clear(_iid0)
        if clear:
            break
        if not S.get("alive", True):
            return False
        print(f"[部署前未清空] {S['sym']} 掛單={n_ord} 持倉={n_pos}，等待中")
        await asyncio.sleep(2)
    else:
        print(f"[部署前檢查逾時] {S['sym']} 放棄本次部署")
        return False
    back_d = "S" if d == "L" else "L"
    spec   = S["spec"]
    tick   = spec["tick"]
    lev    = Decimal(str(S["lev"]))
    margin = Decimal(str(S["margin"]))
    sl_pct = Decimal(str(S["sl"])) / 100
    tp_pct = Decimal(str(S["tp"])) / 100
    offset_pct = Decimal(str(S["offset"])) / 100

    # 取現價
    op = await get_last(iid)

    # 前單埋伏價
    if d == "L":
        front_amb = align(op * (1 - offset_pct), tick, "L")
    else:
        front_amb = align(op * (1 + offset_pct), tick, "S")

    # 前單靜態 SL（= 後單埋伏限價）
    if d == "L":
        front_static_sl = align(front_amb * (1 - sl_pct), tick, "L")
        front_tp        = align(front_amb * (1 + tp_pct), tick, "S")
    else:
        front_static_sl = align(front_amb * (1 + sl_pct), tick, "S")
        front_tp        = align(front_amb * (1 - tp_pct), tick, "L")

    # 後單觸發價 = A單靜態SL ± back_offset%（距A的SL保持固定距離）
    back_offset_pct = Decimal(str(S.get("back_offset", S["offset"]))) / 100
    if d == "S":   # A是SHORT，SL在上方；B是LONG，觸發在SL下方
        back_amb = align(front_static_sl * (1 - back_offset_pct), tick, back_d)
    else:          # A是LONG，SL在下方；B是SHORT，觸發在SL上方
        back_amb = align(front_static_sl * (1 + back_offset_pct), tick, back_d)
    if back_d == "S":
        back_static_sl = align(back_amb * (1 + sl_pct), tick, "S")
        back_tp        = align(back_amb * (1 - tp_pct), tick, "L")
    else:
        back_static_sl = align(back_amb * (1 - sl_pct), tick, "L")
        back_tp        = align(back_amb * (1 + tp_pct), tick, "S")

    sz_front = csize(margin, lev, front_amb, spec["ctval"], spec["lot"])
    sz_back  = csize(margin, lev, back_amb,  spec["ctval"], spec["lot"])

    front_pos = "long" if d == "L" else "short"
    back_pos  = "long" if back_d == "L" else "short"

    # 清殘單（限價單 + 計劃委託都清）
    await sweep(iid, front_pos)
    await sweep(iid, back_pos)
    await sweep_algos(iid, back_pos)   # 清舊的後單計劃委託

    # 掛前單
    front_oid = await _place_limit(iid, front_pos, d, front_amb, sz_front,
                                   front_tp, front_static_sl)
    if not front_oid:
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} 前單掛單失敗，暫停 5 秒後重試")
        return False

    # 掛後單（計劃委託 taker，觸發價 = back_amb）
    back_algo_id = await _place_trigger(iid, back_pos, back_d, back_amb, sz_back,
                                        back_tp, back_static_sl)
    if not back_algo_id:
        await _cancel_order(iid, front_oid)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} 後單掛單失敗，暫停 5 秒後重試")
        return False

    # 更新 S
    S["front_oid"]       = front_oid
    S["front_px"]        = str(front_amb)
    S["front_static_sl"] = str(front_static_sl)
    S["front_tp_px"]     = str(front_tp)
    S["front_sl_px"]     = str(front_amb)    # 動態SL初始值 = 進場價（進場後才開始移動）
    S["front_filled"]    = False
    S["front_ee"]        = None
    S["front_sz"]        = str(sz_front)
    S["front_move_n"]    = 0
    S["front_move_hist"] = []
    S["back_algo_id"]    = back_algo_id
    S["back_px"]         = str(back_amb)
    S["back_amb_px"]     = str(back_amb)   # 保存原始觸發埋伏價，後單SL後補回用
    S["back_static_sl"]  = str(back_static_sl)
    S["back_tp_px"]      = str(back_tp)
    S["back_filled"]     = False
    S["back_sz"]         = str(sz_back)
    S["back_d"]          = back_d
    S["state"]           = "委託中"
    S["pair_state"]      = "waiting"
    bump(skey(S["sym"], S["dir"]), "placed")
    save_state()
    return True


async def _okx_has_oco(iid, ps):
    """直接問 OKX：這個持倉方向上有沒有活著的 OCO(止盈止損)單。
    完全不看程式內部旗標 —— 守門狗要抓的正是『程式記錯』的情況。"""
    try:
        r = await api("GET", f"/api/v5/trade/orders-algo-pending?ordType=oco&instId={iid}")
        if r.get("code") != "0":
            return None  # 查不到就不判定，避免誤報
        for o in (r.get("data") or []):
            if o.get("posSide") == ps:
                return True
        return False
    except Exception as e:
        print("查 OCO 失敗", type(e).__name__, e)
        return None


async def _naked_guard(app, S, iid, ps, tag, now_t):
    """守門狗：OKX 上有倉、但 OKX 上沒有對應 OCO → 裸倉。
    判定一律以 OKX 為準，不看 back_filled / algo_id 等內部旗標。
    超過 NAKED_ALERT_SEC 秒 → 只發一次 TG 告警，絕不自動平倉（平倉一律人工）。"""
    if S.get("closing"):
        return
    since_key   = f"_naked_since_{ps}"
    alerted_key = f"_naked_alerted_{ps}"
    has_oco = await _okx_has_oco(iid, ps)
    if has_oco is None:
        return
    if has_oco:
        S.pop(since_key, None)
        S.pop(alerted_key, None)
        return
    t0 = S.get(since_key)
    if not t0:
        S[since_key] = now_t
        return
    if now_t - float(t0) >= NAKED_ALERT_SEC and not S.get(alerted_key):
        S[alerted_key] = True
        await notify(app, S.get("chat") or CHAT_ID,
            f"{E.BOT} {E.WARN} \u5b88\u9580\u72d7\u8b66\u544a\uff1a\u88f8\u5009\u672a\u639b\u6b62\u76c8\u6b62\u640d\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\u5546\u54c1\uff1a{S['sym']} {tag}\n"
            f"\u65b9\u5411\uff1a{ps}\n"
            f"\u5df2\u88f8\u5009\uff1a{int(now_t - float(t0))} \u79d2\n"
            f"OKX \u4e0a\u67e5\u4e0d\u5230\u6b64\u6301\u5009\u7684\u6b62\u76c8\u6b62\u640d\u55ae\uff0c\u8acb\u81f3 OKX \u624b\u52d5\u8655\u7406\u3002\n"
            f"\u6642\u9593\uff1a{hhmmss()}")


async def frame_mover(app):
    """前後單策略：每 interval 秒移動前單的動態SL。
    無論獲利或虧損，SL 每秒依 move_pct 往有利方向移動。
    SL 只能往有利方向移，不能後退。
    """
    await asyncio.sleep(3)
    print("原K止損移動任務已啟動")
    while True:
        try:
            await asyncio.sleep(MOVE_TICK - (time.time() % MOVE_TICK))
            now_t = time.time()

            candidates = [S for S in list(STRATS.values()) if S.get("pair_state", "idle") != "idle"]
            if not candidates:
                continue

            # 查 OKX 持倉，確認哪些策略真的有倉位（前單或後單）
            active = []
            for S in candidates:
                try:
                    d = S["dir"]
                    iid = S["spec"]["iid"]
                    front_ps = "long" if d == "L" else "short"
                    cur_front = await okx_pos(iid, front_ps)
                    back_d_v  = S.get("back_d", "S" if d == "L" else "L")
                    back_ps   = "long" if back_d_v == "L" else "short"
                    # 守門狗：B 邊一律查 OKX，不看 back_filled —— 孤兒倉正是旗標為 False 的那種
                    cur_back  = await okx_pos(iid, back_ps)
                    if cur_front:
                        active.append((S, "front", d, front_ps, "front_sl_px", "algo_id", cur_front))
                        await _naked_guard(app, S, iid, front_ps, "A/\u524d\u55ae", now_t)
                    else:
                        S.pop(f"_naked_since_{front_ps}", None)
                        S.pop(f"_naked_alerted_{front_ps}", None)
                    if cur_back:
                        await _naked_guard(app, S, iid, back_ps, "B/\u5f8c\u55ae", now_t)
                        if S.get("back_filled"):
                            active.append((S, "back", back_d_v, back_ps, "back_sl_px", "back_algo2_id", cur_back))
                    else:
                        S.pop(f"_naked_since_{back_ps}", None)
                        S.pop(f"_naked_alerted_{back_ps}", None)
                except Exception as e:
                    print("frame_mover 查持倉錯誤", S.get("sym"), type(e).__name__, e)
            if not active:
                continue

            due = [(S, side, d, ps, sl_field, aid_field, cur_pos) for S, side, d, ps, sl_field, aid_field, cur_pos in active
                   if now_t - float(S.get(f"_last_move_t_{side}", 0)) >= float(S.get("interval", 1))]
            if not due:
                continue

            # 批次查現價
            px_map = {}
            for S, side, d, ps, sl_field, aid_field, cur_pos in due:
                sym = S["sym"]
                if sym not in px_map:
                    try:
                        px_map[sym] = await get_last(S["spec"]["iid"])
                    except Exception:
                        px_map[sym] = None

            amends = []
            for S, side, d, ps, sl_field, aid_field, cur_pos in due:
                if S.get("closing"):
                    continue
                px = px_map.get(S["sym"])
                if not px:
                    continue
                tick = S["spec"]["tick"]
                move_pct = Decimal(str(S["move_pct"])) / 100
                entry_field = "front_px" if side == "front" else "back_px"
                try:
                    cur_sl = Decimal(str(S[sl_field]))
                    cur_px = Decimal(str(px))
                except Exception:
                    continue

                if d == "L":
                    profit_pct = (cur_px - Decimal(str(S.get(entry_field, cur_px)))) / Decimal(str(S.get(entry_field, cur_px)))
                    shift = max(profit_pct, move_pct)
                    nsl = align(cur_sl * (1 + shift), tick, "L")
                    if nsl <= cur_sl:
                        continue
                    if nsl >= cur_px:
                        nsl = align(cur_px - tick, tick, "L")
                        if nsl <= cur_sl:
                            continue
                else:
                    profit_pct = (Decimal(str(S.get(entry_field, cur_px))) - cur_px) / Decimal(str(S.get(entry_field, cur_px)))
                    shift = max(profit_pct, move_pct)
                    nsl = align(cur_sl * (1 - shift), tick, "S")
                    if nsl >= cur_sl:
                        continue
                    if nsl <= cur_px:
                        nsl = align(cur_px + tick, tick, "S")
                        if nsl >= cur_sl:
                            continue

                algo_id = S.get(aid_field)
                if not algo_id:
                    continue

                move_type = "跟" if abs(profit_pct) >= move_pct and profit_pct != move_pct else "底"
                S[f"_pending_sl_{side}"] = (str(nsl), str(cur_px), move_type)
                amends.append((S["spec"]["iid"], algo_id, nsl, S, side, sl_field, entry_field))

            if amends:
                items = [(iid, aid, sl) for iid, aid, sl, _, _s, _sf, _ef in amends]
                okn = await amend_frames(items)
                for iid, aid, sl, S, side, sl_field, entry_field in amends:
                    pend = S.pop(f"_pending_sl_{side}", None)
                    if not pend:
                        continue
                    nsl, npx, move_type = pend
                    hist_key = "front_move_hist" if side == "front" else "back_move_hist"
                    mh = S.get(hist_key)
                    if not isinstance(mh, list):
                        mh = []; S[hist_key] = mh
                    label = f"{S['sym']} {S['dir'] if side == 'front' else S.get('back_d','?')}"
                    if okn:
                        # 成功：更新 SL、計次
                        S[sl_field] = nsl
                        move_n_key = f"{'front' if side == 'front' else 'back'}_move_n"
                        S[move_n_key] = int(S.get(move_n_key, 0)) + 1
                        S[f"_last_move_t_{side}"] = now_t
                        print(f"[SL移動] {label} {side} {move_type} 現價={npx} 新SL={nsl} 第{S[move_n_key]}次")
                        mh.append({"t": hhmmss(), "type": move_type, "px": npx, "sl": nsl})
                    else:
                        # 失敗：SL 不變（上一次的 SL 仍守著），但一樣留下痕跡
                        fail_key = f"{'front' if side == 'front' else 'back'}_move_fail_n"
                        S[fail_key] = int(S.get(fail_key, 0)) + 1
                        print(f"[SL移動失敗] {label} {side} 現價={npx} 欲改SL={nsl} 累計失敗{S[fail_key]}次")
                        mh.append({"t": hhmmss(), "type": "失", "px": npx, "sl": nsl})
                    if len(mh) > 200:
                        S[hist_key] = mh[-200:]
                    save_state()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("frame_mover error", type(e).__name__, e)
            await asyncio.sleep(5)



ALGO_TYPES = ("trigger", "oco")   # 本腳本用到的兩種 algo 單：觸發進場、OCO止盈止損


async def list_all_orders(iid=None, pos_side=None):
    """查該幣種所有掛單。回傳 (普通單list, algo單list)。
    【統一入口】algo 單一律涵蓋 trigger + oco 兩類 ——
    只查 trigger 會漏掉 OCO，撤不乾淨、數量也不準。全檔查掛單都走這裡。"""
    q = f"?instId={iid}" if iid else ""
    r1 = await api("GET", f"/api/v5/trade/orders-pending{q}")
    orders = [o for o in (r1.get("data") or [])
              if (not pos_side or o.get("posSide") == pos_side)]
    algos = []
    for ot in ALGO_TYPES:
        sep = "&" if q else "?"
        r2 = await api("GET", f"/api/v5/trade/orders-algo-pending{q}{sep}ordType={ot}")
        algos += [o for o in (r2.get("data") or [])
                  if (not pos_side or o.get("posSide") == pos_side)]
    return orders, algos


async def cancel_all_orders(iid=None, pos_side=None, tries=5):
    """撤光該幣種（或該方向）所有掛單：普通單 + trigger + oco。
    確認 OKX 回報 0 張才返回 True。絕不碰持倉。"""
    for _ in range(tries):
        orders, algos = await list_all_orders(iid, pos_side)
        if not orders and not algos:
            return True
        if orders:
            for i in range(0, len(orders), 20):
                await api("POST", "/api/v5/trade/cancel-batch-orders",
                          [{"instId": o["instId"], "ordId": o["ordId"]} for o in orders[i:i+20]])
        if algos:
            for i in range(0, len(algos), 20):
                await api("POST", "/api/v5/trade/cancel-algos",
                          [{"instId": o["instId"], "algoId": o["algoId"]} for o in algos[i:i+20]])
        await asyncio.sleep(0.5)
    return False


async def _pre_clear_orders(iid):
    """掛單前標準前置步驟：清空該幣種所有掛單，確認OKX=0才返回。"""
    return await cancel_all_orders(iid)


async def _algo_actual_side(iid, algo_id, tries=6):
    """查 OKX algo(OCO) 單真實觸發端：回傳 "tp" / "sl" / None。
    以 OKX actualSide 為準，不用損益推論。"""
    if not algo_id:
        return None
    for _ in range(tries):
        try:
            r = await api("GET", f"/api/v5/trade/orders-algo-history?ordType=oco&instId={iid}&state=effective&limit=20")
            if r.get("code") == "0":
                for o in (r.get("data") or []):
                    if str(o.get("algoId")) == str(algo_id):
                        a = (o.get("actualSide") or "").lower()
                        if a in ("tp", "sl"):
                            return a
                        return None
        except Exception as e:
            print("查 algo actualSide 失敗", type(e).__name__, e)
        await asyncio.sleep(1)
    return None


async def _get_net_pnl(iid, pos_side, after_ms):
    """查 OKX 出場淨損益（realizedPnl）。"""
    rec = await close_record(iid, pos_side, after_ms, tries=10)
    if rec:
        return Decimal(str(rec.get("realizedPnl") or "0")), rec
    return Decimal("0"), None


async def _query_open_fee(iid, pos_side, entry_epoch):
    """查開倉手續費（fills API）。"""
    open_fee = Decimal("0")
    try:
        r = await api("GET", f"/api/v5/trade/fills?instId={iid}&limit=10")
        if r.get("code") == "0":
            for f in (r.get("data") or []):
                if (f.get("posSide") == pos_side and
                        abs(int(f.get("ts") or 0) - int(entry_epoch * 1000)) <= 5000):
                    open_fee += Decimal(str(f.get("fee") or "0"))
                    print(f"[開倉手續費] {f.get('fee')} ts={f.get('ts')}")
    except Exception as e:
        print("查開倉手續費失敗", type(e).__name__, e)
    return open_fee


def _sl_block(mn, mhist, fn=0):
    """建立 SL 移動明細區塊。fn=失敗次數。
    凡走過必留痕跡：成功(跟/底)與失敗(失)都列入明細，時間順序不打散。"""
    head = f"SL移動 {mn} 次" + (f"（失敗 {fn} 次）" if fn else "")
    lines = ["━━━━━━━━━━", head]
    if mhist:
        for m in mhist[-20:]:
            lines.append(f"{m.get('t','')} | {m.get('type','現')} | {m.get('px','')} | 止{m.get('sl','')}")
    return "\n".join(lines)


async def _notify_exit(app, chat, S, side, rec, open_fee, reason_label, extra=""):
    """統一出場通知格式（與用戶核准的格式一致）。"""
    _is_a = side.startswith("A")   # side 為 A單/B單/A補單/B補單
    d = S["dir"] if _is_a else S.get("back_d", "S" if S["dir"] == "L" else "L")
    sym = S["sym"]
    mv = float(Decimal(str(S.get("margin", "1"))))

    _pos_side = ("long" if d == "L" else "short")
    _iid_x = S["spec"]["iid"]
    _otp, _osl = await okx_tpsl(_iid_x, _pos_side)   # 以OKX為主：出場當下查實際掛的TP/SL
    if _is_a:
        entry_px  = S.get("front_px", "-")
        entry_ee  = S.get("front_ee", 0)
        static_tp = _otp or S.get("front_tp_px", "-")
        static_sl = _osl or S.get("front_static_sl", "-")
        last_sl   = S.get("front_sl_px", "-")
        mn        = int(S.get("front_move_n", 0))
        mhist     = S.get("front_move_hist") or []
    else:
        entry_px  = S.get("back_px", "-")
        entry_ee  = S.get("back_ee", 0)
        static_tp = _otp or S.get("back_tp_px", "-")
        static_sl = _osl or S.get("back_static_sl", "-")
        last_sl   = S.get("back_sl_px", "-")
        mn        = int(S.get("back_move_n", 0))
        mhist     = S.get("back_move_hist") or []

    entry_t = datetime.fromtimestamp(float(entry_ee), TZ8).strftime("%H:%M:%S") if entry_ee else "-"

    if rec:
        g_r   = Decimal(str(rec.get("pnl") or "0"))
        fee_r = Decimal(str(rec.get("fee") or "0")) + open_fee
        net_r = Decimal(str(rec.get("realizedPnl") or "0")) + open_fee
        xpx   = str(rec.get("closeAvgPx") or "-")
    else:
        g_r = fee_r = net_r = Decimal("0")
        xpx = "-"

    g_pct   = float(g_r)   / mv * 100 if mv else 0
    fee_pct = float(fee_r) / mv * 100 if mv else 0
    net_pct = float(net_r) / mv * 100 if mv else 0
    ico = E.WIN if net_r >= 0 else E.LOSS

    _fn = int((S.get("front_move_fail_n", 0) if _is_a else S.get("back_move_fail_n", 0)))
    sl_b = _sl_block(mn, mhist, _fn)

    msg = (
        f"{E.BOT} OKX原K｜{ACCT}\n"
        f"事件：{ico} {side}出場成交\n"
        f"━━━━━━━━━━\n"
        f"商品：{E.dir_emoji(d)} {sym} {E.dir_word(d)} {S.get('lev')}x {S.get('margin')}\n"
        f"{side}：{reason_label}\n"
        f"━━━━━━━━━━\n"
        f"進場：{entry_px} | {entry_t}\n"
        f"靜態TP：{static_tp}\n"
        f"靜態SL：{static_sl}\n"
        f"最後SL：{last_sl}\n"
        f"出場：{xpx} | {hhmmss()}\n"
        f"━━━━━━━━━━\n"
        f"毛損益：{g_r:+.6f} ({g_pct:+.3f}%)\n"
        f"手續費：{fee_r:.6f} ({fee_pct:+.3f}%)\n"
        f"淨損益：{net_r:+.6f} ({net_pct:+.3f}%) {ico}\n"
        f"{sl_b}\n"
        f"━━━━━━━━━━\n"
        f"{extra}\n"
        f"時間：{hhmmss()}"
    )
    await notify(app, chat, msg)
    return g_r, fee_r, net_r


async def okx_tpsl(iid, pos_side):
    """查 OKX 上該方向【實際掛著】的 TP/SL，回傳 (tp, sl)；查不到回 (None, None)。
    【以OKX為主】畫面顯示一律走這裡 —— 記憶體存的是「程式打算用的值」，
    不等於 OKX 上真的掛著的值。兩者一旦不符，畫面必須說實話。
    來源優先序：持倉上的 OCO 掛單 > 未成交委託單附帶的 attachAlgoOrds。"""
    try:
        # 1) 已進場：查該方向活著的 OCO 單
        r = await api("GET", f"/api/v5/trade/orders-algo-pending?ordType=oco&instId={iid}")
        if r.get("code") == "0":
            for o in (r.get("data") or []):
                if o.get("posSide") == pos_side:
                    return (o.get("tpTriggerPx") or None), (o.get("slTriggerPx") or None)
        # 2) 未成交：查委託單上附帶的 TP/SL
        for ot in ("trigger",):
            r = await api("GET", f"/api/v5/trade/orders-algo-pending?ordType={ot}&instId={iid}")
            if r.get("code") == "0":
                for o in (r.get("data") or []):
                    if o.get("posSide") == pos_side:
                        att = (o.get("attachAlgoOrds") or [{}])[0]
                        return (att.get("tpTriggerPx") or None), (att.get("slTriggerPx") or None)
        r = await api("GET", f"/api/v5/trade/orders-pending?instId={iid}")
        if r.get("code") == "0":
            for o in (r.get("data") or []):
                if o.get("posSide") == pos_side:
                    att = (o.get("attachAlgoOrds") or [{}])[0]
                    return (att.get("tpTriggerPx") or None), (att.get("slTriggerPx") or None)
    except Exception as e:
        print("查 OKX TP/SL 失敗", type(e).__name__, e)
    return None, None


async def _field_is_clear(iid):
    """戰場是否完全清空：無任何掛單(普通+trigger+oco) 且 無任何持倉。
    回傳 (是否清空, 掛單數, 持倉數)。"""
    orders, algos = await list_all_orders(iid)
    n_ord = len(orders) + len(algos)
    n_pos = 0
    r = await api("GET", "/api/v5/account/positions?instType=SWAP")
    if r.get("code") == "0":
        for p in (r.get("data") or []):
            if p.get("instId") == iid:
                try:
                    if float(p.get("pos") or 0) != 0:
                        n_pos += 1
                except Exception:
                    pass
    return (n_ord == 0 and n_pos == 0), n_ord, n_pos


async def _wait_field_clear(S, iid, max_wait=3600):
    """【SOP二 步驟3+4】等戰場完全清空。
    絕不平倉：剩餘持倉靠自己的 TP/SL 與移動SL 出場（移動SL 是獨立任務，照常運作）。
    仍有掛單則再撤一次（撤單可重複，持倉不動）。
    回傳 True=已清空可繼續；False=策略已停止，放棄。"""
    t0 = time.time()
    while True:
        if not S.get("alive", True):
            return False   # /stop 或 /stopall 已停止此策略
        clear, n_ord, n_pos = await _field_is_clear(iid)
        if clear:
            return True
        if n_ord and not n_pos:
            # 沒持倉卻還有掛單 —— 再撤一次（重複執行同一 SOP）
            await cancel_all_orders(iid)
        if time.time() - t0 > max_wait:
            print(f"[等待清空逾時] {iid} 掛單={n_ord} 持倉={n_pos}")
            return False
        await asyncio.sleep(2)


async def _handle_win(S, iid, chat, app, winner, pnl, rec, other_side, other_filled):
    """【SOP二】戰役結束（任一單淨損益>0）：
    撤光所有掛單 → 剩餘持倉等它自己出場（移動SL照推，絕不平倉）
    → 確認戰場清空 → 等TF≥120s → 用當下現價重新部署。"""
    S["pair_state"] = "idle"
    d = S["dir"]
    # 偵測到結束就直接撤單，不做前置檢查；涵蓋普通單+trigger+oco。絕不平倉。
    await cancel_all_orders(iid)
    a_side = "long" if d == "L" else "short"
    b_side = other_side
    win_side_pos = a_side if winner == "A" else b_side
    win_ee = S.get("front_ee", 0) if winner == "A" else S.get("back_ee", 0)
    open_fee = await _query_open_fee(iid, win_side_pos, float(win_ee or 0))
    win_algo_id = S.get("algo_id") if winner == "A" else S.get("back_algo2_id")
    _side = await _algo_actual_side(iid, win_algo_id)
    if _side == "tp":
        reason_label, reason_code = "Take Profit", "Take_Profit"
    elif _side == "sl":
        reason_label, reason_code = "Stop Loss", "Stop_Loss"
    else:
        reason_label, reason_code = "Manual/Unknown", "Manual"
    _ps_now = S.get("pair_state", "")
    if winner == "A":
        _wname = "A\u88dc\u55ae" if _ps_now == "A_REFILL_B_IN" else "A\u55ae"
    else:
        _wname = "B\u88dc\u55ae" if _ps_now == "A_IN_B_REFILL" else "B\u55ae"
    g_r, fee_r, net_r = await _notify_exit(app, chat, S, _wname, rec, open_fee, reason_label, extra="戰役結束：已撤光掛單，等待剩餘持倉出場")
    mv = float(Decimal(str(S.get("margin", "1"))))
    log_trade({"date": today8(), "sym": S["sym"], "dir": d, "reason": reason_code,
               "gross": float(g_r), "fee": float(fee_r), "net": float(net_r),
               "nv": mv, "hold_s": int(time.time() - float(win_ee or time.time())), "ambush_s": 0})
    for a in ("front_oid","front_px","front_static_sl","front_tp_px","front_sl_px",
              "front_filled","front_ee","front_sz","front_move_n","front_move_hist",
              "back_algo_id","back_algo2_id","back_amb_px","back_px","back_static_sl",
              "back_tp_px","back_sl_px","back_filled","back_ee","back_sz","back_d",
              "back_move_n","back_move_hist",
              "front_move_fail_n","back_move_fail_n",
              "algo_id"):
        S.pop(a, None)
    S["dir"] = S.get("locked_dir", d)
    # 【SOP二 步驟3】等剩餘持倉自己出場 —— 絕不平倉。移動SL 是獨立任務，期間照常推。
    if not await _wait_field_clear(S, iid):
        return
    # 【SOP二 步驟4】戰場確認清空後，才等TF，再用當下現價重新部署
    tf_sec = TF_SEC.get(S.get("tf", ACCOUNT_TF), 300)
    while True:
        if not S.get("alive", True):
            return
        secs_left = (int(time.time() // tf_sec) + 1) * tf_sec - time.time()
        if secs_left >= 120:
            break
        await asyncio.sleep(5)
    S["pair_state"] = "waiting"
    save_state()
    await _place_pair(S, iid, chat, app, label="\u65b0\u6230\u5f79")


async def _handle_lose_a(S, iid, chat, app, rec, pnl):
    """A虧損出場：補A觸發（觸發價 = B靜態SL ± back_offset%）。"""
    d      = S["dir"]
    a_side = "long" if d == "L" else "short"
    b_static_sl     = Decimal(str(S.get("back_static_sl", "0")))
    back_offset_pct = Decimal(str(S.get("back_offset", S.get("offset","0")))) / 100
    tick   = S["spec"]["tick"]
    sl_pct = Decimal(str(S["sl"])) / 100
    tp_pct = Decimal(str(S["tp"])) / 100
    if d == "L":   # A是LONG，B是SHORT；補A觸發在B靜態SL下方
        orig_a_px = align(b_static_sl * (1 - back_offset_pct), tick, "L")
        new_sl    = align(orig_a_px * (1 - sl_pct), tick, "L")
        new_tp    = align(orig_a_px * (1 + tp_pct), tick, "S")
    else:          # A是SHORT，B是LONG；補A觸發在B靜態SL上方
        orig_a_px = align(b_static_sl * (1 + back_offset_pct), tick, "S")
        new_sl    = align(orig_a_px * (1 + sl_pct), tick, "S")
        new_tp    = align(orig_a_px * (1 - tp_pct), tick, "L")
    open_fee = await _query_open_fee(iid, a_side, float(S.get("front_ee") or 0))
    _aname = "A\u88dc\u55ae" if S.get("pair_state") == "A_REFILL_B_IN" else "A\u55ae"
    await _notify_exit(app, chat, S, _aname, rec, open_fee, "Stop Loss", extra=f"補A觸發委託，觸發價：{orig_a_px}")
    r1 = await api("GET", f"/api/v5/trade/orders-pending?instId={iid}")
    a_orders = [o for o in (r1.get("data") or []) if o.get("posSide") == a_side]
    if a_orders:
        await api("POST", "/api/v5/trade/cancel-batch-orders",
                  [{"instId": iid, "ordId": o["ordId"]} for o in a_orders])
        await asyncio.sleep(0.3)
    sz = Decimal(str(S.get("front_sz", "1")))
    new_algo = await _place_trigger(iid, a_side, d, orig_a_px, sz, new_tp, new_sl)
    if new_algo:
        S["front_oid"] = None
        S["front_filled"] = False
        S["front_sl_px"] = str(new_sl)
        S["front_tp_px"] = str(new_tp)
        S["front_static_sl"] = str(new_sl)
        S["front_move_n"] = 0
        S["front_move_hist"] = []
        S["algo_id"] = None
        S["pair_state"] = "A_REFILL_B_IN"
        save_state()
        print(f"[\u88dcA] {S['sym']} {d} \u89f8\u767c\u50f9={orig_a_px}")
    else:
        await notify(app, chat, f"{E.BOT} {E.LOSS} \u88dcA\u89f8\u767c\u59d4\u8098\u5931\u6557\uff0c\u8acb\u624b\u52d5\u8655\u7406\n\u6642\u9593\uff1a{hhmmss()}")
        S["pair_state"] = "idle"


async def _handle_lose_b(S, iid, chat, app, rec, pnl):
    """B虧損出場：補B觸發（觸發價 = A靜態SL ± back_offset%）。"""
    back_d = S.get("back_d", "S" if S["dir"] == "L" else "L")
    b_side = "long" if back_d == "L" else "short"
    d      = S["dir"]
    a_static_sl     = Decimal(str(S.get("front_static_sl", "0")))
    back_offset_pct = Decimal(str(S.get("back_offset", S.get("offset","0")))) / 100
    tick   = S["spec"]["tick"]
    sl_pct = Decimal(str(S["sl"])) / 100
    tp_pct = Decimal(str(S["tp"])) / 100
    if d == "L":   # A是LONG，SL在下方；補B（SHORT）觸發在A靜態SL上方
        orig_b_px = align(a_static_sl * (1 + back_offset_pct), tick, "S")
        new_sl    = align(orig_b_px * (1 + sl_pct), tick, "S")
        new_tp    = align(orig_b_px * (1 - tp_pct), tick, "L")
    else:          # A是SHORT，SL在上方；補B（LONG）觸發在A靜態SL下方
        orig_b_px = align(a_static_sl * (1 - back_offset_pct), tick, "L")
        new_sl    = align(orig_b_px * (1 - sl_pct), tick, "L")
        new_tp    = align(orig_b_px * (1 + tp_pct), tick, "S")
    open_fee = await _query_open_fee(iid, b_side, float(S.get("back_ee") or 0))
    _bnm = "B\u88dc\u55ae" if S.get("pair_state") == "A_IN_B_REFILL" else "B\u55ae"
    await _notify_exit(app, chat, S, _bnm, rec, open_fee, "Stop Loss", extra=f"補B觸發委託，觸發價：{orig_b_px}")
    _, _algos_b = await list_all_orders(iid)       # 涵蓋 trigger + oco
    b_algos = [o for o in _algos_b if o.get("posSide") == b_side]
    if b_algos:
        await api("POST", "/api/v5/trade/cancel-algos",
                  [{"instId": iid, "algoId": o["algoId"]} for o in b_algos])
        await asyncio.sleep(0.3)
    sz = Decimal(str(S.get("back_sz", "1")))
    new_algo = await _place_trigger(iid, b_side, back_d, orig_b_px, sz, new_tp, new_sl)
    if new_algo:
        S["back_algo_id"] = new_algo
        S["back_filled"] = False
        S["back_ee"] = None
        S["back_sl_px"] = None
        S["back_algo2_id"] = None
        S["back_static_sl"] = str(new_sl)
        S["back_tp_px"] = str(new_tp)
        cur_ps = S.get("pair_state")
        if cur_ps == "AB_in":
            S["pair_state"] = "A_IN_B_REFILL"
        save_state()
        print(f"[\u88dcB] {S['sym']} {back_d} \u89f8\u767c\u50f9={orig_b_px}")
    else:
        await notify(app, chat, f"{E.BOT} {E.LOSS} \u88dcB\u89f8\u767c\u59d4\u8098\u5931\u6557\uff0c\u8acb\u624b\u52d5\u8655\u7406\n\u6642\u9593\uff1a{hhmmss()}")


async def loop(app, chat, S):
    # 設計此腳本的目的在於用bot取代我在交易所app上的一切手動行為，切記
    # Loop 只監控進出場成交訊號（持倉），不監控掛單
    spec = S["spec"]
    iid  = spec["iid"]
    k    = skey(S["sym"], S["dir"])

    try:
        ok = await _place_pair(S, iid, chat, app, label="\u9996\u6b21\u57cb\u4f0f")
        if not ok:
            S["pair_state"] = "idle"
            return

        while True:
            await asyncio.sleep(1)
            ps = S.get("pair_state", "idle")
            if ps == "idle":
                break

            d      = S["dir"]
            a_side = "long" if d == "L" else "short"
            back_d = S.get("back_d", "S" if d == "L" else "L")
            b_side = "long" if back_d == "L" else "short"
            cur_a  = await okx_pos(iid, a_side)
            cur_b  = await okx_pos(iid, b_side) if ps != "waiting" else None

            if ps == "waiting":
                tf_sec    = TF_SEC.get(S.get("tf", ACCOUNT_TF), 300)
                secs_left = (int(time.time() // tf_sec) + 1) * tf_sec - time.time()
                if secs_left <= 1.5:
                    await _pre_clear_orders(iid)
                    if S.get("pair_state") != "idle":
                        print(f"[TF\u91cd\u639b] {S['sym']} {d}")
                        await _place_pair(S, iid, chat, app, label="TF\u91cd\u639b")
                    continue
                if cur_a:
                    fpx = Decimal(str(cur_a.get("avgPx") or cur_a.get("last") or S.get("front_px","0")))
                    S["front_filled"] = True
                    S["front_px"]     = str(fpx)
                    S["front_ee"]     = time.time()
                    S["front_sl_px"]  = str(S.get("front_static_sl", fpx))
                    S["pair_state"]   = "A_in"
                    bump(skey(S["sym"], d), "entered")
                    save_state()
                    algo_id = await place_algo(iid, a_side, d,
                                               Decimal(str(S["front_sz"])),
                                               Decimal(str(S["front_tp_px"])),
                                               Decimal(str(S["front_static_sl"])))
                    if algo_id:
                        S["algo_id"] = algo_id
                    save_state()
                    print(f"[A\u9032\u5834] {S['sym']} {d} {fpx}")
                    _otp, _osl = await okx_tpsl(iid, a_side)          # 以OKX為主
                    _btrig_o, _ = await okx_tpsl(iid, b_side)
                    _btrig = S.get("back_px")
                    await notify(app, chat,
                        f"{E.BOT} OKX\u539f\u004b\uff5c{ACCT}\n\u4e8b\u4ef6\uff1a{E.ENTRY} A\u55ae\u9650\u50f9\u9032\u5834\u6210\u4ea4\n"
                        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                        f"\u5546\u54c1\uff1a{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                        f"\u9032\u5834\uff1a{fpx} | {hhmmss()}\n"
                        f"\u975c\u614bTP\uff1a{_otp or '-'}\uff08+{S['tp']}%\uff09\n"
                        f"\u975c\u614bSL\uff1a{_osl or '-'}\uff08-{S['sl']}%\uff09\n"
                        f"\u52d5\u614bSL\uff1a{S['interval']}s\uff5c{S['move_pct']}%\n"
                        f"B\u55ae\u89f8\u767c\uff1a{_btrig or '-'}\n\u6642\u9593\uff1a{hhmmss()}")

            elif ps in ("A_in", "A_IN_B_REFILL"):
                if ps in ("A_in", "A_IN_B_REFILL") and cur_b and not S.get("back_filled"):
                    bpx = Decimal(str(cur_b.get("avgPx") or cur_b.get("last") or S.get("back_px","0")))
                    S["back_filled"]  = True
                    S["back_px"]      = str(bpx)
                    S["back_ee"]      = time.time()
                    S["back_sl_px"]   = str(S.get("back_static_sl", bpx))
                    S["back_algo_id"] = None
                    S["pair_state"]   = "AB_in"
                    ba2 = await place_algo(iid, b_side, back_d,
                                           Decimal(str(S["back_sz"])),
                                           Decimal(str(S["back_tp_px"])),
                                           Decimal(str(S["back_static_sl"])))
                    if ba2:
                        S["back_algo2_id"] = ba2
                    save_state()
                    print(f"[B\u9032\u5834] {S['sym']} {back_d} {bpx}")
                    _bname = "B\u88dc\u55ae" if ps == "A_IN_B_REFILL" else "B\u55ae"
                    _otp, _osl = await okx_tpsl(iid, b_side)          # 以OKX為主
                    await notify(app, chat,
                        f"{E.BOT} OKX\u539f\u004b\uff5c{ACCT}\n\u4e8b\u4ef6\uff1a{E.ENTRY} {_bname}\u89f8\u767c\u9032\u5834\u6210\u4ea4\n"
                        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                        f"\u5546\u54c1\uff1a{E.dir_emoji(back_d)} {S['sym']} {E.dir_word(back_d)}\n"
                        f"\u9032\u5834\uff1a{bpx} | {hhmmss()}\n"
                        f"\u975c\u614bTP\uff1a{_otp or '-'}\uff08+{S['tp']}%\uff09\n"
                        f"\u975c\u614bSL\uff1a{_osl or '-'}\uff08-{S['sl']}%\uff09\n\u6642\u9593\uff1a{hhmmss()}")
                if not cur_a:
                    after_ms = int(float(S.get("front_ee", time.time())) * 1000)
                    pnl, rec = await _get_net_pnl(iid, a_side, after_ms)
                    print(f"[A\u51fa\u5834] {S['sym']} \u6de8\u640d\u76ca={pnl}")
                    if pnl > 0:
                        await _handle_win(S, iid, chat, app, "A", pnl, rec, b_side, S.get("back_filled", False))
                    else:
                        await _handle_lose_a(S, iid, chat, app, rec, pnl)
                    continue

            elif ps == "AB_in":
                # 【SOP一】兩個位置各自獨立判斷：空了就查該單損益。
                # 只對真的出場的位置查損益 —— 沒出場的位置不參與任何判斷（不塞假的0）。
                a_gone = not cur_a and S.get("front_filled")
                b_gone = not cur_b and S.get("back_filled")
                if a_gone or b_gone:
                    a_pnl = a_rec = b_pnl = b_rec = None
                    if a_gone:
                        after_a = int(float(S.get("front_ee", time.time())) * 1000)
                        a_pnl, a_rec = await _get_net_pnl(iid, a_side, after_a)
                    if b_gone:
                        after_b = int(float(S.get("back_ee", time.time())) * 1000)
                        b_pnl, b_rec = await _get_net_pnl(iid, b_side, after_b)
                    # 【SOP二 步驟1】結束條件只有一個：任一單淨損益>0。不比較誰賺得多。
                    if a_pnl is not None and a_pnl > 0:
                        await _handle_win(S, iid, chat, app, "A", a_pnl, a_rec, b_side, not b_gone)
                    elif b_pnl is not None and b_pnl > 0:
                        await _handle_win(S, iid, chat, app, "B", b_pnl, b_rec, a_side, not a_gone)
                    else:
                        # 戰役繼續：哪個位置空了就補回該位置（A空補A、B空補B）
                        if a_gone:
                            await _handle_lose_a(S, iid, chat, app, a_rec, a_pnl)
                        if b_gone and S.get("alive", True):
                            await _handle_lose_b(S, iid, chat, app, b_rec, b_pnl)

            elif ps == "A_REFILL_B_IN":
                if cur_a and not S.get("front_filled"):
                    fpx = Decimal(str(cur_a.get("avgPx") or cur_a.get("last") or S.get("front_px","0")))
                    S["front_filled"] = True
                    S["front_px"]     = str(fpx)
                    S["front_ee"]     = time.time()
                    S["front_sl_px"]  = str(S.get("front_static_sl", fpx))
                    S["pair_state"]   = "AB_in"
                    algo_id = await place_algo(iid, a_side, d,
                                               Decimal(str(S["front_sz"])),
                                               Decimal(str(S["front_tp_px"])),
                                               Decimal(str(S["front_static_sl"])))
                    if algo_id:
                        S["algo_id"] = algo_id
                    save_state()
                    print(f"[A\u88dc\u9032\u5834] {S['sym']} {d} {fpx}")
                    _otp, _osl = await okx_tpsl(iid, a_side)          # 以OKX為主
                    await notify(app, chat,
                        f"{E.BOT} OKX\u539f\u004b\uff5c{ACCT}\n\u4e8b\u4ef6\uff1a{E.ENTRY} A\u88dc\u55ae\u89f8\u767c\u9032\u5834\u6210\u4ea4\n"
                        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                        f"\u5546\u54c1\uff1a{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                        f"\u9032\u5834\uff1a{fpx} | {hhmmss()}\n"
                        f"\u975c\u614bTP\uff1a{_otp or '-'}\uff08+{S['tp']}%\uff09\n"
                        f"\u975c\u614bSL\uff1a{_osl or '-'}\uff08-{S['sl']}%\uff09\n"
                        f"\u52d5\u614bSL\uff1a{S['interval']}s\uff5c{S['move_pct']}%\n"
                        f"\u6642\u9593\uff1a{hhmmss()}")
                    continue
                if not cur_b and S.get("back_filled"):
                    after_b = int(float(S.get("back_ee", time.time())) * 1000)
                    b_pnl, b_rec = await _get_net_pnl(iid, b_side, after_b)
                    if b_pnl > 0:
                        await _handle_win(S, iid, chat, app, "B", b_pnl, b_rec, a_side, False)
                    else:
                        await _handle_lose_b(S, iid, chat, app, b_rec, b_pnl)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("loop error", S.get("sym"), S.get("dir"), type(e).__name__, e)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} \u7b56\u7565\u932f\u8aa4\uff1a{type(e).__name__}: {e}")
    finally:
        if not SHUTTING_DOWN:
            S["pair_state"] = "idle"
            S["state"] = "\u5df2\u505c\u6b62"
            if STRATS.get(k) is S:
                STRATS.pop(k, None)
            try:
                if TASKS.get(k) is asyncio.current_task():
                    TASKS.pop(k, None)
            except Exception:
                pass
            save_state()

async def rebuild_strat(d):
    spec = await get_spec(d["sym"])
    S = {"sym": d["sym"], "dir": d["dir"],
         "lev": int(d["lev"]), "margin": Decimal(str(d["margin"])),
         "offset": Decimal(str(d["offset"])), "back_offset": Decimal(str(d.get("back_offset", d["offset"]))),
         "tp": Decimal(str(d["tp"])),
         "sl": Decimal(str(d["sl"])),
         "move_pct": Decimal(str(d.get("move_pct", "0"))),
         "interval": float(d.get("interval", 1)),
         "spec": spec,
         "alive": True, "state": d.get("state", "委託中"),
         "pair_state": d.get("pair_state", "waiting"),
         "chat": d.get("chat", CHAT_ID),
         "locked_dir": d.get("locked_dir", d["dir"])}
    for a in ("front_oid","front_px","front_static_sl","front_tp_px","front_sl_px",
              "front_filled","front_ee","front_sz","front_move_n","front_move_hist",
              "back_algo_id","back_px","back_filled","back_sz","closing"):
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
                         "\n".join("・" + x for x in failed) + "\n{E.WARN} 這些策略已消失，請確認 OKX 是否有殘留掛單")
    if CHAT_ID and rec and n_ord > len(rec):
        await notify(app, CHAT_ID, f"{E.BOT} {E.LOSS} OKX 掛單 {n_ord} 筆 > 策略 {len(rec)} 個，可能有孤兒單，請查 /status")
    if CHAT_ID and rec:
        await notify(app, CHAT_ID,
            f"{E.BOT} OKX原K｜{ACCT}\n事件：{E.RELOAD} 重啟認領完成\n━━━━━━━━━━\n"
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
    if not S or S.get("pair_state","idle") == "idle":
        return f"{dr}（已停止）"
    return (f"{dr} {S['lev']}x {pct(S['margin'])} {pct(S['offset'])} "
            f"{pct(S['tp'])} {pct(S['sl'])} {pct(S.get('move_pct',0))}% {S.get('interval',0)}s")

async def cmd_run(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args
    fmt = (f"{E.BOT} 用法：/run 商品 方向 槓桿 保證金 A單埋伏% B距A_SL% TP% SL% 移動門檻% 間隔秒\n"
           f"例：/run ETHUSDT L 1x 3 0.5 0.1 1.5 0.2 0.002 1\n"
           f"B距A_SL% = B觸發點距A靜態SL的距離\n"
           f"共10個參數，方向只能 L 或 S")
    if len(a) != 10:
        await reply(u, f"{E.BOT} 參數數量錯誤（需10個）\n{fmt}"); return
    try:
        sym = a[0].upper(); dr = a[1].upper(); lev = int(a[2].replace("x", ""))
        margin = Decimal(a[3]); offset = Decimal(a[4]); back_offset = Decimal(a[5])
        tp = Decimal(a[6].rstrip("%")); sl = Decimal(a[7].rstrip("%"))
        move_pct = Decimal(a[8].rstrip("%")); interval = float(a[9])
    except Exception:
        await reply(u, f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if dr not in ("L", "S"):
        await reply(u, f"{E.BOT} 方向須 L 或 S"); return
    for nm, v in (("移動門檻", move_pct), ("TP", tp), ("SL", sl)):
        if v < 0:
            await reply(u, f"{E.BOT} {nm} 不可為負數"); return
    if not 0.1 <= interval <= 300:
        await reply(u, f"{E.BOT} 間隔秒須介於 0.1~300（支援小數一位，例如 0.5、1.5）"); return

    # 方向鎖定檢查：同幣種不能同時做 L 又做 S
    k = skey(sym, dr)
    if k in STRATS and STRATS[k].get("pair_state","idle") != "idle":
        await reply(u, f"{E.BOT} {sym} {E.dir_word(dr)} 已在運行"); return
    # 同幣種反向也不允許
    k_rev = skey(sym, "S" if dr == "L" else "L")
    if k_rev in STRATS and STRATS[k_rev].get("pair_state","idle") != "idle":
        await reply(u, f"{E.BOT} {E.LOSS} {sym} 已有反向策略在運行，請先 /stop 再重新下單"); return

    try:
        spec = await get_spec(sym)
    except Exception:
        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return

    op = await get_last(spec["iid"])
    tick = spec["tick"]
    # 前單埋伏價
    front_amb = align(op * (1 - offset / 100) if dr == "L"
                      else op * (1 + offset / 100), tick, dr)
    # 前單靜態 SL / TP
    back_dr = "S" if dr == "L" else "L"
    if dr == "L":
        front_static_sl = align(front_amb * (1 - sl / 100), tick, "L")
        front_tp = align(front_amb * (1 + tp / 100), tick, "S")
    else:
        front_static_sl = align(front_amb * (1 + sl / 100), tick, "S")
        front_tp = align(front_amb * (1 - tp / 100), tick, "L")
    # B單觸發價 = A靜態SL ± back_offset%（距A的SL保持固定距離）
    if dr == "S":   # A是SHORT，SL在上方；B是LONG，觸發在SL下方
        back_amb = align(front_static_sl * (1 - back_offset / 100), tick, back_dr)
    else:           # A是LONG，SL在下方；B是SHORT，觸發在SL上方
        back_amb = align(front_static_sl * (1 + back_offset / 100), tick, back_dr)
    # 後單靜態 SL / TP（以後單埋伏價為基準）
    if back_dr == "S":
        back_static_sl = align(back_amb * (1 + sl / 100), tick, "S")
        back_tp = align(back_amb * (1 - tp / 100), tick, "L")
    else:
        back_static_sl = align(back_amb * (1 - sl / 100), tick, "L")
        back_tp = align(back_amb * (1 + tp / 100), tick, "S")

    sz_front = csize(margin, Decimal(lev), front_amb, spec["ctval"], spec["lot"])
    sz_back  = csize(margin, Decimal(lev), back_amb,  spec["ctval"], spec["lot"])

    # 參數檢測：back_offset必須大於0
    if back_offset <= 0:
        await reply(u, f"{E.BOT} {E.LOSS} 參數錯誤：B單距離%必須大於0"); return
    if sz_front < spec["minsz"]:
        need = spec["minsz"] * spec["ctval"] * op / Decimal(lev)
        await reply(u, f"{E.BOT} {E.LOSS} 保證金不足：前單算出 {sz_front} 張 < 最小 {spec['minsz']}\n至少需 {need:.4f} USDT"); return

    PENDING[u.effective_chat.id] = {
        "kind": "run", "t": time.time(),
        "sym": sym, "dir": dr, "lev": lev, "margin": margin,
        "offset": offset, "back_offset": back_offset, "tp": tp, "sl": sl,
        "move_pct": move_pct, "interval": interval, "spec": spec,
        "front_amb": front_amb, "front_static_sl": front_static_sl,
        "front_tp": front_tp, "front_sz": sz_front,
        "back_dr": back_dr, "back_amb": back_amb,
        "back_static_sl": back_static_sl, "back_tp": back_tp,
        "back_sz": sz_back, "locked_dir": dr,
    }
    await reply(u, f"{E.BOT} OKX原K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
        f"商品：{E.dir_emoji(dr)} {sym} {E.dir_word(dr)} {lev}x {margin}\n"
        f"現價：{op}｜{hhmmss()}\n"
        f"━━━━━━━━━━\n"
        f"前單：{sym} {E.dir_word(dr)}\n"
        f"埋伏價 ：{front_amb}（距現價{offset}%）\n"
        f"靜態TP：{front_tp}（+{tp}%）\n"
        f"靜態SL：{front_static_sl}（-{sl}%）\n"
        f"\n"
        f"後單：{sym} {E.dir_word(back_dr)}（觸發進場）\n"
        f"埋伏價 ：{back_amb}（距A靜態SL {back_offset}%）\n"
        f"靜態TP：{back_tp}（+{tp}%）\n"
        f"靜態SL：{back_static_sl}（-{sl}%）\n"
        f"━━━━━━━━━━\n"
        f"移動SL：每{interval}s｜門檻{move_pct}%\n"
        f"{E.WARN} 確認後立即開始埋伏\n下一步：60秒內 /confirm\n時間：{hhmmss()}")
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
        await do_stop(u, p["sym"], p["iid"]); return
    if kind == "stopall":
        del PENDING[u.effective_chat.id]
        await do_stopall(u); return
    del PENDING[u.effective_chat.id]
    k = skey(p["sym"], p["dir"])
    old_t = TASKS.get(k)
    if old_t and not old_t.done():
        old_s = STRATS.get(k)
        if old_s: old_s["pair_state"] = "idle"
        old_t.cancel()
    S = {**p, "alive": True, "state": "委託中", "chat": u.effective_chat.id,
         "locked_dir": p.get("locked_dir", p["dir"])}
    STRATS[k] = S
    TASKS[k] = asyncio.create_task(loop(c.application, u.effective_chat.id, S))
    save_state()
    cnt = sum(1 for t in TASKS.values() if t and not t.done())
    await reply(u, f"{E.BOT} {E.OK} 已確認，{p['sym']} {E.dir_word(p['dir'])} 啟動\n運行中策略：{cnt} 個")

# 撤單部分 stopall / stop：一切以查詢交易所為主，DB 只是確認後的資料回補而已
async def cmd_stop(u, c):
    a = c.args
    if not a:
        await reply(u, f"{E.BOT} 用法：/stop ETHUSDT"); return
    sym = a[0].upper()
    try:
        spec = await get_spec(sym)
    except Exception:
        await reply(u, f"{E.BOT} 找不到商品 {sym}"); return
    iid = spec["iid"]
    PENDING[u.effective_chat.id] = {"kind": "stop", "t": time.time(), "sym": sym, "iid": iid}
    await reply(u, f"{E.BOT} 將停止 {sym} 所有掛單\n60秒內 /confirm 確認")
    asyncio.create_task(_to(c.application, u.effective_chat.id, PENDING[u.effective_chat.id]["t"]))

async def do_stop(u, sym, iid):
    # 步驟1：先設 pair_state=idle，讓 loop 立刻停下來
    for k, S in STRATS.items():
        if S.get("sym") == sym:
            S["pair_state"] = "idle"
            S["alive"] = False       # 讓等待清空/等TF 的迴圈立刻退出

    # 步驟2：批次撤掉該幣種所有掛單，直到掛單數=0
    for attempt in range(5):
        r1 = await api("GET", f"/api/v5/trade/orders-pending?instId={iid}")
        orders = r1.get("data") or []
        _, algos = await list_all_orders(iid)      # 涵蓋 trigger + oco

        if not orders and not algos:
            break

        if orders:
            batch = [{"instId": o["instId"], "ordId": o["ordId"]} for o in orders]
            for i in range(0, len(batch), 20):
                await api("POST", "/api/v5/trade/cancel-batch-orders", batch[i:i+20])

        if algos:
            algo_batch = [{"instId": o["instId"], "algoId": o["algoId"]} for o in algos]
            for i in range(0, len(algo_batch), 20):
                await api("POST", "/api/v5/trade/cancel-algos", algo_batch[i:i+20])

        await asyncio.sleep(0.5)

    # 步驟3：查該幣種持倉
    pr = await api("GET", f"/api/v5/account/positions?instId={iid}")
    positions = [p for p in (pr.get("data") or []) if float(p.get("pos") or 0) != 0]

    # 步驟4：回填 DB
    save_state()

    # 步驟5：TG 回報
    r_chk = await api("GET", f"/api/v5/trade/orders-pending?instId={iid}")
    final_pending = len(r_chk.get("data") or [])
    pos_count = len(positions)
    msg = f"{E.BOT} 已停止 {sym}\n掛單數：{final_pending}｜持倉數：{pos_count}"
    if positions:
        msg += "\n━━━━━━━━━━\n持倉清單（請手動平倉）："
        for i, p in enumerate(positions, 1):
            msg += f"\n{i}. {p.get('instId','')} {p.get('posSide','')}"
    await reply(u, msg)

async def cmd_stopall(u, c):
    alive = [k for k, s in STRATS.items() if s.get("pair_state","idle") != "idle"]
    if not alive:
        await reply(u, f"{E.BOT} 目前無運行中策略"); return
    PENDING[u.effective_chat.id] = {"kind": "stopall", "t": time.time()}
    await reply(u, f"{E.BOT} {E.WARN} 將停止全部 {len(alive)} 個策略\n60秒內 /confirm 確認")
    asyncio.create_task(_to(c.application, u.effective_chat.id, PENDING[u.effective_chat.id]["t"]))

async def do_stopall(u):
    # 步驟1：先設所有策略 pair_state=idle，讓所有 loop 立刻停下來
    for S in STRATS.values():
        S["pair_state"] = "idle"
        S["alive"] = False           # 讓等待清空/等TF 的迴圈立刻退出

    # 步驟2：批次撤掉所有掛單，直到掛單數=0
    for attempt in range(5):
        # 查所有限價掛單
        r1 = await api("GET", "/api/v5/trade/orders-pending")
        orders = r1.get("data") or []
        # 查所有 trigger 計劃委託
        _, algos = await list_all_orders()         # 涵蓋 trigger + oco

        if not orders and not algos:
            break  # 掛單全部清空，離開迴圈

        # 批次撤限價掛單（每次最多20筆）
        if orders:
            batch = [{"instId": o["instId"], "ordId": o["ordId"]} for o in orders]
            for i in range(0, len(batch), 20):
                await api("POST", "/api/v5/trade/cancel-batch-orders", batch[i:i+20])

        # 批次撤 trigger 計劃委託
        if algos:
            algo_batch = [{"instId": o["instId"], "algoId": o["algoId"]} for o in algos]
            for i in range(0, len(algo_batch), 20):
                await api("POST", "/api/v5/trade/cancel-algos", algo_batch[i:i+20])

        await asyncio.sleep(0.5)  # 等OKX確認後再查

    # 步驟3：查 OKX 持倉
    pr = await api("GET", "/api/v5/account/positions")
    positions = [p for p in (pr.get("data") or []) if float(p.get("pos") or 0) != 0]

    # 步驟4：回填 DB
    save_state()

    # 步驟4：TG 回報
    pos_count = len(positions)
    r_msg = await api("GET", "/api/v5/trade/orders-pending")
    final_pending = len(r_msg.get("data") or [])
    msg = f"{E.BOT} 已執行 /stopall\n掛單數：{final_pending}｜持倉數：{pos_count}"
    if positions:
        msg += "\n━━━━━━━━━━\n持倉清單（請手動平倉）："
        for i, p in enumerate(positions, 1):
            msg += f"\n{i}. {p.get('instId','')} {p.get('posSide','')}"
    await reply(u, msg)

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
    alive = [s for s in STRATS.values() if s.get("pair_state", "idle") != "idle"]
    L = [f"{E.BOT} OKX原K｜{ACCT}", "事件：現況（即時查OKX）",
         f"USDT權益：{eq}", f"可用餘額：{av}", f"帳戶週期：{ACCOUNT_TF}",
         f"運行中策略：{len(alive)}個"]
    # 查計劃委託數（trigger algo）
    _, algo_list = await list_all_orders()         # 涵蓋 trigger + oco，否則數字不準
    total_pending = len(pdl) + len(algo_list)

    for i, s in enumerate(alive):
        d = s["dir"]
        front_waiting  = 1 if s.get("front_oid") and not s.get("front_filled") else 0
        back_waiting   = 1 if s.get("back_algo_id") and not s.get("back_filled") else 0
        front_in       = 1 if s.get("front_filled") else 0
        back_in        = 1 if s.get("back_filled") else 0
        state_str = f"前{front_waiting}/後{back_waiting}/前進{front_in}/後進{back_in}"
        live_label = "持倉中" if (front_in or back_in) else "委託中"
        live_emoji = E.HOLD if (front_in or back_in) else E.dir_emoji(d)
        round_t = int(s.get("round_today", 0))
        enter_t = int(s.get("enter_today", 0))
        lev = s.get("lev", "?")
        margin = s.get("margin", "?")

        if i == 0:
            L.append("━━━━━━━━━━")
        else:
            L.append("")
        L.append(f"{live_emoji} {s['sym']} {E.dir_word(d)} {lev}x {margin}（輪{round_t}｜進{enter_t}）")
        L.append(f"{live_label}({state_str})")

        iid_s = s["spec"]["iid"]
        # 燈號：這根 TF K 棒的漲跌（現價 vs 當根開盤價，與策略方向無關）
        try:
            cur_px_s = await get_last(iid_s)
            bar_s = NATIVE_BARS.get(ACCOUNT_TF, "5m")
            kr = await pub(f"/api/v5/market/candles?instId={iid_s}&bar={bar_s}&limit=1")
            open_px_s = Decimal(kr["data"][0][1]) if kr.get("code") == "0" and kr.get("data") else None
            if open_px_s and cur_px_s:
                if cur_px_s > open_px_s:
                    px_emoji = E.KLINE_UP
                elif cur_px_s < open_px_s:
                    px_emoji = E.KLINE_DOWN
                else:
                    px_emoji = E.EVEN
            else:
                px_emoji = "⚪"
            L.append(f"現：{hhmmss()}|{px_emoji} {cur_px_s}")
        except Exception:
            L.append(f"現：{hhmmss()}|⚪ -")

        # 前單資訊（TP/SL 以OKX實際掛單為主）
        _fps = "long" if d == "L" else "short"
        _bps = "short" if d == "L" else "long"
        _tp_o, _sl_o = await okx_tpsl(iid_s, _fps)
        tp_f  = _tp_o or s.get("front_tp_px", "-")
        sl_f  = _sl_o or s.get("front_static_sl", "-")
        amb_f = s.get("front_px", "-")
        if front_waiting:
            L.append(f"前：{tp_f}｜📍{amb_f}｜{sl_f}")
        elif front_in:
            sl_d = s.get("front_sl_px", "-")
            mn   = s.get("front_move_n", 0)
            L.append(f"前：{tp_f}｜📍{amb_f}｜動態SL:{sl_d}（{mn}次）")
            mhist = s.get("front_move_hist") or []
            prev_px = None
            for mrec in mhist:
                cur_px = mrec.get("px", "")
                arrow = E.price_emoji(cur_px, prev_px) if prev_px else "🔸"
                L.append(f"  {mrec.get('t','')} | {arrow} | {cur_px} | 止{mrec.get('sl','')}")
                prev_px = cur_px

        # 後單資訊（TP/SL 以OKX實際掛單為主）
        _tp_ob, _sl_ob = await okx_tpsl(iid_s, _bps)
        tp_b  = _tp_ob or s.get("back_tp_px", "-")
        sl_b  = _sl_ob or s.get("back_static_sl", "-")
        trig_b = s.get("back_px", "-")
        if back_waiting:
            L.append(f"後：{tp_b}｜📍{trig_b}｜{sl_b}")
        elif back_in:
            sl_d = s.get("back_sl_px", "-")      # 修正：後單應讀 back 欄位
            mn   = s.get("back_move_n", 0)
            L.append(f"後升格：{tp_b}｜📍{trig_b}｜動態SL:{sl_d}（{mn}次）")

    L.append("━━━━━━━━━━")
    L.append(f"掛單數：{total_pending}｜持倉數：{len(pl)}")
    L.append(f"時間：{hhmmss()} UTC+8")
    await reply(u, "\n".join(L))

# ---------- /summary ----------
def sum_lines(rs, placed, entered):
    L = []
    m = len(rs)
    hit = (entered / placed * 100) if placed else 0
    amb = ("%d秒" % (sum(int(r.get("ambush_s") or 0) for r in rs) / m)) if m else "-"
    L.append("次數:%d|%d(%s)|%.2f%%" % (placed, entered, amb, hit))
    NAME = {"Take_Profit": "TP", "Stop_Loss": "SL", "Frame_Exit": "SL", "Manual": "手動"}
    for lab, cats in (("獲利", ("Take_Profit", "Stop_Loss", "Manual")), ("虧損", ("Stop_Loss", "Take_Profit", "Frame_Exit", "Manual"))):
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
        L.append("%s數:%d|%s" % (lab, len(sub), "|".join(ps)))
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
    L = [f"{E.BOT} OKX原K｜{ACCT}", f"{E.CHART}{E.CHART}{E.CHART} Summary {t}"]
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
AMP_MAX = 110000     # 單次最多抓幾根（支援整年 5m ≈ 105,120 根）
AMP_YEAR_BARS = 12 * 24 * 365  # 整年 5m 根數 = 105,120
AMP_BINS = [Decimal(str(x)) for x in
            ("0.1","0.2","0.3","0.4","0.5","0.6","0.7","0.8","0.9","1.0","1.2","1.5",
             "2.0","2.5","3.0","3.5","4.0","5.0")]

AMP_THRESHOLDS = [
    ("≥ 0.1%", 0.001), ("≥ 0.2%", 0.002), ("≥ 0.3%", 0.003),
    ("≥ 0.4%", 0.004), ("≥ 0.5%", 0.005), ("≥ 0.6%", 0.006),
    ("≥ 0.7%", 0.007), ("≥ 0.8%", 0.008), ("≥ 0.9%", 0.009),
    ("≥ 1.0%", 0.01),  ("≥ 1.2%", 0.012), ("≥ 1.5%", 0.015),
    ("≥ 2.0%", 0.02),  ("≥ 2.5%", 0.025), ("≥ 3.0%", 0.03),
    ("≥ 3.5%", 0.035), ("≥ 4.0%", 0.04),  ("≥ 5.0%", 0.05),
]


async def amp_fetch_page(iid, after, ep):
    """抓一頁 K 線（新->舊，只含已收線）。回傳 (list, 下一個ep, 是否到盡頭)。"""
    q = f"/api/v5/market/{ep}?instId={iid}&bar=5m&limit=300"
    if after:
        q += f"&after={after}"
    r = await pub(q)
    batch = r.get("data") or []
    if r.get("code") != "0" or not batch:
        if ep == "candles" and after:
            return [], "history-candles", False      # candles 僅近期，改歷史端點續抓
        return [], ep, True                          # OKX 沒有更早資料（多半是該幣上市日）
    out = []
    for c in batch:
        try:
            if len(c) >= 9 and str(c[8]) != "1":
                continue
            out.append({"ts": int(c[0]), "o": Decimal(c[1]), "h": Decimal(c[2]),
                        "l": Decimal(c[3]), "c": Decimal(c[4])})
        except Exception:
            continue
    if not out:
        return [], ep, True
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out, ep, False


async def amp_stream_build(sym, iid, tick, years, path, notify_cb=None):
    """【全程串流】邊抓邊算邊寫：記憶體只留當前一頁(300根)+一個跨頁接縫值。
    OKX 回傳新->舊，直接照此序寫 Excel（最新在上），省去暫存檔與二次讀寫。
    振幅/漲跌幅需要「前一根收盤」=同頁的下一根；跨頁時以 pending 暫存一根等下頁補算。
    回傳 dict: rows / newest_ts / oldest_ts / want_days / short（資料不足）。
    """
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, Alignment, PatternFill, Color
    from openpyxl.utils import get_column_letter

    FONT = "蘋方-繁 標準體"
    TINT = 0.7999816888943144
    f_blue = PatternFill("solid", fgColor=Color(theme=6, tint=TINT, type="theme"))
    f_orng = PatternFill("solid", fgColor=Color(theme=9, tint=TINT, type="theme"))
    f_grn  = PatternFill("solid", fgColor=Color(theme=6, tint=0.6, type="theme"))
    f_red  = PatternFill("solid", fgColor=Color(theme=9, tint=0.6, type="theme"))
    hdr = Font(name=FONT, bold=True, size=11)
    dat = Font(name=FONT, size=11)
    lft = Alignment(horizontal="left")
    cen = Alignment(horizontal="center")

    dp = max(0, -tick.as_tuple().exponent)
    pfmt = "0" if dp == 0 else "0." + "0" * dp
    P4, P4N, P2 = "0.0000%", "0.0000%;[Red]\\-0.0000%", "0.00%"

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=sym)
    for ci, w in {1: 4.4, 2: 12.8, 3: 6.8, 4: 16.8, 5: 11.4, 6: 6.8, 7: 11.2,
                  11: 13.6, 12: 12.2, 13: 13.0, 14: 9.4, 15: 12.2, 16: 9.0,
                  17: 12.2, 18: 3.0, 19: 9.4, 20: 12.2, 21: 9.0, 22: 12.2,
                  23: 3.0, 24: 16.0, 25: 6.8, 26: 13.0, 27: 3.0, 28: 16.0,
                  29: 6.8, 30: 13.0, 31: 3.0, 32: 16.0, 33: 6.8, 34: 13.0,
                  35: 3.0, 36: 16.0, 37: 6.8, 38: 13.0}.items():
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.freeze_panes = "B3"

    def C(v, font=dat, fmt=None, align=None, fill=None):
        c = WriteOnlyCell(ws, value=v)
        c.font = font
        if fmt:   c.number_format = fmt
        if align: c.alignment = align
        if fill:  c.fill = fill
        return c

    def blank_row():
        return [None] * 37

    # row1：分析表總數（串流無法回頭改，故用整欄 COUNT）
    r1 = blank_row()
    for col, f in ((24, "=COUNT($O:$O)"), (28, "=COUNT($Q:$Q)"),
                   (32, "=COUNT($S:$S)"), (36, "=COUNT($U:$U)")):
        r1[col - 1] = C(f, font=hdr)
    ws.append(r1)

    # row2：主欄標頭 + 分析表標頭
    heads = ["幣種", "週期", "日期", "時間", "漲跌", "開", "高", "低", "收",
             "漲跌幅%", "振幅%", "ABS(振幅%-漲跌幅%)", "開到高", "開到高%",
             "開到低", "開到低%", "收到高", "收到高%", "收到低", "收到低%"]
    fills = [f_blue] * 16 + [f_grn] * 4
    r2 = blank_row()
    for ci, (h, fl) in enumerate(zip(heads, fills), start=2):
        r2[ci - 1] = C(h, font=hdr, align=cen, fill=fl)
    for col, lb, fl in ((23, "開到高%門檻", f_blue), (24, "根數", f_blue), (25, "佔比", f_blue),
                        (27, "開到低%門檻", f_orng), (28, "根數", f_orng), (29, "佔比", f_orng),
                        (31, "收到高%門檻", f_grn),  (32, "根數", f_grn),  (33, "佔比", f_grn),
                        (35, "收到低%門檻", f_red),  (36, "根數", f_red),  (37, "佔比", f_red)):
        r2[col - 1] = C(lb, font=hdr, align=lft, fill=fl)
    ws.append(r2)

    want_days  = 365 * years
    horizon_ms = want_days * 86400 * 1000
    newest_ts = oldest_ts = None
    rows = 0
    pending = None
    after, ep = "", "candles"
    short = False

    for page in range(years * 500 + 100):
        page_k, ep, done = await amp_fetch_page(iid, after, ep)
        if done:
            short = True              # OKX 已無更早資料
            break
        if not page_k:
            continue

        if newest_ts is None:
            newest_ts = page_k[0]["ts"]

        work = ([pending] if pending else []) + page_k
        pending = work[-1]            # 本頁最舊一根，留待下一頁當它的前收
        stop = False

        for i in range(len(work) - 1):
            k = work[i]
            if (newest_ts - k["ts"]) > horizon_ms:
                stop = True           # 已達要求年限
                break
            prev_c = work[i + 1]["c"]
            ts_i = k["ts"]
            dt = datetime.fromtimestamp(ts_i / 1000, TZ8)
            o, h, lo, cl = float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"])
            pc = float(prev_c) or o
            chg = (cl - pc) / pc if pc else 0
            amp = (h - lo) / pc if pc else 0
            h2o, l2o = h - o, o - lo
            h2c, c2l = h - cl, cl - lo
            row = blank_row()
            vals = [sym, "5m", dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"),
                    (E.KLINE_UP if cl >= o else E.KLINE_DOWN),
                    o, h, lo, cl, chg, amp, abs(amp - chg),
                    round(h2o, dp), (h2o / o if o else 0),
                    round(l2o, dp), (l2o / o if o else 0),
                    round(h2c, dp), (h2c / cl if cl else 0),
                    round(c2l, dp), (c2l / cl if cl else 0)]
            fmts = [None, None, None, None, None,
                    pfmt, pfmt, pfmt, pfmt, P4N, P4, P4,
                    pfmt, P4, pfmt, P4, pfmt, P4, pfmt, P4]
            for ci, (v, fm) in enumerate(zip(vals, fmts), start=2):
                row[ci - 1] = C(v, fmt=fm)

            # 前 18 列資料同時帶出右側門檻分析表（串流只能一次寫完一列）
            if rows < len(AMP_THRESHOLDS):
                lb, dv = AMP_THRESHOLDS[rows]
                tr = rows + 3
                for lab_c, cnt_c, pct_c, src in ((23, 24, 25, "O"), (27, 28, 29, "Q"),
                                                 (31, 32, 33, "S"), (35, 36, 37, "U")):
                    row[lab_c - 1] = C(lb, align=lft)
                    row[cnt_c - 1] = C(f'=COUNTIF(${src}:${src},">={dv}")')
                    ltr = get_column_letter(cnt_c)
                    row[pct_c - 1] = C(f"={ltr}{tr}/{ltr}$1", fmt=P2)

            ws.append(row)
            oldest_ts = ts_i
            rows += 1

        if stop:
            break
        after = str(min(x["ts"] for x in page_k))
        if notify_cb and page and page % 60 == 0:
            await notify_cb(rows, oldest_ts)
        await asyncio.sleep(0.05)     # 同時讓出控制權，移動SL不受影響

    wb.save(path)
    return {"rows": rows, "newest_ts": newest_ts, "oldest_ts": oldest_ts,
            "want_days": want_days, "short": short}


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
    """原K 振幅分析報表（全程串流，記憶體友善）。
    用法：/amp <幣種> <往回年數 1~3>
    例：/amp BTCUSDT 1  → 從OKX最新一根往回抓 365 天
    以「往回 N 年」取代指定年份：各幣上市日不同，往回抓永遠從有資料處開始，
    抓不滿會明確回報實際天數與最早日期，不會卡住。TF 固定 5m。
    """
    fmt = (f"{E.BOT} 用法：/amp <幣種> <往回年數>\n"
           f"例：/amp BTCUSDT 1\n"
           f"年數只接受 1~3（1=365天, 2=730天, 3=1095天）\n"
           f"TF 固定 5m，產生 Excel 寄到信箱")
    if not c.args or len(c.args) != 2:
        await reply(u, fmt); return
    sym = c.args[0].upper()
    try:
        years = int(c.args[1])
        if years not in (1, 2, 3):
            raise ValueError
    except (ValueError, TypeError):
        await reply(u, f"{E.LOSS} 年數只接受 1、2、3\n{fmt}"); return

    days = 365 * years
    est  = 288 * days
    await reply(u, f"{E.BOT} 振幅分析報表中…\n"
                   f"幣種：{sym}｜TF：5m\n"
                   f"範圍：從OKX最新一根往回 {days} 天（約{est}根）\n"
                   f"全程串流寫入，不影響進行中的策略\n"
                   f"時間：{hhmmss()}")
    try:
        spec = await get_spec(sym)
    except Exception:
        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return

    day  = now8().strftime("%Y%m%d")
    name = f"OKX.{sym}.5m.{years}Y.{day}.xlsx"
    path = f"/srv/1111bot/data/{name}"

    async def _progress(rows, oldest_ts):
        if oldest_ts:
            od = datetime.fromtimestamp(oldest_ts / 1000, TZ8).strftime("%Y-%m-%d")
            print(f"[amp] {sym} 已寫 {rows} 根，最舊 {od}")

    try:
        info = await amp_stream_build(sym, spec["iid"], spec["tick"], years, path,
                                      notify_cb=_progress)
    except Exception as e:
        await reply(u, f"{E.LOSS} 產生失敗：{type(e).__name__}: {e}"); return

    rows = info["rows"]
    if not rows:
        await reply(u, f"{E.LOSS} {sym} 查無資料"); return

    nts, ots = info["newest_ts"], info["oldest_ts"]
    ndt = datetime.fromtimestamp(nts / 1000, TZ8).strftime("%Y-%m-%d") if nts else "-"
    odt = datetime.fromtimestamp(ots / 1000, TZ8).strftime("%Y-%m-%d") if ots else "-"
    got_days = int((nts - ots) / 86400000) + 1 if (nts and ots) else 0

    short_note = ""
    if info["short"] or got_days < days - 2:
        short_note = (f"\n{E.WARN} 資料不足：要求 {days} 天，實際 {got_days} 天\n"
                      f"OKX 最早只到 {odt}（多半是該幣上市日）")

    subject = f"OKX 振幅分析 {sym} 5m 往回{years}年（{rows}根）"
    body = (f"幣種：{sym}｜TF：5m\n"
            f"範圍：{odt} ~ {ndt}（{got_days} 天）\n"
            f"實際根數：{rows} 根\n"
            f"產生時間：{now8().strftime('%Y/%m/%d %H:%M:%S')}\n")
    try:
        ok, minfo = send_amp_mail(path, name, subject, body)
    except Exception as e:
        await reply(u, f"{E.LOSS} 寄送失敗：{type(e).__name__}: {e}\n"
                       f"檔案已存於 VPS：{name}{short_note}"); return
    if not ok:
        await reply(u, f"{E.LOSS} 未寄送：{minfo}\n檔案已存於 VPS：{name}{short_note}"); return
    await reply(u, f"{E.BOT} {E.OK} {sym} 振幅報表已寄出\n"
                   f"範圍：{odt} ~ {ndt}（{got_days} 天）\n"
                   f"根數：{rows}｜時間：{hhmmss()}{short_note}")

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
    await reply(u, f"{E.BOT} {E.OK} 帳戶週期已設為 {tf}\n（僅影響之後新建立的策略）")

async def cmd_menu(u, c):
    await reply(u, f"{E.BOT} OKX原K｜{ACCT}\n使用說明\n━━━━━━━━━━\n"
        "/run 商品 方向 槓桿 保證金 進場距離% TP% SL% 移動門檻% 間隔秒\n"
        f"例：/run ETHUSDT L 1x 3 0.5 0.5 0.5 0.05 5\n週期依 /timeframe（目前 {ACCOUNT_TF}）\n"
        "/confirm 確認啟動\n/stop 商品 方向\n/stopall 停全部+清殘單\n"
        "/status 所有策略現況\n/summary 當日戰報\n"
        "/amp 幣種 年份  整年5m振幅報表 Excel 寄信\n"
        "/timeframe 查看/設定週期\n/coins 幣種\n"
        "━━━━━━━━━━\n"
        f"一個 TF 一輪：TF 開始埋伏\n"
        f"未成交且剩餘不足 {ENTRY_CUTOFF}s → 撤單放棄本輪\n"
        "已進場 → OKX algo OCO 單守 TP/SL\n"
        "每根收盤推 SL（移動門檻%）｜每 N 秒現價追蹤 SL\n"
        "獲利≥0.1%→緊貼現價0.1%\n"
        "出場只有 TP / SL，無 TF 強平\n"
        f"{E.WARN} 真實下單，循環交易\n{E.OK} 重啟接管持倉與掛單")

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

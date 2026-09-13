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
SAVE_FIELDS = ("sym","dir","lev","margin","offset","tp","sl","move_pct","interval","chat",
               "locked_dir",
               "front_oid","front_px","front_static_sl","front_tp_px","front_sl_px",
               "front_filled","front_ee","front_sz","front_move_n","front_move_hist",
               "back_algo_id","back_px","back_filled","back_sz",
               "closing","state",
               "round_today","enter_today","round_date")

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

async def _place_limit(iid, pos_side, d, amb, sz, prefix="n"):
    """掛限價單，回傳 ordId 或 None。"""
    r = await api("POST", "/api/v5/trade/order", {
        "instId": iid, "tdMode": "isolated",
        "side": "buy" if d == "L" else "sell",
        "posSide": pos_side,
        "ordType": "limit", "px": str(amb), "sz": str(sz),
        "clOrdId": prefix + uuid.uuid4().hex[:14]
    })
    if r.get("code") == "0" and r.get("data"):
        return r["data"][0]["ordId"]
    return None

async def _cancel_order(iid, oid):
    """撤銷單張限價掛單。"""
    await api("POST", "/api/v5/trade/cancel-order", {"instId": iid, "ordId": oid})


async def _place_trigger(iid, pos_side, d, trigger_px, sz, prefix="b"):
    """掛計劃委託（觸發後市價進場，taker），回傳 algoId 或 None。
    後單專用：triggerPx = 前單靜態SL，觸發後以市價成交。
    """
    r = await api("POST", "/api/v5/trade/order-algo", {
        "instId": iid, "tdMode": "isolated",
        "side": "buy" if d == "L" else "sell",
        "posSide": pos_side,
        "ordType": "trigger",
        "sz": str(sz),
        "triggerPx": str(trigger_px),
        "orderPx": "-1",           # -1 = 市價
        "triggerPxType": "last",   # 以最新成交價觸發
        "algoClOrdId": prefix + uuid.uuid4().hex[:14]
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

    # 後單 TP/靜態SL（以後單埋伏價為基準）
    back_amb = front_static_sl
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
    front_oid = await _place_limit(iid, front_pos, d, front_amb, sz_front)
    if not front_oid:
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} 前單掛單失敗，暫停 5 秒後重試")
        return False

    # 掛後單（計劃委託 taker，觸發價 = 前單靜態SL）
    back_algo_id = await _place_trigger(iid, back_pos, back_d, front_static_sl, sz_back)
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
    S["back_static_sl"]  = str(back_static_sl)
    S["back_tp_px"]      = str(back_tp)
    S["back_filled"]     = False
    S["back_sz"]         = str(sz_back)
    S["back_d"]          = back_d
    S["state"]           = "委託中"
    save_state()
    return True


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

            candidates = [S for S in list(STRATS.values()) if S.get("alive")]
            if not candidates:
                continue

            # 查 OKX 持倉，確認哪些策略真的有倉位
            active = []
            for S in candidates:
                try:
                    d = S["dir"]
                    iid = S["spec"]["iid"]
                    pos_side = "long" if d == "L" else "short"
                    cur_pos = await okx_pos(iid, pos_side)
                    if cur_pos:
                        # OKX 有持倉，同步 front_filled
                        if not S.get("front_filled"):
                            S["front_filled"] = True
                        active.append(S)
                except Exception as e:
                    print("frame_mover 查持倉錯誤", S.get("sym"), type(e).__name__, e)
            if not active:
                continue

            due = [S for S in active
                   if now_t - float(S.get("_last_move_t", 0)) >= float(S.get("interval", 1))]
            if not due:
                continue

            # 批次查現價（每個 sym 查一次）
            px_map = {}
            for S in due:
                sym = S["sym"]
                if sym not in px_map:
                    try:
                        px_map[sym] = await get_last(S["spec"]["iid"])
                    except Exception:
                        px_map[sym] = None

            amends = []
            for S in due:
                if S.get("closing"):
                    continue
                px = px_map.get(S["sym"])
                if not px:
                    continue
                d = S["dir"]
                tick = S["spec"]["tick"]
                move_pct = Decimal(str(S["move_pct"])) / 100
                try:
                    cur_sl = Decimal(str(S["front_sl_px"]))
                    cur_px = Decimal(str(px))
                except Exception:
                    continue

                # SL 以現價為基準，距離固定 move_pct
                # 做多：SL = 現價 × (1 - move_pct)，保證 SL < 現價
                # 做空：SL = 現價 × (1 + move_pct)，保證 SL > 現價
                if d == "L":
                    nsl = align(cur_px * (1 - move_pct), tick, "L")
                    if nsl <= cur_sl:
                        continue  # 只能往上移
                else:
                    nsl = align(cur_px * (1 + move_pct), tick, "S")
                    if nsl >= cur_sl:
                        continue  # 只能往下移

                algo_id = S.get("algo_id")
                if not algo_id:
                    continue

                S["_pending_sl"] = (str(nsl), str(cur_px))
                amends.append((S["spec"]["iid"], algo_id, nsl, S))

            if amends:
                items = [(iid, aid, sl) for iid, aid, sl, _ in amends]
                okn = await amend_frames(items)
                for iid, aid, sl, S in amends:
                    if okn:
                        nsl, npx = S.pop("_pending_sl")
                        S["front_sl_px"] = nsl
                        S["front_move_n"] = int(S.get("front_move_n", 0)) + 1
                        S["_last_move_t"] = now_t
                        mh = S.get("front_move_hist")
                        if not isinstance(mh, list):
                            mh = []; S["front_move_hist"] = mh
                        mh.append({"t": hhmmss(), "type": "現",
                                   "px": npx, "sl": nsl})
                        if len(mh) > 200:
                            S["front_move_hist"] = mh[-200:]
                        save_state()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("frame_mover error", type(e).__name__, e)
            await asyncio.sleep(5)


async def _exit_front(app, S, chat, iid, reason, fpx, xpx, ee):
    """前單出場共用處理：計算損益、發通知、決定後續動作。"""
    d    = S["dir"]
    back_d = S.get("back_d", "S" if d == "L" else "L")
    spec = S["spec"]
    margin = Decimal(str(S["margin"]))
    sz   = Decimal(str(S.get("front_sz", "1")))
    ctval = spec["ctval"]
    tick  = spec["tick"]

    # 損益：查 OKX positions-history 真實數據
    after_ms = int(ee * 1000)
    pos_side = "long" if d == "L" else "short"
    rec = await close_record(iid, pos_side, after_ms, tries=20)
    if rec:
        xpx = Decimal(str(rec.get("closeAvgPx") or rec.get("last") or xpx))
        g   = Decimal(str(rec.get("realizedPnl") or "0"))
        fee = Decimal(str(rec.get("fee") or "0"))
        net = g + fee  # OKX fee 已是負數
        npv = float(net / margin * 100) if margin else 0
        # 出場原因
        pnl_type = rec.get("type", "")
        if pnl_type == "close_long" or pnl_type == "close_short":
            reason = "SL" if rec.get("closeAvgPx") else reason
    else:
        # OKX 查不到時用本地估算
        if d == "L":
            g = (xpx - fpx) * sz * ctval
        else:
            g = (fpx - xpx) * sz * ctval
        fee = -abs(xpx * sz * ctval) * Decimal("0.0005") * 2
        net = g + fee
        npv = float(net / margin * 100) if margin else 0

    mn = S.get("front_move_n", 0)
    mhist = S.get("front_move_hist") or []

    ico = E.WIN if net >= 0 else E.LOSS
    reason_label = "Take Profit" if reason == "TP" else "Stop Loss"

    # SL 移動明細
    def _sl_lines(mhist, mn):
        lines = []
        if mn > 0 and mhist:
            lines.append(f"━━━━━━━━━━\nSL移動 {mn} 次")
            for mrec in mhist[-20:]:
                tp_label = mrec.get("type", "現")
                lines.append(f"{mrec.get('t','')} | {tp_label} | {mrec.get('px','')} | 止{mrec.get('sl','')}")
        return lines

    sl_block = ("\n" + "\n".join(_sl_lines(mhist, mn))) if mn > 0 else ""

    # 查後單狀態
    back_algo_id = S.get("back_algo_id")
    back_d_local = S.get("back_d", "S" if d == "L" else "L")
    back_pos_side = "long" if back_d_local == "L" else "short"

    # 以 OKX 為主：每 0.5 秒查後單持倉，最多等 30 秒
    max_wait = 30
    waited = 0
    cur_back_pos = None
    while S.get("alive") and waited < max_wait:
        try:
            cur_back_pos = await okx_pos(iid, back_pos_side)
            if cur_back_pos:
                break  # 查到持倉，後單已進場
            # 查計劃委託狀態
            if back_algo_id:
                r_algo = await api("GET", f"/api/v5/trade/order-algo?algoId={back_algo_id}&ordType=trigger")
                if r_algo.get("code") == "0" and r_algo.get("data"):
                    algo_state = r_algo["data"][0].get("state", "")
                    if algo_state in ("live", "pause"):
                        # 計劃委託還在等待，後單未觸發，跳出
                        cur_back_pos = None
                        break
                    elif algo_state in ("canceled", "failed"):
                        # 計劃委託失效，跳出
                        cur_back_pos = None
                        break
                    # 其他狀態（已觸發但持倉未更新）繼續等
        except Exception as e:
            print("查後單持倉錯誤", type(e).__name__, e)
        await asyncio.sleep(0.5)
        waited += 0.5

    if cur_back_pos:
        # OKX 確認後單已進場 → 後單升格為新前單
        back_fpx_str = cur_back_pos.get("avgPx") or cur_back_pos.get("last") or S.get("back_px", "0")
        back_fpx = Decimal(str(back_fpx_str))
        back_sz  = Decimal(str(S.get("back_sz", "1")))
        back_tp  = Decimal(str(S.get("back_tp_px", "0")))
        back_static_sl = Decimal(str(S.get("back_static_sl", "0")))

        # 通知：前單出場 + 後單升格
        await notify(app, chat,
            f"{E.BOT} OKX原K｜{ACCT}\n事件：{ico} 前單出場 / 後單升格\n"
            f"━━━━━━━━━━\n"
            f"商品：{E.dir_emoji(d)} {S['sym']}\n"
            f"前單（{E.dir_word(d)}）：{reason_label}\n"
            f"進場：{fpx}\n"
            f"出場：{xpx}\n"
            f"━━━━━━━━━━\n"
            f"毛損益：{g:+.6f}\n"
            f"手續費：{-fee:.6f}\n"
            f"淨損益：{net:+.6f} ({npv:+.3f}%) {ico}"
            f"{sl_block}\n"
            f"━━━━━━━━━━\n"
            f"後單（{E.dir_word(back_d)}）：升格為新前單\n"
            f"進場：{back_fpx} | TP：{back_tp} | 靜態SL：{back_static_sl}\n"
            f"時間：{hhmmss()}")

        # 後單升格：更新 S 為新前單
        S["dir"]             = back_d
        S["front_oid"]       = None
        S["front_px"]        = str(back_fpx)
        S["front_static_sl"] = str(back_static_sl)
        S["front_tp_px"]     = str(back_tp)
        S["front_sl_px"]     = str(back_fpx)   # 動態SL重置為進場價
        S["front_filled"]    = True
        S["front_ee"]        = time.time()
        S["front_sz"]        = str(back_sz)
        S["front_move_n"]    = 0
        S["front_move_hist"] = []
        S["back_algo_id"]    = None
        S["back_filled"]     = False
        S["algo_id"]         = None
        S["state"]           = "升格持倉"
        save_state()

        # 補掛新後單
        new_back_d = "S" if back_d == "L" else "L"
        new_back_amb = back_static_sl
        sl_pct = Decimal(str(S["sl"])) / 100
        tp_pct = Decimal(str(S["tp"])) / 100
        if new_back_d == "S":
            new_back_sl = align(new_back_amb * (1 + sl_pct), tick, "S")
            new_back_tp = align(new_back_amb * (1 - tp_pct), tick, "L")
        else:
            new_back_sl = align(new_back_amb * (1 - sl_pct), tick, "L")
            new_back_tp = align(new_back_amb * (1 + tp_pct), tick, "S")
        new_back_sz = csize(Decimal(str(S["margin"])), Decimal(str(S["lev"])),
                            new_back_amb, spec["ctval"], spec["lot"])
        new_back_pos = "long" if new_back_d == "L" else "short"
        new_back_algo_id = await _place_trigger(iid, new_back_pos, new_back_d,
                                              new_back_amb, new_back_sz)
        if new_back_algo_id:
            S["back_algo_id"]   = new_back_algo_id
            S["back_px"]        = str(new_back_amb)
            S["back_static_sl"] = str(new_back_sl)
            S["back_tp_px"]     = str(new_back_tp)
            S["back_filled"]    = False
            S["back_sz"]        = str(new_back_sz)
            S["back_d"]         = new_back_d
            save_state()
            await notify(app, chat,
                f"{E.BOT} 新後單（{E.dir_word(new_back_d)}）已掛出\n"
                f"埋伏：{new_back_amb} | TP：{new_back_tp} | SL：{new_back_sl}\n"
                f"時間：{hhmmss()}")
        else:
            await notify(app, chat,
                f"{E.BOT} {E.LOSS} 新後單掛出失敗，請檢查\n時間：{hhmmss()}")

        # 掛新前單的 algo OCO（為新前單設 TP/SL）
        algo_id = await place_algo(iid, "long" if back_d == "L" else "short",
                                   back_d, back_sz, back_tp, back_static_sl)
        if algo_id:
            S["algo_id"] = algo_id
            save_state()

    else:
        # OKX 確認後單未進場 → 取消後單計劃委託，等下一輪TF重掛
        if back_algo_id:
            await _cancel_trigger(iid, back_algo_id)

        await notify(app, chat,
            f"{E.BOT} OKX原K｜{ACCT}\n事件：{ico} 前單出場 / 重新埋伏\n"
            f"━━━━━━━━━━\n"
            f"商品：{E.dir_emoji(d)} {S['sym']}\n"
            f"前單（{E.dir_word(d)}）：{reason_label}\n"
            f"進場：{fpx}\n"
            f"出場：{xpx}\n"
            f"━━━━━━━━━━\n"
            f"毛損益：{g:+.6f}\n"
            f"手續費：{-fee:.6f}\n"
            f"淨損益：{net:+.6f} ({npv:+.3f}%) {ico}"
            f"{sl_block}\n"
            f"━━━━━━━━━━\n後單已取消，重新下新一輪\n時間：{hhmmss()}")

        # 重置 S 方向為原始方向
        S["dir"] = S.get("locked_dir", d)
        # 清除持倉狀態
        for a in ("front_oid","front_px","front_static_sl","front_tp_px","front_sl_px",
                  "front_filled","front_ee","front_sz","front_move_n","front_move_hist",
                  "back_algo_id","back_px","back_filled","back_sz","back_d",
                  "algo_id","closing"):
            S.pop(a, None)
        S["state"] = "等下輪TF"
        save_state()

        # 等下一 TF 開盤才重掛
        tf_sec = TF_SEC.get(S.get("tf", ACCOUNT_TF), 300)
        now_t  = time.time()
        tf_end = (int(now_t // tf_sec) + 1) * tf_sec
        wait   = tf_end - now_t
        if wait > 0:
            await asyncio.sleep(wait)
        ok = await _place_pair(S, iid, chat, app, label="出場重掛")
        if not ok:
            await asyncio.sleep(5)


async def loop(app, chat, S):
    """前後單策略主迴圈。"""
    spec = S["spec"]
    iid  = spec["iid"]
    d    = S["dir"]
    k    = skey(S["sym"], d)
    try:
        # 第一輪：掛前後單
        ok = await _place_pair(S, iid, chat, app, label="首次埋伏")
        if not ok:
            S["alive"] = False
            return

        # 主監控迴圈
        while S["alive"]:
            await asyncio.sleep(1)

            front_oid    = S.get("front_oid")
            front_filled = S.get("front_filled", False)
            back_filled  = S.get("back_filled", False)

            if S.get("closing"):
                continue

            # ── TF 計時：前單未成交時，TF 結束前 1 秒撤單重掛 ──
            if not front_filled and not back_filled and front_oid:
                tf_sec = TF_SEC.get(S.get("tf", ACCOUNT_TF), 300)
                now_t  = time.time()
                tf_end = (int(now_t // tf_sec) + 1) * tf_sec
                secs_left = tf_end - now_t
                if secs_left <= 1.5:   # TF 結束前 1.5 秒觸發
                    # 撤前單
                    await _cancel_order(iid, front_oid)
                    # 撤後單計劃委託
                    back_algo_id = S.get("back_algo_id")
                    if back_algo_id:
                        await _cancel_trigger(iid, back_algo_id)
                    # 清除掛單狀態
                    for a in ("front_oid","front_px","front_static_sl","front_tp_px",
                              "front_sl_px","front_filled","front_ee","front_sz",
                              "front_move_n","front_move_hist",
                              "back_algo_id","back_px","back_filled","back_sz","back_d"):
                        S.pop(a, None)
                    # 等到新 TF 開盤（最多等 2 秒）
                    await asyncio.sleep(min(secs_left + 0.2, 2))
                    # 查現價，立刻重掛
                    ok = await _place_pair(S, iid, chat, app, label="TF重掛")
                    if not ok:
                        await asyncio.sleep(3)
                    continue

            # 查前單狀態：以 OKX 持倉為主
            front_pos_side = "long" if d == "L" else "short"
            cur_front_pos = await okx_pos(iid, front_pos_side)
            if front_oid and not front_filled and cur_front_pos:
                # OKX 有持倉，確認前單已進場
                state, avgpx = await _order_state(iid, front_oid)
                fpx = Decimal(avgpx or S["front_px"])
                S["front_filled"] = True
                S["front_px"]     = str(fpx)
                S["front_sl_px"]  = str(fpx)   # 動態SL從進場價開始
                S["front_ee"]     = time.time()
                S["state"]        = "持倉中"
                # 進場次數 +1
                today = today8()
                if S.get("round_date") != today:
                    S["round_today"] = 0; S["enter_today"] = 0; S["round_date"] = today
                S["enter_today"] = int(S.get("enter_today", 0)) + 1
                save_state()
                # 掛前單 algo OCO
                back_d = S.get("back_d", "S" if d == "L" else "L")
                front_tp = Decimal(str(S["front_tp_px"]))
                front_static_sl = Decimal(str(S["front_static_sl"]))
                algo_id = await place_algo(iid,
                                           "long" if d == "L" else "short",
                                           d, Decimal(str(S["front_sz"])),
                                           front_tp, front_static_sl)
                if algo_id:
                    S["algo_id"] = algo_id
                save_state()
                await notify(app, chat,
                    f"{E.BOT} OKX原K｜{ACCT}\n事件：{E.ENTRY} 前單進場成交\n"
                    f"━━━━━━━━━━\n"
                    f"商品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                    f"進場：{fpx} | {hhmmss()}\n"
                    f"TP：{front_tp}（+{S['tp']}%）\n"
                    f"靜態SL：{front_static_sl}（-{S['sl']}%）\n"
                    f"動態SL每{S['interval']}s移動，門檻{S['move_pct']}%\n"
                    f"後單（{E.dir_word(back_d)}）埋伏中：{S.get('back_px')}\n"
                    f"時間：{hhmmss()}")

            # 查後單狀態（計劃委託是否已觸發成交）
            back_algo_id = S.get("back_algo_id")
            if back_algo_id and not back_filled:
                r_algo = await api("GET", f"/api/v5/trade/order-algo?algoId={back_algo_id}&ordType=trigger")
                if r_algo.get("code") == "0" and r_algo.get("data"):
                    algo_state = r_algo["data"][0].get("state", "")
                    if algo_state == "order":  # 已觸發並掛出
                        # 查實際成交
                        fill_px = r_algo["data"][0].get("avgPx") or r_algo["data"][0].get("triggerPx")
                        S["back_filled"] = True
                        if fill_px:
                            S["back_px"] = fill_px
                        save_state()
                    elif algo_state in ("canceled", "failed"):
                        # 計劃委託被取消，視為後單失效
                        S["back_algo_id"] = None
                        save_state()

            # 偵測前單出場（已進場但倉位消失）
            if front_filled and not S.get("closing"):
                pos_side = "long" if d == "L" else "short"
                cur_pos = await okx_pos(iid, pos_side)
                if not cur_pos:
                    S["closing"] = True
                    # 判斷出場原因（查最近成交）
                    reason = "SL"
                    fpx = Decimal(str(S["front_px"]))
                    front_tp_val = Decimal(str(S["front_tp_px"]))
                    # 查最近成交價
                    xpx = fpx  # 預設
                    try:
                        fills = await api("GET", f"/api/v5/trade/fills?instId={iid}&limit=5")
                        if fills.get("code") == "0" and fills.get("data"):
                            xpx = Decimal(fills["data"][0].get("fillPx") or str(fpx))
                            # 判斷 TP 或 SL
                            if d == "L" and xpx >= front_tp_val * Decimal("0.998"):
                                reason = "TP"
                            elif d == "S" and xpx <= front_tp_val * Decimal("1.002"):
                                reason = "TP"
                    except Exception:
                        pass
                    ee = S.get("front_ee") or time.time()
                    await _exit_front(app, S, chat, iid, reason, fpx, xpx, ee)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("loop error", S.get("sym"), S.get("dir"), type(e).__name__, e)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} 策略錯誤：{type(e).__name__}: {e}")
    finally:
        if not SHUTTING_DOWN:
            S["state"] = "已停止"; S["alive"] = False
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
         "offset": Decimal(str(d["offset"])), "tp": Decimal(str(d["tp"])),
         "sl": Decimal(str(d["sl"])),
         "move_pct": Decimal(str(d.get("move_pct", "0"))),
         "interval": float(d.get("interval", 1)),
         "spec": spec,
         "alive": True, "state": d.get("state", "委託中"),
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
    if not S or not S.get("alive"):
        return f"{dr}（已停止）"
    return (f"{dr} {S['lev']}x {pct(S['margin'])} {pct(S['offset'])} "
            f"{pct(S['tp'])} {pct(S['sl'])} {pct(S.get('move_pct',0))}% {S.get('interval',0)}s")

async def cmd_run(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args
    fmt = (f"{E.BOT} 用法：/run 商品 方向 槓桿 保證金 埋伏% TP% SL% 移動門檻% 間隔秒\n"
           f"例：/run ETHUSDT L 1x 3 0.3 5 0.2 0.01 1\n"
           f"共9個參數，方向只能 L 或 S")
    if len(a) != 9:
        await reply(u, f"{E.BOT} 參數數量錯誤（需9個）\n{fmt}"); return
    try:
        sym = a[0].upper(); dr = a[1].upper(); lev = int(a[2].replace("x", ""))
        margin = Decimal(a[3]); offset = Decimal(a[4])
        tp = Decimal(a[5].rstrip("%")); sl = Decimal(a[6].rstrip("%"))
        move_pct = Decimal(a[7].rstrip("%")); interval = float(a[8])
    except Exception:
        await reply(u, f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if dr not in ("L", "S"):
        await reply(u, f"{E.BOT} 方向須 L 或 S"); return
    for nm, v in (("移動門檻", move_pct), ("TP", tp), ("SL", sl)):
        if v < 0:
            await reply(u, f"{E.BOT} {nm} 不可為負數"); return
    if not 0.1 <= interval <= 300:
        await reply(u, f"{E.BOT} 間隔秒須介於 0.1~300（支援小數一位，例如 0.5、1.5）"); return

    # 方向鎖定檢查：帳戶已有任何活躍策略就擋
    if STRATS:
        alive = [S for S in STRATS.values() if S.get("alive")]
        if alive:
            locked = alive[0].get("locked_dir", alive[0].get("dir"))
            if locked != dr:
                await reply(u, f"{E.BOT} {E.LOSS} 帳戶已鎖定方向 {E.dir_word(locked)}，請先 /stop 再重新下單"); return
            await reply(u, f"{E.BOT} {sym} {E.dir_word(dr)} 已在運行"); return

    try:
        spec = await get_spec(sym)
    except Exception:
        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return

    op = await get_last(spec["iid"])
    tick = spec["tick"]
    # 前單埋伏價
    front_amb = align(op * (1 - offset / 100) if dr == "L"
                      else op * (1 + offset / 100), tick, dr)
    # 前單靜態 SL（同時也是後單的埋伏限價）
    back_dr = "S" if dr == "L" else "L"
    if dr == "L":
        front_static_sl = align(front_amb * (1 - sl / 100), tick, "L")
    else:
        front_static_sl = align(front_amb * (1 + sl / 100), tick, "S")
    # 前單 TP
    if dr == "L":
        front_tp = align(front_amb * (1 + tp / 100), tick, "S")
    else:
        front_tp = align(front_amb * (1 - tp / 100), tick, "L")
    # 後單靜態 SL（以後單埋伏價為基準）
    if back_dr == "S":
        back_static_sl = align(front_static_sl * (1 + sl / 100), tick, "S")
        back_tp = align(front_static_sl * (1 - tp / 100), tick, "L")
    else:
        back_static_sl = align(front_static_sl * (1 - sl / 100), tick, "L")
        back_tp = align(front_static_sl * (1 + tp / 100), tick, "S")

    sz_front = csize(margin, Decimal(lev), front_amb, spec["ctval"], spec["lot"])
    sz_back  = csize(margin, Decimal(lev), front_static_sl, spec["ctval"], spec["lot"])
    if sz_front < spec["minsz"]:
        need = spec["minsz"] * spec["ctval"] * op / Decimal(lev)
        await reply(u, f"{E.BOT} {E.LOSS} 保證金不足：前單算出 {sz_front} 張 < 最小 {spec['minsz']}\n至少需 {need:.4f} USDT"); return

    PENDING[u.effective_chat.id] = {
        "kind": "run", "t": time.time(),
        "sym": sym, "dir": dr, "lev": lev, "margin": margin,
        "offset": offset, "tp": tp, "sl": sl,
        "move_pct": move_pct, "interval": interval, "spec": spec,
        "front_amb": front_amb, "front_static_sl": front_static_sl,
        "front_tp": front_tp, "front_sz": sz_front,
        "back_dr": back_dr, "back_amb": front_static_sl,
        "back_static_sl": back_static_sl, "back_tp": back_tp,
        "back_sz": sz_back, "locked_dir": dr,
    }
    await reply(u, f"{E.BOT} OKX原K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
        f"商品：{E.dir_emoji(dr)} {sym} {E.dir_word(dr)} {lev}x\n"
        f"現價：{op}\n"
        f"━━━━━━━━━━\n"
        f"前單（{E.dir_word(dr)}）\n"
        f"  埋伏：{front_amb}（距現價{offset}%）\n"
        f"  TP：{front_tp}（+{tp}%）\n"
        f"  靜態SL：{front_static_sl}（-{sl}%）\n"
        f"  保證金：{margin} USDT｜{sz_front}張\n"
        f"━━━━━━━━━━\n"
        f"後單（{E.dir_word(back_dr)}）\n"
        f"  埋伏：{front_static_sl}（= 前單靜態SL）\n"
        f"  TP：{back_tp}（+{tp}%）\n"
        f"  靜態SL：{back_static_sl}（-{sl}%）\n"
        f"  保證金：{margin} USDT｜{sz_back}張\n"
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
        await do_stop(u, p["key"]); return
    if kind == "stopall":
        del PENDING[u.effective_chat.id]
        await do_stopall(u); return
    del PENDING[u.effective_chat.id]
    k = skey(p["sym"], p["dir"])
    old_t = TASKS.get(k)
    if old_t and not old_t.done():
        old_s = STRATS.get(k)
        if old_s: old_s["alive"] = False
        old_t.cancel()
    S = {**p, "alive": True, "state": "委託中", "chat": u.effective_chat.id,
         "locked_dir": p.get("locked_dir", p["dir"])}
    STRATS[k] = S
    TASKS[k] = asyncio.create_task(loop(c.application, u.effective_chat.id, S))
    save_state()
    cnt = sum(1 for s in STRATS.values() if s.get("alive"))
    await reply(u, f"{E.BOT} {E.OK} 已確認，{p['sym']} {E.dir_word(p['dir'])} 啟動\n運行中策略：{cnt} 個")

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
    back_d = S.get("back_d", "S" if d == "L" else "L")
    back_ps = "long" if back_d == "L" else "short"
    p = await okx_pos(iid, ps)
    S["alive"] = False
    n = await sweep(iid, ps)
    na = await sweep_algos(iid, back_ps)
    save_state()
    tail = f"\n{E.WARN} 持倉 {p['pos']} 張，請至 OKX 平倉" if p else ""
    await reply(u, f"{E.BOT} 已停止 {E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}｜撤限價單 {n}｜撤計劃委託 {na}{tail}")

async def cmd_stopall(u, c):
    alive = [k for k, s in STRATS.items() if s.get("alive")]
    if not alive:
        await reply(u, f"{E.BOT} 目前無運行中策略"); return
    PENDING[u.effective_chat.id] = {"kind": "stopall", "t": time.time()}
    await reply(u, f"{E.BOT} {E.WARN} 將停止全部 {len(alive)} 個策略\n60秒內 /confirm 確認")
    asyncio.create_task(_to(c.application, u.effective_chat.id, PENDING[u.effective_chat.id]["t"]))

async def do_stopall(u):
    alive = [k for k, s in STRATS.items() if s.get("alive")]
    held = []; done = []
    for k in list(alive):
        S = STRATS[k]; d = S["dir"]; iid = S["spec"]["iid"]
        ps = "long" if d == "L" else "short"
        back_d = S.get("back_d", "S" if d == "L" else "L")
        back_ps = "long" if back_d == "L" else "short"
        p = await okx_pos(iid, ps)
        S["alive"] = False
        await sweep(iid, ps)
        await sweep_algos(iid, back_ps)   # 撤後單計劃委託
        (held if p else done).append(f"{S['sym']} {S['dir']}")
    orphan = 0
    for o in await okx_orders(prefix="n"):
        cr = await api("POST", "/api/v5/trade/cancel-order", {"instId": o["instId"], "ordId": o["ordId"]})
        if cr.get("code") == "0": orphan += 1
    save_state()
    m = f"{E.BOT} 已停止 {len(done)} 個策略｜清殘單 {orphan}"
    if held: m += "\n{E.WARN} 持倉需手動平倉：" + "、".join(held)
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
    L = [f"{E.BOT} OKX原K｜{ACCT}", "事件：現況（即時查OKX）", "━━━━━━━━━━",
         f"USDT權益：{eq}", f"可用餘額：{av}", f"帳戶週期：{ACCOUNT_TF}",
         f"運行中策略：{len(alive)}個"]
    # 查計劃委託數（trigger algo）
    algo_r = await api("GET", "/api/v5/trade/orders-algo-pending?ordType=trigger")
    algo_list = algo_r.get("data", []) if algo_r.get("code") == "0" else []
    total_pending = len(pdl) + len(algo_list)   # 限價單 + 計劃委託

    for s in alive:
        d = s["dir"]
        back_d = s.get("back_d", "S" if d == "L" else "L")
        # 計算前後單狀態
        front_waiting  = 1 if s.get("front_oid") and not s.get("front_filled") else 0
        back_waiting   = 1 if s.get("back_algo_id") and not s.get("back_filled") else 0
        front_in       = 1 if s.get("front_filled") else 0
        back_in        = 1 if s.get("back_filled") else 0

        state_str = f"前{front_waiting}/後{back_waiting}/前進{front_in}/後進{back_in}"
        if front_in or back_in:
            live_label = "持倉中"
            live_emoji = E.HOLD
        else:
            live_label = "委託中"
            live_emoji = E.dir_emoji(d)

        L.append("━━━━━━━━━━")
        L.append(f"{live_emoji} {s['sym']}")
        L.append(f"{live_label}({state_str})")
        L.append(f"參數：{strat_params(s['sym'], s['dir'])}")
        round_t = int(s.get("round_today", 0))
        enter_t = int(s.get("enter_today", 0))
        L.append(f"今日輪次：{round_t}｜今日進場：{enter_t}")

        # 前單資訊
        if front_waiting:
            L.append(f"前單（{E.dir_word(d)}）")
            L.append(f"  埋伏：{s.get('front_px','-')}")
            L.append(f"  TP：{s.get('front_tp_px','-')} | SL：{s.get('front_static_sl','-')}")
        elif front_in:
            fpx = s.get("front_px", "-")
            tp_s = s.get("front_tp_px", "-")
            static_sl = s.get("front_static_sl", "-")
            sl_s = s.get("front_sl_px", "-")
            mn = s.get("front_move_n", 0)
            L.append(f"前單（{E.dir_word(d)}）進場：{fpx}")
            L.append(f"  TP：{tp_s}（固定）")
            L.append(f"  靜態SL：{static_sl}")
            L.append(f"  動態SL：{sl_s}（目前）")
            L.append(f"  SL移動：{mn}次")
            mhist = s.get("front_move_hist") or []
            prev_px = None
            for mrec in mhist:
                cur_px = mrec.get("px", "")
                arrow = E.price_emoji(cur_px, prev_px) if prev_px else "🔸"
                L.append(f"  {mrec.get('t','')} | {arrow} | {cur_px} | 止{mrec.get('sl','')}")
                prev_px = cur_px

        # 後單資訊
        if back_waiting:
            L.append(f"後單（{E.dir_word(back_d)}）：觸發{s.get('back_px','-')}")
        elif back_in:
            fpx = s.get("back_px", "-")
            tp_s = s.get("back_tp_px", "-")
            static_sl = s.get("back_static_sl", "-")
            sl_s = s.get("front_sl_px", "-")
            mn = s.get("front_move_n", 0)
            L.append(f"後單升格（{E.dir_word(back_d)}）進場：{fpx}")
            L.append(f"  TP：{tp_s}（固定）")
            L.append(f"  靜態SL：{static_sl}")
            L.append(f"  動態SL：{sl_s}（目前）")
            L.append(f"  SL移動：{mn}次")
            mhist = s.get("front_move_hist") or []
            prev_px = None
            for mrec in mhist:
                cur_px = mrec.get("px", "")
                arrow = E.price_emoji(cur_px, prev_px) if prev_px else "🔸"
                L.append(f"  {mrec.get('t','')} | {arrow} | {cur_px} | 止{mrec.get('sl','')}")
                prev_px = cur_px

    L.append("━━━━━━━━━━")
    L.append(f"掛單數：{total_pending}｜持倉數：{len(pl)}")
    for p in pl:
        pd = "L" if p["posSide"] == "long" else "S"
        L.append(f"{E.dir_emoji(pd)} {p['instId'].replace('-USDT-SWAP','USDT')} {pd}")
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

async def get_klines_paged(iid, bar, want):
    """分頁往前抓 K 線（OKX 單次上限 300），回傳舊->新、只含已收線。"""
    out = []
    after = ""
    ep = "candles"          # 近期用 candles，翻不動時自動切 history-candles
    for _ in range(400):    # 最多 400 頁（支援整年 5m ≈ 351 頁）
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
    欄位：幣種/週期/日期/時間/漲跌/開/高/低/收/漲跌幅%/振幅%/ABS(振幅%-漲跌幅%)/開到高/開到高%/開到低/開到低%/收到高/收到高%/收到低/收到低%
    右側分析表：開到高% / 開到低% / 收到高% / 收到低% 門檻統計（≥0.1% ~ ≥5.0%）
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
    TINT = 0.7999816888943144
    fill_blue = PatternFill("solid", fgColor=Color(theme=6, tint=TINT, type="theme"))
    fill_orng = PatternFill("solid", fgColor=Color(theme=9, tint=TINT, type="theme"))
    fill_grn  = PatternFill("solid", fgColor=Color(theme=6, tint=0.6, type="theme"))   # 收到高群組
    fill_red  = PatternFill("solid", fgColor=Color(theme=9, tint=0.6, type="theme"))   # 收到低群組

    wb = Workbook()
    wb.remove(wb.active)

    for sym, kl, amps, tick in results:
        dp = max(0, -tick.as_tuple().exponent)
        price_fmt = "0" if dp == 0 else "0." + "0" * dp

        ws = wb.create_sheet(title=sym)
        hdr = Font(name=FONT, bold=True, size=11)
        dat = Font(name=FONT, size=11)
        lft = Alignment(horizontal="left")
        cen = Alignment(horizontal="center")

        # 主欄標頭（row 2，col B~U）
        # B=幣種 C=週期 D=日期 E=時間 F=漲跌
        # G=開 H=高 I=低 J=收
        # K=漲跌幅% L=振幅% M=ABS(振幅%-漲跌幅%)
        # N=開到高 O=開到高% P=開到低 Q=開到低%
        # R=收到高 S=收到高% T=收到低 U=收到低%
        main_heads = ["幣種", "週期", "日期", "時間", "漲跌",
                      "開", "高", "低", "收", "漲跌幅%", "振幅%", "ABS(振幅%-漲跌幅%)",
                      "開到高", "開到高%", "開到低", "開到低%",
                      "收到高", "收到高%", "收到低", "收到低%"]
        fills_main = [fill_blue] * 12 + [fill_blue] * 4 + [fill_grn] * 4
        for ci, (h, f) in enumerate(zip(main_heads, fills_main), start=2):
            c = ws.cell(row=2, column=ci, value=h)
            c.font = hdr; c.alignment = cen; c.fill = f

        # 分析表標頭（row 2）：W/X/Y 淡藍（開到高%），AA/AB/AC 淡橘（開到低%）
        #                       AE/AF/AG 收到高%，AI/AJ/AK 收到低%
        # col: 23=W 24=X 25=Y  27=AA 28=AB 29=AC  31=AE 32=AF 33=AG  35=AI 36=AJ 37=AK
        for col, label, fill in [
            (23, "開到高%門檻", fill_blue), (24, "根數", fill_blue), (25, "佔比", fill_blue),
            (27, "開到低%門檻", fill_orng), (28, "根數", fill_orng), (29, "佔比", fill_orng),
            (31, "收到高%門檻", fill_grn),  (32, "根數", fill_grn),  (33, "佔比", fill_grn),
            (35, "收到低%門檻", fill_red),  (36, "根數", fill_red),  (37, "佔比", fill_red),
        ]:
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
            abs_diff = abs(amp_pct - chg_pct)
            h2o = h - o;  h2o_pct = h2o / o if o else 0
            l2o = o - lo; l2o_pct = l2o / o if o else 0
            h2c = h - cl; h2c_pct = h2c / cl if cl else 0   # 收到高
            c2l = cl - lo; c2l_pct = c2l / cl if cl else 0  # 收到低
            flag = E.UP if cl >= o else E.DOWN
            vals = [sym, tf, dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"),
                    flag, o, h, lo, cl, chg_pct, amp_pct, abs_diff,
                    round(h2o, dp), h2o_pct, round(l2o, dp), l2o_pct,
                    round(h2c, dp), h2c_pct, round(c2l, dp), c2l_pct]
            for ci, v in enumerate(vals, start=2):
                ws.cell(row=r, column=ci, value=v).font = dat
            # 價格欄（開/高/低/收/開到高/開到低/收到高/收到低）
            for ci in [7, 8, 9, 10, 14, 16, 18, 20]:
                ws.cell(r, ci).number_format = price_fmt
            # 漲跌幅%
            ws.cell(r, 11).number_format = "0.0000%;[Red]\\-0.0000%"
            # 振幅%、ABS差、開到高%、開到低%、收到高%、收到低%
            for ci in [12, 13, 15, 17, 19, 21]:
                ws.cell(r, ci).number_format = "0.0000%"

        # 分析表（row 1 SUM，row 3+ 門檻）
        last_r = len(kl) + 2
        # 開到高% → 欄 O(15)，根數欄 X(24)
        ws.cell(1, 24, f"=COUNT($O3:$O{last_r})").font = Font(name=FONT, bold=True, size=11)
        # 開到低% → 欄 Q(17)，根數欄 AB(28)
        ws.cell(1, 28, f"=COUNT($Q3:$Q{last_r})").font = Font(name=FONT, bold=True, size=11)
        # 收到高% → 欄 S(19)，根數欄 AF(32)
        ws.cell(1, 32, f"=COUNT($S3:$S{last_r})").font = Font(name=FONT, bold=True, size=11)
        # 收到低% → 欄 U(21)，根數欄 AJ(36)
        ws.cell(1, 36, f"=COUNT($U3:$U{last_r})").font = Font(name=FONT, bold=True, size=11)

        for ti, (label, dec) in enumerate(THRESHOLDS):
            tr = ti + 3
            ps = f">={dec}"
            # 開到高%
            ws.cell(tr, 23, label).font = dat; ws.cell(tr, 23).alignment = lft
            ws.cell(tr, 24, f'=COUNTIF({sym}!$O3:$O{last_r},"{ps}")').font = dat
            c = ws.cell(tr, 25, f"=X{tr}/X$1"); c.font = dat; c.number_format = "0.00%"
            # 開到低%
            ws.cell(tr, 27, label).font = dat; ws.cell(tr, 27).alignment = lft
            ws.cell(tr, 28, f'=COUNTIF({sym}!$Q3:$Q{last_r},"{ps}")').font = dat
            c = ws.cell(tr, 29, f"=AB{tr}/AB$1"); c.font = dat; c.number_format = "0.00%"
            # 收到高%
            ws.cell(tr, 31, label).font = dat; ws.cell(tr, 31).alignment = lft
            ws.cell(tr, 32, f'=COUNTIF({sym}!$S3:$S{last_r},"{ps}")').font = dat
            c = ws.cell(tr, 33, f"=AF{tr}/AF$1"); c.font = dat; c.number_format = "0.00%"
            # 收到低%
            ws.cell(tr, 35, label).font = dat; ws.cell(tr, 35).alignment = lft
            ws.cell(tr, 36, f'=COUNTIF({sym}!$U3:$U{last_r},"{ps}")').font = dat
            c = ws.cell(tr, 37, f"=AJ{tr}/AJ$1"); c.font = dat; c.number_format = "0.00%"

        # Row/Col 字體
        for rd in ws.row_dimensions.values():
            rd.font = Font(name=FONT, size=11)
        for cd in ws.column_dimensions.values():
            cd.font = Font(name=FONT, size=11)

        # 欄寬
        col_widths = {
            1: 4.4,   2: 12.8,  3: 6.8,   4: 16.8,  5: 11.4,
            6: 6.8,   7: 11.2,  11: 13.6, 12: 12.2, 13: 13.0,
            14: 9.4,  15: 12.2, 16: 9.0,  17: 12.2, 18: 3.0,
            19: 9.4,  20: 12.2, 21: 9.0,  22: 12.2, 23: 3.0,
            24: 16.0, 25: 6.8,  26: 13.0, 27: 3.0,  28: 16.0,
            29: 6.8,  30: 13.0, 31: 3.0,  32: 16.0, 33: 6.8,
            34: 13.0, 35: 3.0,  36: 16.0, 37: 6.8,  38: 13.0,
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
    """原K 振幅分析報表（單幣種整年 Excel 寄信）。
    用法：/amp <幣種> <年份>
    例：/amp ETHUSDT 2025
    TF 固定 5m，抓整年資料，產生 Excel 寄到信箱。
    """
    fmt = f"{E.BOT} 用法：/amp <幣種> <年份>\n例：/amp ETHUSDT 2025\nTF 固定 5m，產生整年 Excel 寄到信箱"
    if not c.args or len(c.args) != 2:
        await reply(u, fmt); return
    sym = c.args[0].upper()
    try:
        year = int(c.args[1])
        if not 2020 <= year <= 2030:
            raise ValueError
    except (ValueError, TypeError):
        await reply(u, f"{E.LOSS} 年份格式錯誤，例：2025"); return

    tf = "5m"
    # 計算該年的起訖 timestamp（ms）
    from calendar import isleap
    days = 366 if isleap(year) else 365
    n = 12 * 24 * days  # 整年 5m 根數
    ts_start = int(datetime(year, 1, 1, 0, 0, 0, tzinfo=TZ8).timestamp() * 1000)
    ts_end   = int(datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=TZ8).timestamp() * 1000)

    await reply(u, f"{E.BOT} 振幅分析報表中…\n"
                   f"幣種：{sym}\nTF：{tf}｜{year}全年（{days}天，約{n}根）\n"
                   f"預計需要 5~10 分鐘，請稍候…")
    try:
        spec = await get_spec(sym)
    except Exception:
        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return

    # 抓整年 K 線（帶起訖時間過濾）
    try:
        kl_raw = await get_klines_paged(spec["iid"], "5m", n)
    except Exception as e:
        await reply(u, f"{E.LOSS} K 線抓取失敗：{type(e).__name__}: {e}"); return

    # 只保留該年度的 K 線
    kl = [k for k in kl_raw if ts_start <= k["ts"] < ts_end]
    if not kl:
        await reply(u, f"{E.LOSS} {sym} {year} 年無資料"); return

    amps = calc_amp(kl)
    results = [(sym, kl, amps, spec["tick"])]

    day = now8().strftime("%Y%m%d")
    name = f"OKX.{sym}.5m.{year}.{day}.xlsx"
    path = f"/srv/1111bot/data/{name}"
    try:
        build_amp_xlsx(results, tf, path)
    except Exception as e:
        await reply(u, f"{E.LOSS} 產生 Excel 失敗：{type(e).__name__}: {e}"); return

    subject = f"OKX 振幅分析 {sym} 5m {year}全年（{len(kl)}根）"
    body = (f"幣種：{sym}｜TF：5m｜{year}全年\n"
            f"實際根數：{len(kl)} 根（{days}天）\n"
            f"產生時間：{now8().strftime('%Y/%m/%d %H:%M:%S')}\n")
    try:
        ok, info = send_amp_mail(path, name, subject, body)
    except Exception as e:
        await reply(u, f"{E.LOSS} 寄送失敗：{type(e).__name__}: {e}\n檔案已存於 VPS：{name}"); return
    if not ok:
        await reply(u, f"{E.LOSS} 未寄送：{info}\n檔案已存於 VPS：{name}"); return
    await reply(u, f"{E.BOT} {E.OK} {sym} {year}全年振幅報表已寄出\n"
                   f"TF：5m｜實際根數：{len(kl)}\n時間：{hhmmss()}")

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

#!/usr/bin/env python3
# 設計此腳本的目的在於用bot取代我在交易所app上的一切手動行為，切記
"""B6-1 原K｜多帳戶 — v3.1 跨式雙向埋伏版

【戰術】
  同時掛 A限價 + B觸發（反向、同量）。兩單都成交時完全對沖，損益鎖死 = -兩單間距，
  與價格無關。價格衝出箱子時一邊被SL掃掉、一邊獨活順勢起飛。
  賭的是「一根針」：多數戰役小輸手續費，少數戰役吃到大波動翻盤。

【SL 唯一公式】峰值緊貼，棘輪不後退
  做L：峰值 = 進場後最高價（初始=進場價）
       若 峰值 >= 進場價×(1+F) → SL = max(靜態SL, 峰值×(1-F))；否則 SL 不動
  做S：鏡像（谷值 = 最低價，SL = min(靜態SL, 谷值×(1+F))）
  F = 該單來回手續費率（A=FEE_A、B=FEE_B）。啟動前靜態SL緩衝區完整保留。
  此式自然涵蓋三階段：毛利F→SL到進場價(保本)、毛利2F→SL到淨歸零、之後一路緊貼。

【TF 判斷樹】TF 是唯一的時間閘門
  兩單都持倉      → 不動（對沖中、損益鎖死，不許攪局）
  單邊持倉有獲利  → 不動（讓緊貼SL去跑）
  單邊持倉無獲利  → SL 貼現價 ∓F（逼出場，沒有對沖就不該久留）
  零持倉          → 撤所有掛單、重新取價、重新部署

【鐵則】
  1. OKX 為唯一真相來源：撤單、持倉、損益一律回查 OKX 確認。
  2. 下單必帶 TP/SL（attachAlgoOrds），成交當下即生效，無裸倉空窗。
  3. 重啟時接管 OKX 上的既有持倉與掛單，不留孤兒。
  4. Telegram 與 WebSocket 皆為旁路：失效絕不影響交易與保護。
  5. 平倉一律人工，守門狗只告警不自動平倉。
"""
import sys, hmac, base64, hashlib, json, time, asyncio, uuid, os
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
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

VERSION = "v3.7.3"     # 腳本版本號：回報問題時請附上（/status 最後一行顯示）
BASE = "https://www.okx.com"
ACCT = os.environ.get("ACCT", "o3333o")  # 由 systemd 注入
TZ8 = timezone(timedelta(hours=8))
ACCOUNT_TF = "5m"
STATE_FILE = f"/srv/1111bot/data/strategies_{ACCT}.json"
NAKED_ALERT_SEC = 15  # 守門狗：有倉但未掛止盈止損超過 N 秒 → 只發一次 TG 告警（絕不自動平倉）

# ---------- 交易所相關常數（換交易所時改這裡，不是改參數） ----------
# 來回手續費率。A單=限價進場(maker)+市價出場(taker)；B單=市價進場+市價出場(taker×2)。
# 這兩個值同時就是各自的「SL緊貼距離」：貼在手續費水位上，出場即約略打平。
FEE_A = Decimal("0.00072")   # A單 0.072%
FEE_B = Decimal("0.00120")   # B單 0.120%
FEE_TOTAL = FEE_A + FEE_B    # 一場雙邊成交戰役的固定成本 0.192%

PRICE_TICK_SEC = 0.5   # 查價 / SL送單節流間隔（秒）。WS 推送是連續的，此值只節流送單
MIN_HUG_TICKS  = 3     # 緊貼距離下限（檔）。低於此值會被買賣價差直接掃掉
MOVE_TICK      = 0.5   # frame_mover 心跳（秒）
AMEND_BACKOFF  = (1, 2, 4, 8)  # amend 連續失敗的退避秒數（問題18：不再每秒無限重試）

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
SAVE_FIELDS = ("sym","dir","lev","margin","offset","gap","tp","sl","hug_auto","chat",
               "locked_dir","pair_state",
               "front_oid","front_px","front_static_sl","front_tp_px","front_sl_px",
               "front_filled","front_ee","front_sz","front_move_n","front_move_hist",
               "front_peak","front_trough","front_exit_reason","front_exit_pnl",
               "back_algo_id","back_algo2_id","back_amb_px","back_px","back_static_sl",
               "back_tp_px","back_sl_px","back_filled","back_ee","back_sz","back_d",
               "back_peak","back_trough","back_exit_reason","back_exit_pnl",
               "algo_id","exit_seq","battle_form","battle_t0","_reported_closes",
               "hug_note","amp",
               "round_date","round_today","enter_today")

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
    """計數器。field 可為任意名稱（placed / entered / form_雙邊成交 ...），
    不存在就從 0 起算 —— 舊版直接 += 會對新欄位丟 KeyError。"""
    t = today8()
    if k not in STATS or STATS[k].get("date") != t:
        STATS[k] = {"date": t, "placed": 0, "entered": 0}
    STATS[k][field] = int(STATS[k].get(field, 0)) + 1
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

# ==================== WebSocket 價格來源（v3.1） ====================
# 【為什麼需要】新的 SL 公式是 SL = 峰值 ∓ F，峰值漏抓就永遠補不回來。
# REST 0.5 秒輪詢，在「幾秒衝 2~3%」的針上只有幾個樣本，原理上吃不到頂部。
# WS trades 是逐筆推送，峰值精確。
# 【旁路原則】WS 掛掉不影響交易：自動降級回 REST 輪詢，
# 而且 OKX 上的 OCO 始終存在 —— 就算整個 bot 死掉，靜態SL 仍然守著，不會裸倉。
WS_PUB_URL = "wss://ws.okx.com:8443/ws/v5/public"
WS_PRI_URL = "wss://ws.okx.com:8443/ws/v5/private"

WS_PX   = {}        # iid -> (Decimal 價格, epoch 時間戳)
WS_LIVE = {"pub": False, "pri": False}
WS_WANT = set()     # 目前需要訂閱的 iid
WS_WAKE = {}        # iid -> asyncio.Event，私有頻道有成交/持倉異動時觸發，讓 loop 立刻醒來
_WS_AVAILABLE = None

def _ws_lib():
    """延遲匯入 websockets：沒裝也不能讓腳本起不來（旁路原則）。"""
    global _WS_AVAILABLE
    if _WS_AVAILABLE is None:
        try:
            import websockets  # noqa
            _WS_AVAILABLE = True
        except Exception:
            _WS_AVAILABLE = False
            print("[WS] 未安裝 websockets 套件 → 全程使用 REST 輪詢（功能正常，峰值精度較低）")
    if not _WS_AVAILABLE:
        return None
    import websockets as _w
    return _w

def ws_wake(iid):
    ev = WS_WAKE.get(iid)
    if ev is None:
        ev = WS_WAKE[iid] = asyncio.Event()
    return ev

async def get_px(iid):
    """取現價：WS 有新鮮報價就用它（零延遲、不吃REST額度），否則回退 REST。"""
    v = WS_PX.get(iid)
    if v and (time.time() - v[1]) < 3.0:
        return v[0]
    return await get_last(iid)

def _ws_push_px(iid, px):
    """WS 收到價格：更新快取，並【立刻】更新所有相關策略的峰值。
    峰值追蹤在這裡（逐筆、零成本），SL 送單在 frame_mover（受速率限制）——
    這就是『追蹤與送單分離』：永遠不漏頂，但 API 用量可控。"""
    try:
        d = Decimal(str(px))
    except Exception:
        return
    WS_PX[iid] = (d, time.time())
    for S in list(STRATS.values()):
        try:
            if S.get("spec", {}).get("iid") != iid or S.get("pair_state", "idle") == "idle":
                continue
            _update_peak(S, d)
        except Exception:
            pass

def _update_peak(S, px):
    """更新兩邊的峰值與谷底。只有已成交的那邊才追蹤。
      峰值 = 最大【有利】偏移（MFE）→ 緊貼SL 就是跟著它走
      谷底 = 最大【不利】偏移（MAE）→ 事後判斷靜態SL 是不是設太窄
    兩個都要留：MAE 貼近 SL 代表這一單一路都在 SL 邊緣求生。"""
    d = S["dir"]
    bd = S.get("back_d", "S" if d == "L" else "L")
    for side, sd in (("front", d), ("back", bd)):
        if not S.get(f"{side}_filled"):
            continue
        pk = S.get(f"{side}_peak")
        if pk is None:
            S[f"{side}_peak"] = str(px)
        else:
            o = Decimal(str(pk))
            if (sd == "L" and px > o) or (sd == "S" and px < o):
                S[f"{side}_peak"] = str(px)
        tr = S.get(f"{side}_trough")
        if tr is None:
            S[f"{side}_trough"] = str(px)
        else:
            o = Decimal(str(tr))
            if (sd == "L" and px < o) or (sd == "S" and px > o):
                S[f"{side}_trough"] = str(px)

async def ws_public_task():
    """公有頻道：訂閱 trades（逐筆成交），持續更新價格與峰值。"""
    wsl = _ws_lib()
    if not wsl:
        return
    while not SHUTTING_DOWN:
        try:
            async with wsl.connect(WS_PUB_URL, ping_interval=20, ping_timeout=10) as ws:
                WS_LIVE["pub"] = True
                subbed = set()
                print("[WS] 公有頻道已連線")
                while not SHUTTING_DOWN:
                    want = set(WS_WANT)
                    add = want - subbed
                    rm  = subbed - want
                    if add:
                        await ws.send(json.dumps({"op": "subscribe",
                            "args": [{"channel": "trades", "instId": i} for i in add]}))
                        subbed |= add
                    if rm:
                        await ws.send(json.dumps({"op": "unsubscribe",
                            "args": [{"channel": "trades", "instId": i} for i in rm]}))
                        subbed -= rm
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    m = json.loads(raw)
                    if m.get("arg", {}).get("channel") == "trades":
                        iid = m["arg"].get("instId")
                        for t in (m.get("data") or []):
                            _ws_push_px(iid, t.get("px"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[WS] 公有頻道斷線，3秒後重連", type(e).__name__, e)
        WS_LIVE["pub"] = False
        await asyncio.sleep(3)

async def ws_private_task():
    """私有頻道：訂閱 orders + positions。收到異動就叫醒 loop 立刻回查 OKX。
    【設計】WS 事件只當『鬧鐘』用，真相一律回查 REST —— WS 漏訊也不會誤判。"""
    wsl = _ws_lib()
    if not wsl:
        return
    while not SHUTTING_DOWN:
        try:
            async with wsl.connect(WS_PRI_URL, ping_interval=20, ping_timeout=10) as ws:
                ts = str(int(time.time()))
                # 欄位名必須與 api() 用的同一組（accounts.env 的實際鍵名）
                sgn = base64.b64encode(hmac.new(
                    ACC[f"OKX_{ACCT}_SECRET"].encode(),
                    (ts + "GET" + "/users/self/verify").encode(),
                    hashlib.sha256).digest()).decode()
                await ws.send(json.dumps({"op": "login", "args": [{
                    "apiKey": ACC[f"OKX_{ACCT}_API_KEY"],
                    "passphrase": ACC[f"OKX_{ACCT}_PASSPHRASE"],
                    "timestamp": ts, "sign": sgn}]}))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                if json.loads(raw).get("event") != "login":
                    print("[WS] 私有頻道登入失敗", raw); raise RuntimeError("login fail")
                await ws.send(json.dumps({"op": "subscribe", "args": [
                    {"channel": "orders",    "instType": "SWAP"},
                    {"channel": "positions", "instType": "SWAP"}]}))
                WS_LIVE["pri"] = True
                print("[WS] 私有頻道已連線")
                while not SHUTTING_DOWN:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    m = json.loads(raw)
                    ch = m.get("arg", {}).get("channel")
                    if ch in ("orders", "positions"):
                        for o in (m.get("data") or []):
                            iid = o.get("instId")
                            if iid:
                                ws_wake(iid).set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[WS] 私有頻道斷線，3秒後重連", type(e).__name__, e)
        WS_LIVE["pri"] = False
        await asyncio.sleep(3)

def ws_status():
    if _WS_AVAILABLE is False:
        return "REST輪詢（未安裝websockets）"
    p = "✅" if WS_LIVE["pub"] else "❌"
    r = "✅" if WS_LIVE["pri"] else "❌"
    return f"公有{p} 私有{r}"


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
# ---------- 持倉查詢：全域共用快取（#27 速率控管） ----------
# 【為什麼需要】OKX 對 /api/v5/account/positions 的限制是 10 次 / 2 秒。
# 舊版 loop 與 frame_mover 各自為 long/short 各查一次 → 8 次/秒 = 16 次/2秒，
# 超限 60%，實測噴滿 50011 Too Many Requests。
# 而這個端點【一次就回傳所有持倉】，分兩次查 long/short 是純粹浪費。
# 改成一次查詢、快取 POS_TTL 秒，兩個方向、兩個任務、所有幣種共用同一份。
POS_TTL   = 0.4
POS_CACHE = {"t": 0.0, "data": None, "cool": 0.0}
_POS_LOCK = None
POS_COOL  = 2.0     # 撞到 50011 後的冷卻秒數：期間不再 force，避免超限自我延續

async def _positions_fetch(force=False):
    """回 (ok, data)。ok=False 代表【查不到答案】（速率限制或網路錯誤），
    絕對不可以解讀成『沒有倉位』—— 這正是昨晚假出場災情的根源。"""
    global _POS_LOCK
    if _POS_LOCK is None:
        _POS_LOCK = asyncio.Lock()
    now = time.time()
    # 冷卻期間一律不強制刷新：撞到速率限制後還猛打，只會讓限制一直續命
    if now < POS_CACHE["cool"]:
        force = False
    if not force and POS_CACHE["data"] is not None and (now - POS_CACHE["t"]) < POS_TTL:
        return True, POS_CACHE["data"]
    async with _POS_LOCK:
        now = time.time()
        # 等鎖期間別人剛更新過就直接用，避免同一瞬間重複打 API
        if POS_CACHE["data"] is not None and (now - POS_CACHE["t"]) < POS_TTL:
            return True, POS_CACHE["data"]
        r = await api("GET", "/api/v5/account/positions")
        if r.get("code") != "0":
            if str(r.get("code")) == "50011":
                POS_CACHE["cool"] = time.time() + POS_COOL
            print(f"[持倉查詢異常] code={r.get('code')} msg={r.get('msg')} → 本輪判定為「未知」，不做任何出場認定")
            return False, None
        data = r.get("data") or []
        POS_CACHE["t"] = time.time()
        POS_CACHE["data"] = data
        return True, data


async def okx_pos_ex(iid, ps, force=False):
    """回 (ok, pos)。ok=False = 查詢失敗（未知），pos=None 且 ok=True = 確實沒倉。"""
    ok, data = await _positions_fetch(force)
    if not ok:
        return False, None
    for p in data:
        if p.get("instId") == iid and p.get("posSide") == ps:
            try:
                if float(p.get("pos") or 0) != 0:
                    return True, p
            except Exception:
                pass
    return True, None


async def okx_pos(iid, ps, force=False):
    """相容介面：只回持倉本身。需要分辨「沒倉」與「查不到」時請用 okx_pos_ex。"""
    ok, p = await okx_pos_ex(iid, ps, force)
    return p if ok else None

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


async def close_record(iid, ps, after_ms, tries=8, gap=0.5):
    """出場後取 OKX 真實平倉紀錄。
    【#25】這個查詢一律在【背景】執行，絕不可擋住 loop —— 實測 2026-09-19，
    兩次假出場讓它在主迴圈裡連續阻塞 21 秒，期間監控完全失明，
    B 單真正的出場就掉在那個空窗裡，沒有任何通知。"""
    for i in range(tries):
        r = await api("GET", f"/api/v5/account/positions-history?instType=SWAP&instId={iid}&limit=10")
        if r.get("code") == "0":
            for p in (r.get("data") or []):
                if p.get("posSide") == ps and int(p.get("uTime") or 0) >= after_ms:
                    return p
        await asyncio.sleep(gap)
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
    """批次修改 algo 單觸發價。
    items 元素可為 3 元組 (instId, algoId, sl) → 只改 SL（移動SL 用，TP 不動）；
    或 4 元組 (instId, algoId, sl, tp) → SL/TP 一起改（依實際成交價校正用）。
    OKX 一次最多 10 筆，超過自動分批。

    【問題10】回傳「成功的 algoId 集合」，不是筆數。
    舊版用一個布林判定整批成敗：同批任一張失敗，OKX 回 code!=0，
    其餘明明成功的也被標成失敗 → 程式內部 SL 值與 OKX 實際值失聯。
    改成逐筆看 data[i].sCode，各自認帳。"""
    okset = set()
    for i in range(0, len(items), 10):
        batch = items[i:i+10]
        body = []
        for it in batch:
            e = {"instId": it[0], "algoId": it[1], "newSlTriggerPx": str(it[2])}
            if len(it) >= 4 and it[3] is not None:
                e["newTpTriggerPx"] = str(it[3])
            body.append(e)
        r = await api("POST", "/api/v5/trade/amend-algos", body)
        data = r.get("data") or []
        for j, it in enumerate(batch):
            d = data[j] if j < len(data) else {}
            if str(d.get("sCode", "")) == "0":
                okset.add(it[1])
            else:
                print(f"amend fail {it[0]} {it[1]} sCode={d.get('sCode')} {d.get('sMsg')}")
        if r.get("code") not in ("0", "2") and not data:
            print("amend_frames 整批失敗", r.get("msg"))
    return okset

def hug_sl(entry_px, peak, d, F, tick, static_sl):
    """【v3.1 唯一的 SL 公式】峰值緊貼，棘輪不後退。

    做L：峰值 = 進場後最高價（初始 = 進場價）
         若 峰值 >= 進場價×(1+F) → SL = 峰值×(1-F)；否則不動（靜態SL緩衝區保留）
    做S：鏡像（谷值 = 最低價，SL = 谷值×(1+F)）

    這一式自然涵蓋原本規劃的三個階段，不是近似：
      峰值 = 進場價×(1+F)   → SL 落在進場價        → 保本
      峰值 = 進場價×(1+2F)  → SL 落在進場價×(1+F)  → 淨損益歸零
      峰值再往上            → SL 一路緊貼 F 的距離  → 鎖住獲利
    回傳新 SL（Decimal）或 None（不該動）。
    """
    try:
        e = Decimal(str(entry_px)); pk = Decimal(str(peak))
        cur = Decimal(str(static_sl))
    except Exception:
        return None
    if e <= 0 or pk <= 0:
        return None
    if d == "L":
        if pk < e * (Decimal("1") + F):        # 尚未達啟動門檻 → 靜態SL 緩衝區完整保留
            return None
        nsl = align(pk * (Decimal("1") - F), tick, "S")
        return nsl if nsl > cur else None      # 棘輪：只准往上，絕不後退
    else:
        if pk > e * (Decimal("1") - F):
            return None
        nsl = align(pk * (Decimal("1") + F), tick, "L")
        return nsl if nsl < cur else None      # 棘輪：只准往下


def tf_hug_sl(cur_px, d, F, tick, static_sl):
    """【TF 逼倉】單邊持倉且無獲利時，把 SL 貼到現價 ∓F，把這單逼出戰場。
    沒有對沖的單邊倉不該久留 —— 夜長夢多，寧可認手續費水位的虧損。
    一樣受棘輪保護：只會讓 SL 更靠近現價，不會放鬆。"""
    try:
        cur = Decimal(str(cur_px)); old = Decimal(str(static_sl))
    except Exception:
        return None
    if cur <= 0:
        return None
    if d == "L":
        nsl = align(cur * (Decimal("1") - F), tick, "S")
        return nsl if nsl > old else None
    else:
        nsl = align(cur * (Decimal("1") + F), tick, "L")
        return nsl if nsl < old else None


# 【v3.7】緊貼距離改回【固定值】。
# 自動版（均幅×1.5）算出 DOGE 0.134%／SUI 0.347%／WIF 0.242%，實測鎖利太少：
# WIF 峰值 +0.6% 只鎖到 +0.358%，回吐 0.242%；固定 0.1% 可鎖到 +0.500%。
# 均幅仍照算並寫進交易紀錄（/tune 分析要用），只是不再決定緊貼距離。
HUG_FIXED   = Decimal("0.001")  # 緊貼距離 0.1%（A/B 共用）← 要調就改這一行
HUG_K       = Decimal("1.5")   # 均幅係數（目前僅供 /tune 參考，不影響交易）
HUG_BARS    = 60               # 取樣根數
HUG_TTL     = 300              # 算完快取幾秒（每次部署會重算，行情變了自己跟上）
_HUG_CACHE  = {}               # iid -> (振幅Decimal, 計算時間)


async def auto_hug(iid, spec, sl_pct, ref_px):
    """依該幣種【自己的波動】算出緊貼距離 —— 不用參數，也不是寫死的常數。

    緊貼距離要對抗的是「行情走到一半的正常回撤」，而每個幣種的回撤幅度差很多：
    同樣設 0.15%，DOGE 是 13 檔、XRP 是 37 檔，抗雜訊能力差三倍。
    所以既不該由人輸入，也不該全域寫死 —— 用該幣種近期 1 分K 的平均振幅推算。

      原始值 = 近60根1分K平均振幅 × HUG_K
      上限  = SL% ÷ 2   （比靜態SL的一半還寬就失去收緊的意義）
      下限  = MIN_HUG_TICKS 檔（低於此會被買賣價差直接掃掉）
    另有一道「不得低於該單手續費率」的下限，在 hug_pct() 依 A/B 各自套用。

    回傳 (緊貼距離Decimal, 診斷字串)。取不到K線時回 (None, 原因)，
    呼叫端沿用手續費率，不影響交易。"""
    now = time.time()
    c = _HUG_CACHE.get(iid)
    amp = c[0] if (c and (now - c[1]) < HUG_TTL) else None
    if amp is None:
        try:
            r = await pub(f"/api/v5/market/candles?instId={iid}&bar=1m&limit={HUG_BARS}")
            vals = []
            for k in (r.get("data") or []):
                try:
                    hi = Decimal(str(k[2])); lo = Decimal(str(k[3])); cl = Decimal(str(k[4]))
                    if cl > 0:
                        vals.append((hi - lo) / cl)
                except Exception:
                    continue
            if vals:
                amp = sum(vals) / len(vals)
                _HUG_CACHE[iid] = (amp, now)
        except Exception as e:
            print("自動緊貼取K線失敗", iid, type(e).__name__, e)
    if amp is None:
        return None, "取不到K線，沿用手續費率"

    val  = amp * HUG_K
    note = f"1分K均幅{float(amp * 100):.4f}%×{HUG_K}"
    # 【#42】上限原本是 SL½，但那會把緊貼壓到【比一根平均1分K還窄】——
    # 實測 2026-09-20 SUI：均幅0.2313%，SL 0.4% → 上限0.200% = 0.86×均幅，
    # 結果被一次 0.219%（不到一根均K）的普通回撤掃掉，接著行情續走，
    # 落難方吃滿靜態SL，整場多賠 0.36%。
    # 改成 SL×0.9（只要不超過靜態SL 就有意義），並加一道「不得低於 1×均幅」的硬下限。
    cap = sl_pct * Decimal("0.9")
    if val > cap:
        val = cap; note += f"→受SL上限{float(cap * 100):.3f}%"
    if val < amp:
        val = amp; note += f"→受1×均幅下限{float(amp * 100):.3f}%"
    if val > sl_pct * Decimal("0.9"):
        note += "｜⚠️SL過窄"
    try:
        floor_tick = (spec["tick"] * MIN_HUG_TICKS) / Decimal(str(ref_px))
        if val < floor_tick:
            val = floor_tick; note += f"→受{MIN_HUG_TICKS}檔下限"
    except Exception:
        pass
    return val, note


def hug_pct(S, side):
    """該單實際使用的緊貼距離 —— v3.7 起為固定值 HUG_FIXED，A/B 共用。

    只保留一道 tick 地板：低於 MIN_HUG_TICKS 檔會被買賣價差直接掃掉。
    【取捨】0.1% 低於 B 單的來回手續費 0.120%，B 若剛好在啟動門檻附近被掃
    會小虧約 0.02%。這是為了多鎖利刻意接受的代價。"""
    F = HUG_FIXED
    try:
        # 參考價各用各的：A 用 front_px、B 用 back_px，不可互相借用
        ref = Decimal(str(S.get("front_px" if side == "front" else "back_px") or 0))
        if ref > 0:
            floor_pct = (S["spec"]["tick"] * MIN_HUG_TICKS) / ref
            if floor_pct > F:
                return floor_pct
    except Exception:
        pass
    return F


def hug_display(spec, ref_px, F):
    """把緊貼距離換算成「x%（n檔）」給 TG 顯示用。"""
    try:
        ref = Decimal(str(ref_px))
        n = int(((ref * F) / spec["tick"]).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return f"{F * 100:.3f}%（{n}檔）"
    except Exception:
        return f"{F * 100:.3f}%"


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
    """掛出 A限價 + B觸發 一對，更新 S 的掛單欄位。輪次 +1，日期歸零判斷。

    【v3.1 幾何】兩單間距 gap 直接量 A 與 B 的進場價距離（gap=0 → 同價，完全對沖）：
        做L：A埋伏 = 現價×(1-offset)；B觸發 = A埋伏×(1-gap)
        做S：A埋伏 = 現價×(1+offset)；B觸發 = A埋伏×(1+gap)
    A 死時 B 的毛利 = SL% - gap%，所以 gap 越小、生還方保護越早啟動、最壞損失越小。
    【移除】120 秒 TF 等待 —— 戰役結束就立刻重新取價部署，不看 K 線節奏。
    """
    today = today8()
    if S.get("round_date") != today:
        S["round_today"] = 0
        S["enter_today"] = 0
        S["round_date"]  = today
    S["round_today"] = int(S.get("round_today", 0)) + 1
    d = S["dir"]

    # ── 掛單前完整檢查：掛單 + 持倉 都必須為 0 才准部署新戰役 ──
    # 【不阻塞】只檢查一次就返回，絕不在此等待 —— loop 是主迴圈，
    # 讓它替 _place_pair 站崗會使進場/出場偵測全部停擺。
    # 【#33 保險絲】任一邊仍標記為「已成交」→ 代表還有單沒結清，絕不准部署。
    # 舊版 _place_pair 無條件重置旗標，把「已平倉但還沒回報出場」的單直接抹掉，
    # 那一單的損益就此消失（實測第69輪 A 單）。這道保險絲讓它不可能再發生。
    if S.get("front_filled") or S.get("back_filled"):
        print(f"[拒絕部署] {S['sym']} 仍有未結清的單 "
              f"(A={bool(S.get('front_filled'))} B={bool(S.get('back_filled'))})")
        return False

    _iid0 = S["spec"]["iid"]
    await cancel_all_orders(_iid0)          # 只撤進場委託，保留持倉保護傘
    clear, n_ord, n_pos = await _field_is_clear(_iid0)
    if not clear:
        print(f"[部署前未清空] {S['sym']} 掛單={n_ord} 持倉={n_pos} → 本輪不部署，稍後重試")
        return False

    back_d = "S" if d == "L" else "L"
    spec   = S["spec"]
    tick   = spec["tick"]
    lev    = Decimal(str(S["lev"]))
    margin = Decimal(str(S["margin"]))
    sl_pct = Decimal(str(S["sl"])) / 100
    tp_pct = Decimal(str(S["tp"])) / 100
    offset_pct = Decimal(str(S["offset"])) / 100
    gap_pct    = Decimal(str(S.get("gap", 0))) / 100

    op = await get_px(iid)

    # A 單埋伏價 / 靜態 TP・SL
    if d == "L":
        front_amb       = align(op * (1 - offset_pct), tick, "L")
        front_static_sl = align(front_amb * (1 - sl_pct), tick, "L")
        front_tp        = align(front_amb * (1 + tp_pct), tick, "S")
        back_amb        = align(front_amb * (1 - gap_pct), tick, back_d)
    else:
        front_amb       = align(op * (1 + offset_pct), tick, "S")
        front_static_sl = align(front_amb * (1 + sl_pct), tick, "S")
        front_tp        = align(front_amb * (1 - tp_pct), tick, "L")
        back_amb        = align(front_amb * (1 + gap_pct), tick, back_d)

    # B 單靜態 TP・SL（以 B 自己的觸發價為基準）
    if back_d == "S":
        back_static_sl = align(back_amb * (1 + sl_pct), tick, "S")
        back_tp        = align(back_amb * (1 - tp_pct), tick, "L")
    else:
        back_static_sl = align(back_amb * (1 - sl_pct), tick, "L")
        back_tp        = align(back_amb * (1 + tp_pct), tick, "S")

    sz_front = csize(margin, lev, front_amb, spec["ctval"], spec["lot"])
    sz_back  = csize(margin, lev, back_amb,  spec["ctval"], spec["lot"])

    # 每次部署都依該幣種【當下】的波動重算緊貼距離 —— 行情變了它自己跟上，
    # 不用參數也不寫死。取不到K線就沿用手續費率（hug_pct 會處理）。
    _ha, _hn = await auto_hug(iid, spec, sl_pct, front_amb)
    S["hug_auto"] = _ha or Decimal("0")
    S["hug_note"] = _hn
    _c = _HUG_CACHE.get(iid)
    S["amp"] = str(_c[0]) if _c else ""     # 進場當下的1分K均幅，事後分析的基準
    print(f"[緊貼] {S['sym']} 固定{float(HUG_FIXED*100):.3f}%｜{_hn}")

    front_pos = "long" if d == "L" else "short"
    back_pos  = "long" if back_d == "L" else "short"

    # 清殘單（限價單 + 計劃委託都清）
    await sweep(iid, front_pos)
    await sweep(iid, back_pos)
    await sweep_algos(iid, back_pos)

    front_oid = await _place_limit(iid, front_pos, d, front_amb, sz_front,
                                   front_tp, front_static_sl)
    if not front_oid:
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} A單掛單失敗，稍後重試")
        return False

    back_algo_id = await _place_trigger(iid, back_pos, back_d, back_amb, sz_back,
                                        back_tp, back_static_sl)
    if not back_algo_id:
        await _cancel_order(iid, front_oid)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} B單掛單失敗，稍後重試")
        return False

    S["front_oid"]       = front_oid
    S["front_px"]        = str(front_amb)
    S["front_static_sl"] = str(front_static_sl)
    S["front_tp_px"]     = str(front_tp)
    S["front_sl_px"]     = str(front_static_sl)
    S["front_filled"]    = False
    S["front_ee"]        = None
    S["front_sz"]        = str(sz_front)
    S["front_move_n"]    = 0
    S["front_move_hist"] = []
    S["front_peak"]      = None
    S["front_trough"]    = None
    S["back_algo_id"]    = back_algo_id
    S["back_algo2_id"]   = None
    S["back_px"]         = str(back_amb)
    S["back_amb_px"]     = str(back_amb)
    S["back_static_sl"]  = str(back_static_sl)
    S["back_tp_px"]      = str(back_tp)
    S["back_sl_px"]      = str(back_static_sl)
    S["back_filled"]     = False
    S["back_ee"]         = None
    S["back_sz"]         = str(sz_back)
    S["back_move_n"]     = 0
    S["back_move_hist"]  = []
    S["back_peak"]       = None
    S["back_trough"]     = None
    S["back_d"]          = back_d
    S["algo_id"]         = None
    S["exit_seq"]        = 0
    for k in ("front_exit_reason","front_exit_pnl","back_exit_reason","back_exit_pnl",
              "battle_form","front_move_fail_n","back_move_fail_n"):
        S.pop(k, None)
    S["state"]           = "委託中"
    S["pair_state"]      = "waiting"
    S["battle_t0"]       = time.time()      # 對帳的時間起點
    WS_WANT.add(iid)
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


async def _okx_oco_id(iid, ps, tries=4, gap=0.2):
    """【問題4 配套】向 OKX 索取守護該持倉方向的 OCO(止盈止損) algoId。
    下單時 attachAlgoOrds 已帶 TP/SL，成交當下由 OKX「自動」生成這張 OCO ——
    程式不再自己補掛第二張（那會變成 2 張），改成回頭跟 OKX 要它的 algoId。
    移動SL(amend-algos) 與贏家方向判定(_algo_actual_side) 都要靠這個 ID。"""
    for _ in range(tries):
        try:
            r = await api("GET", f"/api/v5/trade/orders-algo-pending?ordType=oco&instId={iid}")
            if r.get("code") == "0":
                for o in (r.get("data") or []):
                    if o.get("posSide") == ps and o.get("algoId"):
                        return o.get("algoId")
        except Exception as e:
            print("查 OCO algoId 失敗", iid, ps, type(e).__name__, e)
        await asyncio.sleep(gap)
    print(f"[警告] 查不到 OCO algoId {iid} {ps} —— 移動SL 將由守門狗監控")
    return None


def _calc_tp_sl(S, px, side_d):
    """依「實際成交價」px 與該單方向 side_d 重算 TP/SL。
    【問題6】補單是觸發後市價成交，會滑價；用預估觸發價算出的 SL 距離會失真。"""
    tick   = S["spec"]["tick"]
    sl_pct = Decimal(str(S["sl"])) / 100
    tp_pct = Decimal(str(S["tp"])) / 100
    if side_d == "L":
        return (align(px * (1 + tp_pct), tick, "S"), align(px * (1 - sl_pct), tick, "L"))
    return (align(px * (1 - tp_pct), tick, "L"), align(px * (1 + sl_pct), tick, "S"))


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


def tf_expired(S, key):
    """TF 是否剛跨過一根。每個呼叫點各自記自己的索引，互不干擾。"""
    tf_sec = TF_SEC.get(S.get("tf", ACCOUNT_TF), 300)
    idx = int(time.time() // tf_sec)
    if S.get(key) is None:
        S[key] = idx
        return False
    if idx != S[key]:
        S[key] = idx
        return True
    return False


def hug_started(S, side):
    """該單的緊貼是否已啟動（峰值已達 進場價×(1±F) 門檻）。
    啟動 = 這單已經賺過至少一個手續費的幅度，SL 已進入棘輪保護。"""
    filled = S.get("front_filled" if side == "front" else "back_filled")
    if not filled:
        return False
    try:
        e  = Decimal(str(S["front_px" if side == "front" else "back_px"]))
        pk = Decimal(str(S.get("front_peak" if side == "front" else "back_peak") or e))
    except Exception:
        return False
    d = S["dir"] if side == "front" else S.get("back_d", "S" if S["dir"] == "L" else "L")
    F = hug_pct(S, side)
    return pk >= e * (1 + F) if d == "L" else pk <= e * (1 - F)


async def frame_mover(app):
    """【v3.1 移動SL 引擎】追蹤與送單分離。

    追蹤：WS 逐筆推送時已在 _ws_push_px 更新峰值（零成本、永不漏頂）。
          WS 不可用時，這裡每 MOVE_TICK 秒用 REST 補一次。
    送單：每 PRICE_TICK_SEC 秒檢查一次，只有「新SL 優於現有SL」才送 amend。

    TF 判斷樹（只處理持倉相關的三條，零持倉重新部署由 loop 負責）：
      兩單都持倉     → 不動。兩單對沖時損益恆等於 -gap，與價格無關，TF 攪局只會
                       把一個不會惡化的鎖定虧損換成真實虧損，還破壞整個天羅地網。
      單邊持倉有獲利 → 不動。緊貼已啟動，棘輪會處理。
      單邊持倉無獲利 → SL 貼現價 ∓F，逼出戰場。沒有對沖就不該久留。
    """
    await asyncio.sleep(3)
    print(f"原K止損移動任務已啟動（{VERSION}）")
    while True:
        try:
            await asyncio.sleep(MOVE_TICK - (time.time() % MOVE_TICK))
            now_t = time.time()

            candidates = [S for S in list(STRATS.values())
                          if S.get("pair_state", "idle") != "idle"]
            if not candidates:
                continue

            amends = []          # (iid, algoId, nsl, S, side, move_type)
            for S in candidates:
                try:
                    d   = S["dir"]
                    iid = S["spec"]["iid"]
                    bd  = S.get("back_d", "S" if d == "L" else "L")
                    fps = "long" if d  == "L" else "short"
                    bps = "long" if bd == "L" else "short"

                    ok_f, cur_f = await okx_pos_ex(iid, fps)
                    ok_b, cur_b = await okx_pos_ex(iid, bps)
                    if not (ok_f and ok_b):
                        continue        # 【#27】查詢未回應，本輪不動任何 SL

                    # 守門狗：一律查 OKX，不看內部旗標（孤兒倉正是旗標為 False 的那種）
                    # 【#27】守門狗節流：裸倉告警門檻是 15 秒，不需要每 0.5 秒查一次
                    # algo 單（那會多吃 8 次/秒的 orders-algo-pending 額度）。每 3 秒一次足夠。
                    _ng_due = (now_t - float(S.get("_naked_chk_t", 0)) >= 3.0)
                    if _ng_due:
                        S["_naked_chk_t"] = now_t
                    if cur_f:
                        if _ng_due:
                            await _naked_guard(app, S, iid, fps, "A/前單", now_t)
                    else:
                        S.pop(f"_naked_since_{fps}", None); S.pop(f"_naked_alerted_{fps}", None)
                    if cur_b:
                        if _ng_due:
                            await _naked_guard(app, S, iid, bps, "B/後單", now_t)
                    else:
                        S.pop(f"_naked_since_{bps}", None); S.pop(f"_naked_alerted_{bps}", None)

                    if not (cur_f or cur_b):
                        continue

                    # 取價並補更新峰值（WS 活著時這只是保險，峰值早就逐筆更新過了）
                    px = await get_px(iid)
                    if not px:
                        continue
                    _update_peak(S, Decimal(str(px)))

                    both = bool(cur_f and cur_b)
                    tf_hit = tf_expired(S, "_tf_idx_mv")

                    for side, sd, ps, cur_pos, sl_f, aid_f in (
                            ("front", d,  fps, cur_f, "front_sl_px", "algo_id"),
                            ("back",  bd, bps, cur_b, "back_sl_px",  "back_algo2_id")):
                        if not cur_pos:
                            continue
                        if not S.get("front_filled" if side == "front" else "back_filled"):
                            continue
                        if S.get("closing"):
                            continue
                        # 退避：連續失敗後拉長重試間隔，不再每秒打爆速率額度
                        fail_n = int(S.get(f"{side}_move_fail_n", 0) or 0)
                        if fail_n:
                            wait = AMEND_BACKOFF[min(fail_n, len(AMEND_BACKOFF)) - 1]
                            if now_t - float(S.get(f"_last_fail_t_{side}", 0)) < wait:
                                continue
                        if now_t - float(S.get(f"_last_move_t_{side}", 0)) < PRICE_TICK_SEC:
                            continue

                        algo_id = S.get(aid_f)
                        if not algo_id:
                            continue
                        F   = hug_pct(S, side)
                        ent = S["front_px" if side == "front" else "back_px"]
                        pk  = S.get("front_peak" if side == "front" else "back_peak") or ent
                        cur_sl = S.get(sl_f) or S["front_static_sl" if side == "front" else "back_static_sl"]
                        tick = S["spec"]["tick"]

                        nsl = hug_sl(ent, pk, sd, F, tick, cur_sl)
                        mtype = E.MOVE_PROFIT
                        # TF 逼倉：只在「單邊持倉且緊貼未啟動」時動作
                        if nsl is None and tf_hit and not both and not hug_started(S, side):
                            nsl = tf_hug_sl(px, sd, F, tick, cur_sl)
                            mtype = E.MOVE_TIME
                            if nsl is not None:
                                print(f"[TF逼倉] {S['sym']} {side} 現價={px} SL→{nsl}")
                        if nsl is None:
                            continue
                        # 【#23】送單前先跟現價比對。峰值算出的 SL 在價格快速回落時
                        # 可能已越過現價；OKX 對做多要求 SL<最新價、做空要求 SL>最新價，
                        # 硬送會被 51280 拒絕、計入失敗、觸發退避。
                        # 越界就跳過這一輪 —— 既有 SL 仍守著，不是保護空窗。
                        cpx = Decimal(str(px))
                        if (sd == "L" and nsl >= cpx) or (sd == "S" and nsl <= cpx):
                            continue
                        S[f"_pending_{side}"] = (str(nsl), str(pk), mtype)
                        amends.append((iid, algo_id, nsl, S, side, sl_f))
                except Exception as e:
                    print("frame_mover 單策略錯誤", S.get("sym"), type(e).__name__, e)

            if not amends:
                continue

            okset = await amend_frames([(a[0], a[1], a[2]) for a in amends])
            for iid, algo_id, nsl, S, side, sl_f in amends:
                pend = S.pop(f"_pending_{side}", None)
                if not pend:
                    continue
                nsl_s, pk_s, mtype = pend
                hist_key = f"{side}_move_hist"
                mh = S.get(hist_key)
                if not isinstance(mh, list):
                    mh = []; S[hist_key] = mh
                label = f"{S['sym']} {S['dir'] if side == 'front' else S.get('back_d','?')}"
                if algo_id in okset:
                    S[sl_f] = nsl_s
                    S[f"{side}_move_n"] = int(S.get(f"{side}_move_n", 0)) + 1
                    S[f"_last_move_t_{side}"] = now_t
                    S[f"{side}_move_fail_n"] = 0
                    print(f"[SL移動] {label} {side} {mtype} 峰值={pk_s} 新SL={nsl_s} "
                          f"第{S[f'{side}_move_n']}次")
                    mh.append({"t": hhmmss(), "type": mtype, "peak": pk_s, "sl": nsl_s})
                else:
                    S[f"{side}_move_fail_n"] = int(S.get(f"{side}_move_fail_n", 0)) + 1
                    S[f"_last_fail_t_{side}"] = now_t
                    print(f"[SL移動失敗] {label} {side} 峰值={pk_s} 欲改SL={nsl_s} "
                          f"累計失敗{S[f'{side}_move_fail_n']}次")
                    mh.append({"t": hhmmss(), "type": E.MOVE_FAIL, "peak": pk_s, "sl": nsl_s})
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


def _is_position_guard(o):
    """這張 algo 單是不是『正在守護持倉』的止盈止損？
    reduceOnly=true 代表平倉單 —— 它是持倉的保護傘，撤掉就變裸倉。
    【鐵則】清場只撤『尚未成交的進場委託』，絕不撤持倉的保護傘。"""
    if str(o.get("reduceOnly")).lower() == "true":
        return True
    # OCO 一定帶 tp/sl 觸發價；trigger 進場單不帶
    if o.get("tpTriggerPx") or o.get("slTriggerPx"):
        return True
    return False


async def cancel_all_orders(iid=None, pos_side=None, tries=5, keep_guards=True):
    """撤光該幣種（或該方向）的『進場委託單』：普通單 + trigger。
    keep_guards=True（預設）時，保留守護持倉的 OCO/止盈止損 —— 絕不製造裸倉。
    確認 OKX 回報 0 張（不含保護傘）才返回 True。絕不碰持倉本身。"""
    for _ in range(tries):
        orders, algos = await list_all_orders(iid, pos_side)
        if keep_guards:
            algos = [o for o in algos if not _is_position_guard(o)]
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


# 【設計原則】損益一律直接讀 OKX，程式絕不自己加減。
# 舊版有 _get_net_pnl / _query_open_fee 兩個輔助函式，會把「開倉手續費」
# 再加到 OKX 給的 fee / realizedPnl 上 —— 但 positions-history 的 fee 欄位
# 本來就已經是【開倉+平倉】的累計值，補加就變成重複計算
# （實測 2026-09-20：真實 -0.00061584，被算成 -0.000791）。
# 兩個函式已全數移除，避免日後又被誤用。程式只做一件事：把金額換算成百分比。

def _move_stat(mhist):
    """統計三類移動次數：✅獲利推進 / ⏱️時間小移動 / 🚫API失敗。"""
    p = t = f = 0
    for m in (mhist or []):
        ty = m.get("type", "")
        if ty == E.MOVE_PROFIT:  p += 1
        elif ty == E.MOVE_TIME:  t += 1
        elif ty == E.MOVE_FAIL:  f += 1
    return p, t, f


def _sl_block(mn, mhist, fn=0):
    """SL 移動明細區塊。
    【v3.1 改】顯示「峰值」而非「現價」—— 新公式 SL = 峰值∓F，SL 停在哪裡
    完全由峰值決定。顯示現價會看不懂 SL 為何不動（現價跌了但峰值沒變）。
    只列最近 10 筆：出場價一定由最後一筆 SL 決定，早期那幾筆參考價值低。"""
    p, t, f = _move_stat(mhist)
    head = f"SL移動 {mn} 次"
    if p or t or f:
        head += f"（{E.MOVE_PROFIT}{p} {E.MOVE_TIME}{t} {E.MOVE_FAIL}{f}）"
    lines = ["━━━━━━━━━━", head]
    if mhist:
        for m in mhist[-10:]:
            pk = m.get("peak", m.get("px", ""))
            lines.append(f"{m.get('t','')} | {m.get('type','')} | 峰值{pk} | 止{m.get('sl','')}")
        if len(mhist) > 10:
            lines.append(f"（顯示最近10筆，共{len(mhist)}筆）")
    return "\n".join(lines)


def _exit_snapshot(S, side, seq):
    """把出場當下需要的所有欄位【複製】一份。
    【#25】記帳改到背景執行後，主迴圈可能已經重新部署並重置 S 的欄位；
    不先快照就會用到新戰役的資料去算舊戰役的損益。"""
    pre = "front" if side == "front" else "back"
    d = S["dir"] if side == "front" else S.get("back_d", "S" if S["dir"] == "L" else "L")
    return {
        "side": side, "pre": pre, "name": "A單" if side == "front" else "B單",
        "dir": d, "sym": S["sym"], "iid": S["spec"]["iid"], "tick": S["spec"]["tick"],
        "pos_side": "long" if d == "L" else "short",
        "lev": S.get("lev"), "margin": S.get("margin"), "seq": seq,
        "entry_px": S.get(f"{pre}_px"), "entry_ee": S.get(f"{pre}_ee"),
        "tp_px": S.get(f"{pre}_tp_px"), "static_sl": S.get(f"{pre}_static_sl"),
        "last_sl": S.get(f"{pre}_sl_px"), "peak": S.get(f"{pre}_peak"),
        "move_n": int(S.get(f"{pre}_move_n", 0)),
        "move_hist": list(S.get(f"{pre}_move_hist") or []),
        "round": S.get("round_today", "-"),
        "hug": hug_pct(S, pre), "hug_note": S.get("hug_note", ""),
        "spec": S["spec"],
        # ── 以下純為事後分析保存，交易邏輯不使用 ──
        "tid": f"{int(time.time()*1000)}-{S['sym']}-{'A' if side=='front' else 'B'}",
        "ab": "A" if side == "front" else "B",
        "strat_dir": S.get("locked_dir", S["dir"]),
        "trough": S.get(f"{pre}_trough"),
        "amp": S.get("amp", ""),
        "sl_set": S.get("sl"), "tp_set": S.get("tp"),
        "gap_set": S.get("gap"), "offset_set": S.get("offset"),
        "hug_note_full": S.get("hug_note", ""),
    }


def _exit_reason(snap, close_px):
    """判定出場原因：TP / 靜態SL / 緊貼SL / TF逼倉 / 手動。"""
    try:
        cp = Decimal(str(close_px))
        tol = snap["tick"] * 3
        tp  = Decimal(str(snap.get("tp_px") or 0))
        ssl = Decimal(str(snap.get("static_sl") or 0))
        lsl = Decimal(str(snap.get("last_sl") or ssl))
    except Exception:
        return "手動/其他"
    if tp and abs(cp - tp) <= tol:
        return "TP"
    if abs(cp - ssl) <= tol and abs(lsl - ssl) <= tol:
        return "靜態SL"
    if abs(cp - lsl) <= tol:
        last_ok = None
        for m in reversed(snap.get("move_hist") or []):
            if m.get("type") != E.MOVE_FAIL:
                last_ok = m; break
        if last_ok and last_ok.get("type") == E.MOVE_TIME:
            return "TF逼倉"
        return "緊貼SL"
    return "手動/其他"


def _peak_gain(snap):
    """這一單在場期間的最大浮動獲利%（供 /summary 判斷緊貼鬆緊）。"""
    try:
        e = Decimal(str(snap.get("entry_px")))
        p = Decimal(str(snap.get("peak") or e))
        g = (p - e) / e * 100 if snap["dir"] == "L" else (e - p) / e * 100
        return float(g)
    except Exception:
        return 0.0


def _peak_line(snap):
    """峰值顯示行（獨立一行，避免訊息過寬）。"""
    ent = snap.get("entry_px"); pk = snap.get("peak") or ent
    if not ent or not pk:
        return "峰值：-"
    try:
        e = Decimal(str(ent)); p = Decimal(str(pk))
        gain = (p - e) / e * 100 if snap["dir"] == "L" else (e - p) / e * 100
        return f"峰值：{p}（{gain:+.3f}%）"
    except Exception:
        return f"峰值：{pk}"


async def _build_exit_msg(snap, rec):
    """組出場通知全文（從快照，不碰 S）。回傳 (訊息, 毛, 費, 淨, 原因)。"""
    d = snap["dir"]
    mv = float(Decimal(str(snap.get("margin", "1"))))
    # 【#30】不再查 OKX 即時掛單。記帳是背景執行的，那時新戰役的掛單早就掛上去了，
    # live 查詢會抓到【下一場】的 TP/SL（實測第63輪顯示 0.09086/0.08701，
    # 那是新 A 埋伏價 0.08736 算出來的，不是這一單的）。一律用進場當下的快照。
    static_tp = snap.get("tp_px", "-")
    static_sl = snap.get("static_sl", "-")
    ee = snap.get("entry_ee")
    entry_t = datetime.fromtimestamp(float(ee), TZ8).strftime("%H:%M:%S") if ee else "-"

    if rec:
        # 【#39】OKX positions-history 的 fee 欄位【已經是整個持倉的累計手續費】
        # （開倉 + 平倉），realizedPnl 同樣已扣除。舊版又把開倉費加一次 → 重複計算。
        # 實測 2026-09-20 第63輪：開倉費 -0.00017544、平倉費 -0.00044040，
        # 真實總費 -0.00061584，TG 卻顯示 -0.000791，正好多算一次開倉費。
        g_r   = Decimal(str(rec.get("pnl") or "0"))
        fee_r = Decimal(str(rec.get("fee") or "0"))
        net_r = Decimal(str(rec.get("realizedPnl") or "0"))
        xpx   = str(rec.get("closeAvgPx") or "-")
    else:
        g_r = fee_r = net_r = Decimal("0")
        xpx = "-"

    reason = _exit_reason(snap, xpx) if xpx != "-" else "查無平倉紀錄"
    g_pct   = float(g_r)   / mv * 100 if mv else 0
    fee_pct = float(fee_r) / mv * 100 if mv else 0
    net_pct = float(net_r) / mv * 100 if mv else 0
    ico = E.WIN if net_r >= 0 else E.LOSS

    msg = (
        f"{E.BOT} OKX原K｜{ACCT}\n"
        f"事件：{ico} {snap['name']}出場成交（本場第{snap['seq']}個出場）\n"
        f"━━━━━━━━━━\n"
        f"商品：{E.dir_emoji(d)} {snap['sym']} {E.dir_word(d)} {snap.get('lev')}x {snap.get('margin')}\n"
        f"出場原因：{reason}\n"
        f"━━━━━━━━━━\n"
        f"進場：{snap.get('entry_px','-')} | {entry_t}\n"
        f"{_peak_line(snap)}\n"
        f"靜態TP：{static_tp}\n"
        f"靜態SL：{static_sl}\n"
        f"最後SL：{snap.get('last_sl','-')}\n"
        f"緊貼：{hug_display(snap['spec'], snap.get('entry_px') or 0, snap.get('hug') or Decimal('0'))}（固定）\n"
        f"出場：{xpx} | {hhmmss()}\n"
        f"━━━━━━━━━━\n"
        f"毛損益：{g_r:+.6f} ({g_pct:+.3f}%)\n"
        f"手續費：{fee_r:.6f} ({fee_pct:+.3f}%)\n"
        f"淨損益：{net_r:+.6f} ({net_pct:+.3f}%) {ico}\n"
        f"{_sl_block(snap['move_n'], snap['move_hist'])}"
    )
    return msg, g_r, fee_r, net_r, reason


def _battle_form(S):
    """戰役形態分類，供 /summary 統計最佳兩單間距用。"""
    a = S.get("front_exit_reason") is not None
    b = S.get("back_exit_reason") is not None
    if a and b:
        if S.get("front_exit_reason") == "靜態SL" and S.get("back_exit_reason") == "靜態SL":
            return "雙殺"
        return "雙邊成交"
    if a:
        return "A單獨成交"
    if b:
        return "B單獨成交"
    return "未成交"


async def _confirm_gone(iid, ps, tries=3, gap=0.3):
    """【#20/#21】確認持倉是真的不見了，不是查詢雜訊。

    OKX /api/v5/account/positions 有讀取一致性延遲：剛成交的倉位可能短暫查不到，
    而且回的是 code=0 的正常回應（沒有任何錯誤碼），okx_pos 無從分辨。
    實測 2026-09-19：A、B 兩個倉在 0.5 秒內「同時消失」，21 秒後又「回來」，
    期間程式已經宣告戰役結束、撤光掛單、把旗標清空 —— 一次空讀滾成全面災情。

    連續 tries 次都查不到才算數。回傳 True = 確實已平倉。

    【#27】查詢失敗（50011 速率限制等）一律視為「沒出場」——
    持續性超限會讓三次查詢一致失敗，若把失敗當成空倉，三重確認反而會
    「一致同意」倉位不見了，比單次誤判更危險。寧可漏判，不可誤判。"""
    for i in range(tries):
        ok, p = await okx_pos_ex(iid, ps, force=True)
        if not ok:
            print(f"[出場判定中止] {iid} {ps} 查詢未回應，不認定為出場")
            return False
        if p:
            return False
        if i < tries - 1:
            await asyncio.sleep(gap)
    return True


def _pct_move(a, b, d):
    """從 a 到 b，依方向 d 換算成有利百分比（做S 反向）。"""
    try:
        a = Decimal(str(a)); b = Decimal(str(b))
        if a <= 0:
            return 0.0
        return float(((b - a) / a * 100) if d == "L" else ((a - b) / a * 100))
    except Exception:
        return 0.0


def _trade_record(snap, reason, g_r, fee_r, net_r, rec, ee):
    """把這一單的一切留下來 —— 凡走過必留下痕跡。

    交易邏輯一概不讀這些欄位，它們只為了事後兵推：
      amp / sl_x / hug_x  把不同幣種放到同一個尺度上比較（絕對%不可比）
      mfe / mae           這一單最好與最壞走到哪，判斷 SL 與緊貼的鬆緊
      moves               【完整】的 SL 移動歷史 —— 舊版只存在記憶體，
                          戰役一結束就永遠消失，這是最該留下的東西
      post5               出場後5分鐘還走多遠，稍後由 _track_post_exit 補上
    """
    d = snap["dir"]
    ent = snap.get("entry_px"); xpx = (rec or {}).get("closeAvgPx")
    nv  = float(Decimal(str(snap.get("margin", "1")))) or 1.0
    try:
        amp = float(Decimal(str(snap.get("amp") or 0)) * 100)
    except Exception:
        amp = 0.0
    hug = float(Decimal(str(snap.get("hug") or 0)) * 100)
    try:
        sl_set = float(snap.get("sl_set") or 0)
    except Exception:
        sl_set = 0.0
    mh = snap.get("move_hist") or []
    n_ok = sum(1 for m in mh if m.get("type") == E.MOVE_PROFIT)
    n_tf = sum(1 for m in mh if m.get("type") == E.MOVE_TIME)
    n_fa = sum(1 for m in mh if m.get("type") == E.MOVE_FAIL)
    return {
        "v": VERSION, "tid": snap.get("tid"), "acct": ACCT,
        "date": today8(), "time": hhmmss(),
        "sym": snap["sym"], "dir": d, "ab": snap.get("ab"),
        "strat_dir": str(snap.get("strat_dir") or ""),
        "reason": reason,
        "entry": str(ent or ""), "exit": str(xpx or ""),
        "peak": str(snap.get("peak") or ""), "trough": str(snap.get("trough") or ""),
        "gross": float(g_r), "fee": float(fee_r), "net": float(net_r), "nv": nv,
        "hold_s": int(time.time() - ee),
        # 參數快照
        "amp": round(amp, 4), "sl": sl_set, "hug": round(hug, 4),
        "tp": float(snap.get("tp_set") or 0), "gap": float(snap.get("gap_set") or 0),
        "offset": float(snap.get("offset_set") or 0),
        "sl_x":  round(sl_set / amp, 2) if amp else 0,
        "hug_x": round(hug / amp, 2) if amp else 0,
        # 走勢
        "mfe": round(_pct_move(ent, snap.get("peak") or ent, d), 4),
        "mae": round(_pct_move(ent, snap.get("trough") or ent, d), 4),
        "post5": None,
        # SL 移動全紀錄
        "move_n": int(snap.get("move_n", 0)),
        "move_ok": n_ok, "move_tf": n_tf, "move_fail": n_fa,
        "moves": mh,
        "battle": snap.get("round"), "ambush_s": 0,
    }


async def _track_post_exit(rec, wait=300):
    """出場後 N 秒，回頭查 1分K：價格往這一單的方向又走了多遠。

    這是判斷緊貼鬆緊的【唯一客觀依據】——
      post5 接近 0  → 出場點抓得準，緊貼合適
      post5 很大    → 出太早了，緊貼太緊，錯過的就是這個數字
    對被靜態SL 掃掉的落難方同樣有意義：出場後價格回頭 = SL 太窄。"""
    try:
        await asyncio.sleep(wait)
        sym = rec.get("sym"); xpx = rec.get("exit")
        if not xpx:
            return
        iid = inst_id(sym)
        r = await pub(f"/api/v5/market/candles?instId={iid}&bar=1m&limit=6")
        rows = r.get("data") or []
        if not rows:
            return
        d = rec.get("dir")
        best = None
        for k in rows:
            try:
                v = Decimal(str(k[2])) if d == "L" else Decimal(str(k[3]))
            except Exception:
                continue
            if best is None or (d == "L" and v > best) or (d == "S" and v < best):
                best = v
        if best is None:
            return
        rec["post5"] = round(_pct_move(xpx, best, d), 4)
        _update_trade(rec.get("date"), rec.get("tid"), {"post5": rec["post5"]})
        print(f"[出場後追蹤] {sym} {rec.get('ab')}單 出場後5分 {rec['post5']:+.3f}%")
    except Exception as e:
        print("出場後追蹤失敗", type(e).__name__, e)


def _update_trade(date, tid, fields):
    """依 tid 回頭補寫某一筆交易紀錄的欄位。"""
    if not (date and tid):
        return
    try:
        fp = trade_file(date)
        arr = json.load(open(fp))
        for r in arr:
            if r.get("tid") == tid:
                r.update(fields); break
        else:
            return
        tmp = fp + ".tmp"
        with open(tmp, "w") as f:
            json.dump(arr, f, default=str); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, fp)
    except Exception as e:
        print("更新交易紀錄失敗", type(e).__name__, e)


async def _reconcile(app, chat, S, iid):
    """【#37】出場對帳：拿 OKX 的平倉紀錄跟程式回報過的比對，把漏掉的補登。

    程式以輪詢偵測進出場，一筆單的完整生老病死可能只有 1 秒（實測第63輪），
    loop 正在處理別的事就會整段錯過 —— 那筆真實損益就永遠不會進帳。
    這是最後一道防線：就算程式漏看，帳也不會錯。"""
    try:
        t0 = float(S.get("battle_t0") or 0)
        if not t0:
            return
        r = await api("GET", f"/api/v5/account/positions-history?instId={iid}&limit=20")
        if r.get("code") != "0":
            return
        done = set(S.get("_reported_closes") or [])
        miss = [p for p in (r.get("data") or [])
                if int(p.get("uTime") or 0) >= int(t0 * 1000)
                and p.get("posId") and p.get("posId") not in done]
        if not miss:
            return
        mv = float(Decimal(str(S.get("margin", "1")))) or 1.0
        for p in miss:
            net = Decimal(str(p.get("realizedPnl") or "0"))
            fee = Decimal(str(p.get("fee") or "0"))
            g   = Decimal(str(p.get("pnl") or "0"))
            ps  = p.get("posSide")
            dd  = "L" if ps == "long" else "S"
            done.add(p.get("posId"))
            print(f"[對帳補登] {S['sym']} {ps} 淨損益={net} posId={p.get('posId')}")
            log_trade({"date": today8(), "sym": S["sym"], "dir": dd, "reason": "補登",
                       "gross": float(g), "fee": float(fee), "net": float(net),
                       "nv": mv, "hold_s": 0, "ambush_s": 0})
            await notify(app, chat,
                f"{E.BOT} OKX原K｜{ACCT}\n事件：{E.WARN} 對帳補登出場\n"
                f"━━━━━━━━━━\n"
                f"商品：{E.dir_emoji(dd)} {S['sym']} {E.dir_word(dd)}\n"
                f"這筆成交程式當時沒偵測到，依 OKX 紀錄補登\n"
                f"━━━━━━━━━━\n"
                f"進場：{p.get('openAvgPx','-')}\n出場：{p.get('closeAvgPx','-')}\n"
                f"毛損益：{g:+.6f}\n手續費：{fee:.6f}\n"
                f"淨損益：{net:+.6f}（{float(net)/mv*100:+.3f}%）{E.pnl_emoji(net)}\n"
                f"時間：{hhmmss()}")
        S["_reported_closes"] = list(done)[-40:]
        save_state()
    except Exception as e:
        print("對帳失敗", type(e).__name__, e)


async def _exit_report(app, chat, S, snaps, tail_fn):
    """【#25】背景記帳：查平倉損益、組訊息、發 TG、寫交易紀錄。
    主迴圈只負責偵測與狀態機，絕不為記帳停下來。"""
    try:
        results = []
        for snap in snaps:
            ee = float(snap.get("entry_ee") or time.time())
            rec  = await close_record(snap["iid"], snap["pos_side"], int(ee * 1000))
            if rec and rec.get("posId"):
                _rc = list(S.get("_reported_closes") or [])
                _rc.append(rec["posId"]); S["_reported_closes"] = _rc[-40:]
            msg, g_r, fee_r, net_r, reason = await _build_exit_msg(snap, rec)
            S[f"{snap['pre']}_exit_reason"] = reason
            S[f"{snap['pre']}_exit_pnl"]    = f"{float(net_r):+.6f}"
            print(f"[{snap['name']}出場] {snap['sym']} {reason} 淨損益={net_r}")
            rec_full = _trade_record(snap, reason, g_r, fee_r, net_r, rec, ee)
            log_trade(rec_full)
            # 出場後 5 分鐘再回頭看價格走去哪 —— 這是判斷「出太早」的唯一依據
            asyncio.create_task(_track_post_exit(rec_full))
            results.append(msg)
        save_state()
        # 【#40】此刻本輪所有 posId 都已登記，對帳才不會誤判成漏看
        try:
            await _reconcile(app, chat, S, S["spec"]["iid"])
        except Exception as e:
            print("對帳呼叫失敗", type(e).__name__, e)
        tail = tail_fn()
        for m in results:
            await notify(app, chat, f"{m}\n{tail}")
    except Exception as e:
        print("背景記帳錯誤", type(e).__name__, e)


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
    # 【#29】改走共用快取，且查詢失敗一律回「未清空」。
    # 舊版查詢失敗時 n_pos 維持 0 → 謊報已清空 → 帶著活倉重新部署。
    # 另外舊版自己打 API，完全繞過速率快取，3 幣種在 TF 邊界會同時各打一次。
    ok, data = await _positions_fetch(force=True)
    if not ok:
        return False, n_ord, -1
    for p in data:
        if p.get("instId") == iid:
            try:
                if float(p.get("pos") or 0) != 0:
                    n_pos += 1
            except Exception:
                pass
    return (n_ord == 0 and n_pos == 0), n_ord, n_pos


async def _ensure_a_oco(S, iid, a_ps, d, fill_px):
    """A 成交後：依【實際成交價】重設 TP/SL，並取得自動生成 OCO 的 algoId。
    A 的母單是 limit，OKX 自動生成的 OCO 可以 amend（實測成功），所以直接改它。"""
    n_tp, n_sl = _calc_tp_sl(S, fill_px, d)
    S["front_tp_px"]     = str(n_tp)
    S["front_static_sl"] = str(n_sl)
    S["front_sl_px"]     = str(n_sl)
    aid = await _okx_oco_id(iid, a_ps)
    S["algo_id"] = aid
    if aid:
        ok = await amend_frames([(iid, aid, n_sl, n_tp)])
        if aid not in ok:
            print(f"[警告] {S['sym']} A單 TP/SL 校正失敗，沿用掛單時的值")
    return aid


async def _ensure_b_oco(S, iid, b_ps, back_d, fill_px):
    """B 成交後：依【實際成交價】重設 TP/SL，並把保護換成一張『自己掛的』OCO。

    【問題9】B 的母單是 trigger 計劃委託，OKX 自動生成的附屬 OCO 不可修改
    （amend-algos 回 51506 Order modification unavailable），實測連續失敗 28 次，
    B 的 SL 從頭到尾一格都沒動過。v3.1 的 SL 是唯一出場機制，這會讓 B 形同裸奔。
    【問題17】B 是觸發後市價成交，必有滑價（實測滑 6 檔），用觸發價算的 SL
    距離會被壓縮（設 0.2% 實際只有 0.137%）。這裡一併用實際成交價重算。

    順序鐵則：【先掛新、後撤舊】。反過來會出現裸倉空窗。
    代價是數百毫秒內同方向有 2 張 OCO —— 是「重複」而非「裸倉」，風險低得多。
    """
    n_tp, n_sl = _calc_tp_sl(S, fill_px, back_d)
    S["back_tp_px"]     = str(n_tp)
    S["back_static_sl"] = str(n_sl)
    S["back_sl_px"]     = str(n_sl)
    old_id = await _okx_oco_id(iid, b_ps, tries=4)
    new_id = await place_algo(iid, b_ps, back_d, S.get("back_sz"), n_tp, n_sl)
    if new_id:
        if old_id and old_id != new_id:
            await cancel_frame(iid, old_id)      # 先確定新的掛上了才撤舊的
        S["back_algo2_id"] = new_id
        print(f"[B保護換單] {S['sym']} 成交={fill_px} TP={n_tp} SL={n_sl} algo={new_id}")
        return True
    S["back_algo2_id"] = old_id                  # 換不成就沿用舊的，至少還有保護
    print(f"[警告] {S['sym']} B單 OCO 換單失敗，沿用自動生成那張（移動SL 可能失效）")
    return False


def _trigger_line(S, side):
    """進場通知用：緊貼的啟動門檻（價格走到這裡緊貼才會開始動）。"""
    pre = "front" if side == "front" else "back"
    d = S["dir"] if side == "front" else S.get("back_d", "S" if S["dir"] == "L" else "L")
    try:
        e = Decimal(str(S[f"{pre}_px"]))
        F = hug_pct(S, pre)
        tick = S["spec"]["tick"]
        thr = align(e * (1 + F), tick, "S") if d == "L" else align(e * (1 - F), tick, "L")
        return str(thr)
    except Exception:
        return "-"


async def _pending_side_note(S, iid, side):
    """戰場狀態用：對手單目前是持倉中 / 等待觸發 / 已出場。"""
    pre = "front" if side == "front" else "back"
    nm  = "A單" if side == "front" else "B單"
    d = S["dir"] if side == "front" else S.get("back_d", "S" if S["dir"] == "L" else "L")
    ps = "long" if d == "L" else "short"
    # 【#28】查不到答案時保守視為「對手還在」—— 寧可晚一輪結束戰役，
    # 也不能因為一次查詢異常就宣告結束、撤光掛單。
    ok_p, pos_p = await okx_pos_ex(iid, ps)
    if (not ok_p) or pos_p:
        pk = S.get(f"{pre}_peak") or S.get(f"{pre}_px") or "-"
        sl = S.get(f"{pre}_sl_px") or S.get(f"{pre}_static_sl") or "-"
        return f"戰場狀態：{nm} 持倉中\n峰值 {pk}｜SL {sl}"
    ords, algos = await list_all_orders(iid)
    for o in (ords + algos):
        if _is_position_guard(o):
            continue
        if o.get("posSide") == ps:
            px = o.get("px") or o.get("triggerPx") or "-"
            return f"戰場狀態：{nm} 等待觸發 @{px}"
    return None


async def loop(app, chat, S):
    # 設計此腳本的目的在於用bot取代我在交易所app上的一切手動行為，切記
    # Loop 只監控進出場成交訊號（持倉），不監控掛單
    spec = S["spec"]
    iid  = spec["iid"]
    k    = skey(S["sym"], S["dir"])

    try:
        WS_WANT.add(iid)
        ok = await _place_pair(S, iid, chat, app, label="首次埋伏")
        if not ok:
            S["pair_state"] = "waiting"
            save_state()

        loop_tick = 0
        while True:
            # WS 私有頻道回報成交/持倉異動 → 立刻醒來；否則睡滿節流間隔。
            # WS 不可用時就是單純的 PRICE_TICK_SEC 輪詢，行為完全一致。
            try:
                await asyncio.wait_for(ws_wake(iid).wait(), timeout=PRICE_TICK_SEC)
                ws_wake(iid).clear()
            except asyncio.TimeoutError:
                pass
            loop_tick += 1
            if S.get("pair_state", "idle") == "idle":
                break

            d      = S["dir"]
            a_side = "long" if d == "L" else "short"
            back_d = S.get("back_d", "S" if d == "L" else "L")
            b_side = "long" if back_d == "L" else "short"
            ok_a, cur_a = await okx_pos_ex(iid, a_side)
            ok_b, cur_b = await okx_pos_ex(iid, b_side)
            if not (ok_a and ok_b):
                # 【#27】查不到答案就整輪跳過。寧可晚 0.5 秒偵測，
                # 也不能拿「未知」去判定進場或出場。
                continue
            a_open = bool(S.get("front_filled"))
            b_open = bool(S.get("back_filled"))

            if loop_tick % 10 == 0:
                print(f"[loop] {S['sym']} {VERSION} "
                      f"A={'倉' if cur_a else '空'}/{'開' if a_open else '待'} "
                      f"B={'倉' if cur_b else '空'}/{'開' if b_open else '待'} "
                      f"ocoA={bool(S.get('algo_id'))} ocoB={bool(S.get('back_algo2_id'))} "
                      f"WS={ws_status()}")

            # ========== A 進場偵測 ==========
            if cur_a and not a_open:
                fpx = Decimal(str(cur_a.get("avgPx") or cur_a.get("last") or S.get("front_px", "0")))
                S["front_filled"] = True
                S["front_px"]     = str(fpx)
                S["front_ee"]     = time.time()
                S["front_peak"]   = str(fpx)      # 峰值起點 = 進場價
                await _ensure_a_oco(S, iid, a_side, d, fpx)
                if S.get("pair_state") == "waiting":
                    S["pair_state"] = "running"
                bump(skey(S["sym"], d), "entered")
                save_state()
                a_open = True
                print(f"[A進場] {S['sym']} {d} {fpx}")
                _bnote = "B單 持倉中" if cur_b else f"B單 等待觸發 @{S.get('back_px','-')}"
                await notify(app, chat,
                    f"{E.BOT} OKX原K｜{ACCT}\n事件：{E.ENTRY} A單進場成交\n"
                    f"━━━━━━━━━━\n"
                    f"商品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)} {S.get('lev')}x {S.get('margin')}\n"
                    f"進場：{fpx} | {hhmmss()}\n"
                    f"靜態TP：{S['front_tp_px']}（{'+' if d=='L' else '-'}{S['tp']}%）\n"
                    f"靜態SL：{S['front_static_sl']}（{'-' if d=='L' else '+'}{S['sl']}%）\n"
                    f"緊貼：{hug_display(spec, fpx, hug_pct(S,'front'))}（固定）\n"
                    f"　{S.get('hug_note','')}\n"
                    f"啟動門檻：{_trigger_line(S,'front')}\n"
                    f"━━━━━━━━━━\n"
                    f"對手：{_bnote}\n"
                    f"時間：{hhmmss()}")

            # ========== B 進場偵測 ==========
            if cur_b and not b_open:
                bpx = Decimal(str(cur_b.get("avgPx") or cur_b.get("last") or S.get("back_px", "0")))
                S["back_filled"] = True
                S["back_px"]     = str(bpx)
                S["back_ee"]     = time.time()
                S["back_peak"]   = str(bpx)
                await _ensure_b_oco(S, iid, b_side, back_d, bpx)
                if S.get("pair_state") == "waiting":
                    S["pair_state"] = "running"
                save_state()
                b_open = True
                print(f"[B進場] {S['sym']} {back_d} {bpx}")
                _anote = "A單 持倉中" if cur_a else f"A單 等待成交 @{S.get('front_px','-')}"
                await notify(app, chat,
                    f"{E.BOT} OKX原K｜{ACCT}\n事件：{E.ENTRY} B單觸發進場成交\n"
                    f"━━━━━━━━━━\n"
                    f"商品：{E.dir_emoji(back_d)} {S['sym']} {E.dir_word(back_d)} {S.get('lev')}x {S.get('margin')}\n"
                    f"進場：{bpx} | {hhmmss()}\n"
                    f"靜態TP：{S['back_tp_px']}（{'+' if back_d=='L' else '-'}{S['tp']}%）\n"
                    f"靜態SL：{S['back_static_sl']}（{'-' if back_d=='L' else '+'}{S['sl']}%）\n"
                    f"緊貼：{hug_display(spec, bpx, hug_pct(S,'back'))}（固定）\n"
                    f"　{S.get('hug_note','')}\n"
                    f"啟動門檻：{_trigger_line(S,'back')}\n"
                    f"━━━━━━━━━━\n"
                    f"對手：{_anote}\n"
                    f"時間：{hhmmss()}")

            # ========== 出場偵測（A/B 各自獨立，同一輪可同時處理） ==========
            a_gone = a_open and not cur_a
            b_gone = b_open and not cur_b
            if a_gone or b_gone:
                # 【#20/#21/#22】三重確認：一次空讀絕不當成出場。
                # OKX 持倉端點有讀取一致性延遲，會回 code=0 但內容缺漏。
                if a_gone and not await _confirm_gone(iid, a_side):
                    print(f"[空讀忽略] {S['sym']} A 持倉仍在，判定為查詢雜訊")
                    a_gone = False
                if b_gone and not await _confirm_gone(iid, b_side):
                    print(f"[空讀忽略] {S['sym']} B 持倉仍在，判定為查詢雜訊")
                    b_gone = False

                # 【#32/#33 核心】任一邊出場時，順便確認【對手】是不是也已經平掉了。
                # 兩次持倉讀取之間對手可能剛好被掃掉：實測第69輪，loop 讀到 A 還在、
                # B 已平，處理完 B 之後再查對手時 A 已經沒了，於是判定戰役結束、
                # 重新部署，而 _place_pair 把 A 的旗標抹掉 —— A 的出場永遠沒被回報，
                # +0.25% 的損益整筆消失。在這裡一併結清就不會有那個縫隙。
                if a_gone or b_gone:
                    if (not a_gone) and S.get("front_filled") and await _confirm_gone(iid, a_side):
                        a_gone = True
                        print(f"[補抓出場] {S['sym']} A單 同一輪也已平倉")
                    if (not b_gone) and S.get("back_filled") and await _confirm_gone(iid, b_side):
                        b_gone = True
                        print(f"[補抓出場] {S['sym']} B單 同一輪也已平倉")
                if not (a_gone or b_gone):
                    continue

                # 先快照、先清旗標，狀態機立刻往前走；記帳丟背景。
                snaps = []
                for gone, side in ((a_gone, "front"), (b_gone, "back")):
                    if not gone:
                        continue
                    seq = int(S.get("exit_seq", 0)) + 1
                    S["exit_seq"] = seq
                    snaps.append(_exit_snapshot(S, side, seq))
                    S[f"{side}_filled"] = False
                    print(f"[出場確認] {S['sym']} {'A' if side=='front' else 'B'}單 "
                          f"第{seq}個出場（記帳轉背景）")
                save_state()

                other = "back" if (a_gone and not b_gone) else ("front" if (b_gone and not a_gone) else None)
                note = await _pending_side_note(S, iid, other) if other else None
                if note:
                    asyncio.create_task(_exit_report(app, chat, S, snaps,
                        lambda n=note: f"━━━━━━━━━━\n{n}\n時間：{hhmmss()}"))
                    continue

                # 【#32 硬性檢查】仍有未結清的單就絕不宣告戰役結束
                if S.get("front_filled") or S.get("back_filled"):
                    print(f"[戰役續行] {S['sym']} 仍有未結清的單，不重新部署")
                    asyncio.create_task(_exit_report(app, chat, S, snaps,
                        lambda: f"━━━━━━━━━━\n戰場狀態：另一單尚未結清\n時間：{hhmmss()}"))
                    continue

                # ---- 戰役結束：零持倉、零掛單 ----
                await cancel_all_orders(iid)
                # 【#40】對帳【不能】在這裡做。此刻 _exit_report 還沒跑，
                # 剛出場那幾單的 posId 尚未登記，對帳會把它們當成「程式漏看」
                # 重複補登一次 —— 實測 2026-09-20 第2輪，A單的 -0.004106 被記了兩次。
                # 改到 _exit_report 內部、所有 posId 登記完之後才比對。
                prev = {"front": (S.get("front_exit_reason"), S.get("front_exit_pnl")),
                        "back":  (S.get("back_exit_reason"),  S.get("back_exit_pnl"))}
                had = {"front": bool(S.get("front_ee")), "back": bool(S.get("back_ee"))}
                rnd = S.get("round_today", "-")
                mvv = float(Decimal(str(S.get("margin", "1")))) or 1.0
                skey_now = skey(S["sym"], S.get("locked_dir", d))
                S["dir"] = S.get("locked_dir", d)
                S["pair_state"] = "waiting"
                redeploy = await _place_pair(S, iid, chat, app, label="新戰役")
                new_a = S.get("front_px") if redeploy else None
                new_b = S.get("back_px")  if redeploy else None

                def _settle_tail(S=S, prev=prev, had=had, rnd=rnd, mvv=mvv,
                                 new_a=new_a, new_b=new_b, kk=skey_now):
                    a_r = S.get("front_exit_reason") or prev["front"][0]
                    a_p = S.get("front_exit_pnl")    or prev["front"][1]
                    b_r = S.get("back_exit_reason")  or prev["back"][0]
                    b_p = S.get("back_exit_pnl")     or prev["back"][1]
                    # 【#31】區分「從未進場」與「進場過但沒拿到出場結果」
                    a_txt = a_r or ("結果未取得" if had["front"] else "未成交")
                    b_txt = b_r or ("結果未取得" if had["back"]  else "未成交")
                    if a_r and b_r:
                        form = "雙殺" if (a_r == "靜態SL" and b_r == "靜態SL") else "雙邊成交"
                    elif a_r:
                        form = "A單獨成交"
                    elif b_r:
                        form = "B單獨成交"
                    else:
                        form = "未成交"
                    try:
                        tot = float(a_p or 0) + float(b_p or 0)
                    except Exception:
                        tot = 0.0
                    bump(kk, f"form_{form}")
                    t = (f"━━━━━━━━━━\n"
                         f"🏁 戰役結束 第{rnd}輪\n"
                         f"A單：{a_txt}  {a_p or '-'}\n"
                         f"B單：{b_txt}  {b_p or '-'}\n"
                         f"戰役淨損益：{tot:+.6f}（{tot/mvv*100:+.3f}%）\n"
                         f"形態：{form}")
                    if new_a and new_b:
                        t += f"\n━━━━━━━━━━\n重新部署：A埋伏 {new_a}\nB觸發 {new_b}"
                    else:
                        t += "\n━━━━━━━━━━\n重新部署：稍後重試（戰場尚未清空）"
                    return t + f"\n時間：{hhmmss()}"

                asyncio.create_task(_exit_report(app, chat, S, snaps, _settle_tail))
                continue

            # ========== TF 零持倉重新部署 ==========
            # 【順序鐵則】排在進出場偵測之後。排在前面會讓 TF 快結束時剛成交的單
            # 永遠偵測不到（無進場通知、無出場通知、無損益）。
            if not cur_a and not cur_b and not a_open and not b_open:
                if tf_expired(S, "_tf_idx_loop"):
                    # 多幣種錯開：3 個幣種的 TF 邊界是同一秒，若同時撤單重掛會在
                    # 一瞬間擠出一堆下單請求。依幣種名給 0~1.4 秒的固定偏移分散掉。
                    _stg = (sum(ord(ch) for ch in S["sym"]) % 15) / 10.0
                    if _stg:
                        await asyncio.sleep(_stg)
                    print(f"[TF重錨定] {S['sym']} {d} 零持倉 → 撤單依現價重新部署（錯開{_stg}s）")
                    await cancel_all_orders(iid)
                    await _reconcile(app, chat, S, iid)
                    await _place_pair(S, iid, chat, app, label="TF重錨定")
                continue

    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("loop error", S.get("sym"), S.get("dir"), type(e).__name__, e)
        await notify(app, chat, f"{E.BOT} {E.LOSS} {S['sym']} 策略錯誤：{type(e).__name__}: {e}")
    finally:
        if not SHUTTING_DOWN:
            S["pair_state"] = "idle"
            S["state"] = "已停止"
            WS_WANT.discard(iid)
            if STRATS.get(k) is S:
                STRATS.pop(k, None)
            try:
                if TASKS.get(k) is asyncio.current_task():
                    TASKS.pop(k, None)
            except Exception:
                pass
            save_state()

def _norm_pair_state(v, d):
    """把舊版存檔裡的組合字串（A_in / AB_in / A_IN_B_REFILL / A_REFILL_B_IN）
    正規化成三態：idle / waiting / running。
    位置狀態由 front_filled / back_filled 兩個獨立旗標負責，不看這個值。"""
    if v == "idle":
        return "idle"
    if d.get("front_filled") or d.get("back_filled"):
        return "running"
    return "waiting" if v == "waiting" else "running"


async def rebuild_strat(d):
    spec = await get_spec(d["sym"])
    S = {"sym": d["sym"], "dir": d["dir"],
         "lev": int(d["lev"]), "margin": Decimal(str(d["margin"])),
         "offset": Decimal(str(d["offset"])),
         "gap": Decimal(str(d.get("gap", d.get("back_offset", 0)))),
         "tp": Decimal(str(d["tp"])),
         "sl": Decimal(str(d["sl"])),
         "hug_auto": Decimal(str(d.get("hug_auto", 0))),
         "spec": spec,
         "alive": True, "state": d.get("state", "委託中"),
         "pair_state": _norm_pair_state(d.get("pair_state", "waiting"), d),
         "chat": d.get("chat", CHAT_ID),
         "locked_dir": d.get("locked_dir", d["dir"])}
    # 重啟接管：把存檔裡所有位置欄位一併還原。
    # （舊版漏還原 back_static_sl / back_d / back_algo2_id / algo_id 等，
    #  導致重啟後 B 邊靜態SL 與 OCO ID 消失、移動SL 推不動。一併補齊。）
    for a in ("front_oid","front_px","front_static_sl","front_tp_px","front_sl_px",
              "front_filled","front_ee","front_sz","front_move_n","front_move_hist",
              "back_algo_id","back_algo2_id","back_amb_px","back_px","back_static_sl",
              "back_tp_px","back_sl_px","back_filled","back_ee","back_sz","back_d",
              "back_move_n","back_move_hist","front_peak","back_peak",
              "front_trough","back_trough","amp","hug_auto","hug_note",
              "front_exit_reason","back_exit_reason","front_exit_pnl","back_exit_pnl",
              "algo_id","exit_seq","battle_form","closing",
              "round_date","round_today","enter_today"):
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
            f"{pct(S.get('gap',0))} {pct(S['tp'])} {pct(S['sl'])}｜緊貼{float(Decimal(str(S.get('hug_auto',0)))*100):.3f}%")


async def cmd_run(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args
    fmt = (f"{E.BOT} 用法：/run 商品 方向 槓桿 保證金 A單埋伏% 兩單間距% TP% SL%\n"
           f"例：/run SUIUSDT L 1x 1 1.0 0 2 0.4\n"
           f"兩單間距% = B進場價比A進場價低(做L)/高(做S)多少；0 = 同價完全對沖\n"
           f"共8個參數，方向只能 L 或 S\n"
           f"（緊貼距離由腳本依該幣種波動自動計算，查價 {PRICE_TICK_SEC} 秒，皆不需輸入）")
    if len(a) != 8:
        await reply(u, f"{E.BOT} 參數數量錯誤（需8個）\n{fmt}"); return
    try:
        sym = a[0].upper(); dr = a[1].upper(); lev = int(a[2].replace("x", ""))
        margin = Decimal(a[3]); offset = Decimal(a[4].rstrip("%")); gap = Decimal(a[5].rstrip("%"))
        tp = Decimal(a[6].rstrip("%")); sl = Decimal(a[7].rstrip("%"))
    except Exception:
        await reply(u, f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if dr not in ("L", "S"):
        await reply(u, f"{E.BOT} 方向須 L 或 S"); return
    for nm, v in (("A單埋伏", offset), ("兩單間距", gap), ("TP", tp), ("SL", sl)):
        if v < 0:
            await reply(u, f"{E.BOT} {nm} 不可為負數"); return

    # ── 護欄① 兩單間距上限 ──
    # A 死時 B 的毛利 = SL% - 間距%。這個毛利必須 >= B 的手續費率，
    # B 的緊貼才會啟動；否則 B 還停在原始靜態SL 裸奔，價格一回頭就是雙殺。
    gap_max = sl - FEE_B * 100
    if gap > gap_max:
        await reply(u, f"{E.BOT} {E.LOSS} 兩單間距過大\n"
                       f"間距 {pct(gap)}% > 上限 {gap_max:.3f}%\n"
                       f"（A被SL掃掉時 B 的毛利只剩 {float(sl-gap):.3f}%，"
                       f"不足 B 的手續費 {float(FEE_B*100):.3f}%，B 的緊貼來不及啟動 → 會雙殺）\n"
                       f"請把間距降到 {gap_max:.3f}% 以下，或把 SL% 加大"); return
    # ── 護欄② TP 必須大於總成本 ──
    tp_min = sl + FEE_TOTAL * 100
    if tp <= tp_min:
        await reply(u, f"{E.BOT} {E.LOSS} TP 過低\n"
                       f"TP {pct(tp)}% <= 下限 {tp_min:.3f}%（SL {pct(sl)}% + 手續費 "
                       f"{float(FEE_TOTAL*100):.3f}%）\n打到 TP 也是虧損，請調高 TP 或調低 SL"); return

    k = skey(sym, dr)
    if k in STRATS and STRATS[k].get("pair_state","idle") != "idle":
        await reply(u, f"{E.BOT} {sym} {E.dir_word(dr)} 已在運行"); return
    k_rev = skey(sym, "S" if dr == "L" else "L")
    if k_rev in STRATS and STRATS[k_rev].get("pair_state","idle") != "idle":
        await reply(u, f"{E.BOT} {E.LOSS} {sym} 已有反向策略在運行，請先 /stop 再重新下單"); return

    try:
        spec = await get_spec(sym)
    except Exception:
        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return

    op = await get_last(spec["iid"])
    tick = spec["tick"]
    back_dr = "S" if dr == "L" else "L"
    if dr == "L":
        front_amb       = align(op * (1 - offset / 100), tick, "L")
        front_static_sl = align(front_amb * (1 - sl / 100), tick, "L")
        front_tp        = align(front_amb * (1 + tp / 100), tick, "S")
        back_amb        = align(front_amb * (1 - gap / 100), tick, back_dr)
    else:
        front_amb       = align(op * (1 + offset / 100), tick, "S")
        front_static_sl = align(front_amb * (1 + sl / 100), tick, "S")
        front_tp        = align(front_amb * (1 - tp / 100), tick, "L")
        back_amb        = align(front_amb * (1 + gap / 100), tick, back_dr)
    if back_dr == "S":
        back_static_sl = align(back_amb * (1 + sl / 100), tick, "S")
        back_tp        = align(back_amb * (1 - tp / 100), tick, "L")
    else:
        back_static_sl = align(back_amb * (1 - sl / 100), tick, "L")
        back_tp        = align(back_amb * (1 + tp / 100), tick, "S")

    sz_front = csize(margin, Decimal(lev), front_amb, spec["ctval"], spec["lot"])
    sz_back  = csize(margin, Decimal(lev), back_amb,  spec["ctval"], spec["lot"])
    if sz_front < spec["minsz"]:
        need = spec["minsz"] * spec["ctval"] * op / Decimal(lev)
        await reply(u, f"{E.BOT} {E.LOSS} 保證金不足：A單算出 {sz_front} 張 < 最小 {spec['minsz']}\n"
                       f"至少需 {need:.4f} USDT"); return

    # ── 護欄③ 緊貼距離 tick 地板（自動修正，不擋下單） ──
    hug_auto, hug_note = await auto_hug(spec["iid"], spec, sl / 100, front_amb)
    hug_u = hug_auto or Decimal("0")
    hug_a = hug_b = HUG_FIXED          # v3.7：固定值，A/B 共用
    hug_note = (f"固定 {float(HUG_FIXED*100):.3f}%"
                + (f"｜參考均幅 {float(hug_u/HUG_K*100):.4f}%" if hug_u else ""))
    warn0 = ""
    if hug_auto is None:
        warn0 = f"\n{E.WARN} {hug_note}"
    else:
        _c = _HUG_CACHE.get(spec["iid"])
        if _c:
            _amp = _c[0] * 100
            _x = float(sl / _amp) if _amp else 0
            if _x < 3:
                warn0 += (f"\n{E.WARN} SL {pct(sl)}% 只有 {_x:.1f}× 該幣種均幅"
                          f"（{float(_amp):.3f}%）\n"
                          f"　建議 SL ≥ {float(_amp * 3):.2f}%（3×），否則靜態SL 會被雜訊掃掉")
            else:
                warn0 += f"\n{E.OK} SL {pct(sl)}% = {_x:.1f}× 均幅（健康）"
    floor_a = (tick * MIN_HUG_TICKS) / front_amb
    floor_b = (tick * MIN_HUG_TICKS) / back_amb
    warn = warn0
    if floor_a > hug_a:
        warn += f"\n{E.WARN} A緊貼 {float(hug_a*100):.3f}% 不足{MIN_HUG_TICKS}檔 → 自動調整為 {float(floor_a*100):.4f}%"
        hug_a = floor_a
    if floor_b > hug_b:
        warn += f"\n{E.WARN} B緊貼 {float(hug_b*100):.3f}% 不足{MIN_HUG_TICKS}檔 → 自動調整為 {float(floor_b*100):.4f}%"
        hug_b = floor_b

    # ── 風險結構（這場戰役的完整輪廓，下單前先看清楚） ──
    # 最壞情境：A 被靜態SL 掃掉，生還方的緊貼 SL 只鎖住「毛利 - 自身緊貼距離」
    #   A毛 = -SL%；B毛 >= (SL% - 間距) - FEE_B；兩單手續費 = FEE_TOTAL
    #   合計 = -(間距 + FEE_B + FEE_TOTAL)  ← 與 SL% 無關，只由間距決定
    # 【#41】舊版只算「生還方撐到對手死才出場」這個【較好】情境，低估了真正的最壞。
    # 實測 2026-09-20：生還方先被中途回撤掃掉（約打平），行情接著恢復、
    # 落難方吃滿靜態SL → -0.457%，比舊版顯示的 -0.312% 差了 0.145%。
    worst_bad  = -(float(gap) + float(sl) + float(FEE_TOTAL * 100))   # 生還方也被掃
    worst_ok   = -(float(gap) + float(FEE_B * 100) + float(FEE_TOTAL * 100))
    breakev    =   float(gap) + float(FEE_TOTAL * 100)
    best       =   float(tp) - float(sl) - float(FEE_TOTAL * 100)

    PENDING[u.effective_chat.id] = {
        "kind": "run", "t": time.time(),
        "sym": sym, "dir": dr, "lev": lev, "margin": margin,
        "offset": offset, "gap": gap, "tp": tp, "sl": sl, "spec": spec,
        "hug_auto": hug_u, "hug_note": hug_note,
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
        f"A單：{sym} {E.dir_word(dr)}（限價埋伏）\n"
        f"埋伏價：{front_amb}（距現價{pct(offset)}%）\n"
        f"靜態TP：{front_tp}（{pct(tp)}%）\n"
        f"靜態SL：{front_static_sl}（{pct(sl)}%）\n"
        f"\n"
        f"B單：{sym} {E.dir_word(back_dr)}（觸發進場）\n"
        f"觸發價：{back_amb}（距A埋伏價{pct(gap)}%）\n"
        f"靜態TP：{back_tp}（{pct(tp)}%）\n"
        f"靜態SL：{back_static_sl}（{pct(sl)}%）\n"
        f"━━━━━━━━━━\n"
        f"最壞損失：{worst_bad:+.3f}%（生還方也被掃）\n"
        f"較好情境：{worst_ok:+.3f}%（生還方撐到最後）\n"
        f"打平需續走：{breakev:.3f}%\n"
        f"最大獲利：{best:+.3f}%（TP觸發）\n"
        f"緊貼距離：A {hug_display(spec, front_amb, hug_a)}｜B {hug_display(spec, back_amb, hug_b)}（固定）\n"
        f"　{hug_note}\n"
        f"查價間隔：{PRICE_TICK_SEC}s｜WS：{ws_status()}{warn}\n"
        f"━━━━━━━━━━\n"
        f"{E.WARN} 確認後立即取價埋伏\n下一步：60秒內 /confirm\n時間：{hhmmss()}")
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

def _side_block(S, side, waiting, holding):
    """/status 的單邊區塊：峰值、階段、SL 各自獨立成行，避免訊息過寬。"""
    pre = "front" if side == "front" else "back"
    nm  = "A單" if side == "front" else "B單"
    d = S["dir"] if side == "front" else S.get("back_d", "S" if S["dir"] == "L" else "L")
    out = []
    if holding:
        ent = S.get(f"{pre}_px", "-")
        out.append(f"{nm} 持倉 {ent}")
        pk = S.get(f"{pre}_peak") or ent
        try:
            eD = Decimal(str(ent)); pD = Decimal(str(pk))
            g = (pD - eD) / eD * 100 if d == "L" else (eD - pD) / eD * 100
            out.append(f"  峰值 {pD}（{g:+.3f}%）")
        except Exception:
            out.append(f"  峰值 {pk}")
        stage = "緊貼中" if hug_started(S, pre) else "緩衝中（未啟動）"
        try:
            out.append(f"  緊貼 {hug_display(S['spec'], S.get(f'{pre}_px') or 0, hug_pct(S, pre))}（固定）")
        except Exception:
            pass
        mn = int(S.get(f"{pre}_move_n", 0))
        _p, _t, _f = _move_stat(S.get(f"{pre}_move_hist") or [])
        stat = f" {E.MOVE_PROFIT}{_p} {E.MOVE_TIME}{_t} {E.MOVE_FAIL}{_f}" if (_p or _t or _f) else ""
        out.append(f"  SL {S.get(f'{pre}_sl_px','-')} {stage}{stat}")
        if mn:
            for m in (S.get(f"{pre}_move_hist") or [])[-5:]:
                out.append(f"    {m.get('t','')} {m.get('type','')} 峰{m.get('peak', m.get('px',''))} 止{m.get('sl','')}")
    elif waiting:
        amb = S.get(f"{pre}_px", "-")
        out.append(f"{nm} 埋伏 @{amb}")
        out.append(f"  TP {S.get(f'{pre}_tp_px','-')}｜SL {S.get(f'{pre}_static_sl','-')}")
        try:
            out.append(f"  緊貼 {hug_display(S['spec'], amb, hug_pct(S, pre))}（固定）")
        except Exception:
            pass
    else:
        r = S.get(f"{pre}_exit_reason")
        out.append(f"{nm} 已出場（{r}）" if r else f"{nm} 無")
    return out


def _tf_note(S, holding_a, holding_b):
    """下個 TF 到期時，判斷樹會走哪一條 —— 直接寫出來，不用自己推。"""
    tf_sec = TF_SEC.get(S.get("tf", ACCOUNT_TF), 300)
    left = int((int(time.time() // tf_sec) + 1) * tf_sec - time.time())
    m, sec = divmod(left, 60)
    t = f"{m}分{sec:02d}秒後" if m else f"{sec}秒後"
    if holding_a and holding_b:
        act = "兩單都在，不干預"
    elif holding_a or holding_b:
        side = "front" if holding_a else "back"
        act = "單邊有獲利，不干預" if hug_started(S, side) else "單邊無獲利，SL貼現價逼出場"
    else:
        act = "零持倉，撤單重新部署"
    return f"下個TF：{t}（{act}）"


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
    _, algo_list = await list_all_orders()
    total_pending = len(pdl) + len(algo_list)

    for i, s in enumerate(alive):
        d = s["dir"]
        bd = s.get("back_d", "S" if d == "L" else "L")
        iid_s = s["spec"]["iid"]
        _fps = "long" if d  == "L" else "short"
        _bps = "long" if bd == "L" else "short"
        # 【問題7】掛單數一律查 OKX，不看內部旗標 —— 手動撤單後旗標不會更新
        try:
            _o_all, _a_all = await list_all_orders(iid_s)
            _pend = [o for o in (_o_all + _a_all) if not _is_position_guard(o)]
            front_waiting = sum(1 for o in _pend if o.get("posSide") == _fps)
            back_waiting  = sum(1 for o in _pend if o.get("posSide") == _bps)
        except Exception:
            front_waiting = back_waiting = 0
        front_in = 1 if s.get("front_filled") else 0
        back_in  = 1 if s.get("back_filled")  else 0
        live_label = "⚡戰役中" if (front_in or back_in) else "埋伏中"
        round_t = int(s.get("round_today", 0))

        L.append("━━━━━━━━━━" if i == 0 else "")
        L.append(f"{E.dir_emoji(d)} {s['sym']} {E.dir_word(d)} "
                 f"{s.get('lev','?')}x {s.get('margin','?')} {live_label} 第{round_t}輪")
        try:
            cur_px_s = await get_px(iid_s)
            bar_s = NATIVE_BARS.get(ACCOUNT_TF, "5m")
            kr = await pub(f"/api/v5/market/candles?instId={iid_s}&bar={bar_s}&limit=1")
            open_px_s = Decimal(kr["data"][0][1]) if kr.get("code") == "0" and kr.get("data") else None
            px_emoji = "⚪"
            if open_px_s and cur_px_s:
                px_emoji = E.KLINE_UP if cur_px_s > open_px_s else (
                           E.KLINE_DOWN if cur_px_s < open_px_s else E.EVEN)
            L.append(f"現價：{px_emoji} {cur_px_s}｜{hhmmss()}")
        except Exception:
            L.append(f"現價：⚪ -｜{hhmmss()}")

        L += _side_block(s, "front", front_waiting, front_in)
        L += _side_block(s, "back",  back_waiting,  back_in)
        L.append(_tf_note(s, front_in, back_in))

    L.append("━━━━━━━━━━")
    L.append(f"OKX掛單：{total_pending}｜持倉：{len(pl)}")
    L.append(f"WS：{ws_status()}")
    L.append(f"時間：{hhmmss()} UTC+8")
    L.append(f"版本：{VERSION}")
    await reply(u, "\n".join(L))


# ---------- /summary ----------
REASONS = ("TP", "緊貼SL", "靜態SL", "TF逼倉", "手動/其他")

def sum_lines(rs, placed, entered):
    L = []
    hit = (entered / placed * 100) if placed else 0
    L.append("次數:%d掛|%d進(%.1f%%)" % (placed, entered, hit))
    for lab, sub in (("獲利", [r for r in rs if Decimal(str(r.get("net") or "0")) > 0]),
                     ("虧損", [r for r in rs if Decimal(str(r.get("net") or "0")) < 0])):
        ps = []
        for cn in REASONS:
            gg = [r for r in sub if r.get("reason") == cn]
            if not gg:
                continue
            sec = int(sum(int(r.get("hold_s") or 0) for r in gg) / len(gg))
            ps.append("%s:%d(%ds)" % (cn, len(gg), sec))
        L.append("%s:%d|%s" % (lab, len(sub), "|".join(ps) if ps else "-"))
    tg = sum((Decimal(str(r.get("gross") or "0")) for r in rs), Decimal(0))
    tf = sum((Decimal(str(r.get("fee") or "0")) for r in rs), Decimal(0))
    tn = sum((Decimal(str(r.get("net") or "0")) for r in rs), Decimal(0))
    nv = sum((Decimal(str(r.get("nv") or "0")) for r in rs), Decimal(0))
    L.append("毛損益:%+.6f (%+.3f%%)" % (tg, (tg / nv * 100) if nv else 0))
    L.append("手續費:%+.6f (%+.3f%%)" % (tf, (tf / nv * 100) if nv else 0))
    L.append("淨損益:%+.6f (%+.3f%%) %s" % (tn, (tn / nv * 100) if nv else 0, E.pnl_emoji(tn)))
    return L


def battle_lines(recs, ts):
    """【v3.1 新增】戰術驗證指標。
    最佳兩單間距不靠推算，靠這幾個數字跑出來 ——
    「A單獨成交」多 → 間距大划算（有便宜的小勝）；「雙殺」不為 0 → 間距設太大。"""
    L = ["━━━━━━━━━━", "【戰術指標】"]
    forms = {}
    for v in ts.values():
        for kk, vv in v.items():
            if str(kk).startswith("form_"):
                forms[kk[5:]] = forms.get(kk[5:], 0) + int(vv)
    tot_f = sum(forms.values())
    if tot_f:
        L.append("戰役形態（共%d場）：" % tot_f)
        for nm in ("A單獨成交", "B單獨成交", "雙邊成交", "雙殺"):
            n = forms.get(nm, 0)
            if n:
                L.append("  %s %d場（%.1f%%）" % (nm, n, n / tot_f * 100))
        if forms.get("雙殺"):
            L.append("  ⚠️ 雙殺不為0 → 兩單間距可能設太大")
    else:
        L.append("戰役形態：尚無完整戰役")

    if recs:
        n = len(recs)
        nets = [float(r.get("net") or 0) for r in recs]
        win = sum(1 for x in nets if x > 0)
        L.append("單筆出場：%d筆｜勝率 %.1f%%" % (n, win / n * 100))
        L.append("平均淨損益：%+.6f" % (sum(nets) / n))
        L.append("最大單筆獲利：%+.6f" % max(nets))
        L.append("最大單筆虧損：%+.6f" % min(nets))
        by = {}
        for r in recs:
            by[r.get("reason", "?")] = by.get(r.get("reason", "?"), 0) + 1
        L.append("出場原因：" + "｜".join("%s%d" % (k2, v2) for k2, v2 in by.items()))
        hugs = [float(r.get("hug") or 0) for r in recs if r.get("hug")]
        if hugs:
            L.append("平均緊貼距離：%.3f%%" % (sum(hugs) / len(hugs)))
        hg = [r for r in recs if r.get("reason") == "緊貼SL" and r.get("peak_pct")]
        if hg:
            pk = sum(float(r["peak_pct"]) for r in hg) / len(hg)
            nt = sum(float(r.get("net") or 0) / float(r.get("nv") or 1) * 100 for r in hg) / len(hg)
            L.append("緊貼出場 %d 筆：平均峰值 %+.3f%% → 實收 %+.3f%%" % (len(hg), pk, nt))
            L.append("　（峰值遠高於實收 → 緊貼偏緊；兩者接近 → 緊貼合適）")
        tfn = by.get("TF逼倉", 0)
        if tfn:
            hrs = max(1.0, (time.time() % 86400) / 3600)
            L.append("TF逼倉：%d次（約%.1f次/小時）" % (tfn, tfn / hrs))
    return L


async def cmd_summary(u, c):
    """總表一頁 → 每個「幣種＋方向」各一頁。多幣種時才分得清誰賺誰賠。"""
    t = today8(); recs = load_trades(t)
    ts = {k: v for k, v in STATS.items() if str(v.get("date")) == str(t)}

    # ---------- 第一頁：全帳戶總表 ----------
    L = [f"{E.BOT} OKX原K｜{ACCT} {VERSION}", f"{E.CHART}{E.CHART}{E.CHART} 總表 {t}"]
    pa_all = sum(v.get("placed", 0) for v in ts.values())
    en_all = sum(v.get("entered", 0) for v in ts.values())
    L += sum_lines(recs, pa_all, en_all)
    pairs = sorted({(r["sym"], r["dir"]) for r in recs})
    if pairs:
        L.append("━━━━━━━━━━")
        L.append("分項：")
        for sy, dr in pairs:
            rows = [r for r in recs if r["sym"] == sy and r["dir"] == dr]
            n = sum((Decimal(str(r.get("net") or "0")) for r in rows), Decimal(0))
            nv = sum((Decimal(str(r.get("nv") or "0")) for r in rows), Decimal(0))
            L.append(f"{E.dir_emoji(dr)} {sy} {E.dir_word(dr)}：{len(rows)}筆 "
                     f"{n:+.6f}（{(n/nv*100) if nv else 0:+.3f}%）{E.pnl_emoji(n)}")
    L += battle_lines(recs, ts)
    L.append(f"時間:{hhmmss()}")
    await reply(u, "\n".join(L))

    # ---------- 之後每頁：一個幣種＋一個方向 ----------
    for sy, dr in pairs:
        rows = [r for r in recs if r["sym"] == sy and r["dir"] == dr]
        st_ = ts.get(skey(sy, dr)) or {"placed": 0, "entered": 0}
        D = [f"{E.dir_emoji(dr)} {sy} {E.dir_word(dr)}　{t}",
             f"策略：{strat_params(sy, dr)}"]
        D += sum_lines(rows, st_.get("placed", 0), st_.get("entered", 0))
        D += battle_lines(rows, {skey(sy, dr): st_})
        best = max(rows, key=lambda r: float(r.get("net") or 0), default=None)
        worst = min(rows, key=lambda r: float(r.get("net") or 0), default=None)
        if best:
            D.append("━━━━━━━━━━")
            D.append(f"最佳：{best.get('reason')} {float(best.get('net') or 0):+.6f}")
            D.append(f"最差：{worst.get('reason')} {float(worst.get('net') or 0):+.6f}")
        D.append(f"時間:{hhmmss()}")
        await reply(u, "\n".join(D))


# ---------- /tune 調參報告 ----------
def _load_days(n):
    """讀近 n 天的交易紀錄（跨日彙整，樣本才夠）。"""
    out = []
    base = now8()
    for i in range(n):
        d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        out += load_trades(d)
    return out


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def _tune_block(sym, all_rows):
    """單一幣種的調參診斷。每條建議都附依據，樣本不足會標示。

    【相容】v3.6 之前的紀錄沒有 amp/mfe/mae/moves 等欄位，
    直接混在一起算會得到一整排 0.000%，看起來像「完全沒移動」——
    那不是真的沒移動，是當時根本沒存。所以分開統計並明確標示。"""
    L = [f"🔧 {sym}　{len(all_rows)} 筆出場"]
    if not all_rows:
        return L + ["（尚無資料）"]
    # 舊紀錄相容：v3.5 的 peak_pct 等同 mfe，救回來
    for r in all_rows:
        if r.get("mfe") is None and r.get("peak_pct") is not None:
            r["mfe"] = r["peak_pct"]
    rows = [r for r in all_rows if float(r.get("amp") or 0) > 0]   # 欄位完整的
    oldn = len(all_rows) - len(rows)
    L.append("━━━━━━━━━━")
    if oldn:
        L.append(f"{E.WARN} 其中 {oldn} 筆是 v3.6 前的紀錄，缺均幅/峰值/SL移動等欄位")
        L.append("　（只列入損益統計，不參與診斷）")
    if not rows:
        nets = [float(r.get("net") or 0) for r in all_rows]
        nvs  = sum(float(r.get("nv") or 0) for r in all_rows) or 1
        L.append("")
        L.append("【診斷】尚無 v3.6 完整紀錄，等新資料累積")
        L.append(f"【損益】淨 {sum(nets):+.6f}（{sum(nets)/nvs*100:+.3f}%）"
                 f"｜勝率 {len([x for x in nets if x>0])/len(nets)*100:.0f}%")
        return L
    last = rows[-1]
    amp = float(last.get("amp") or 0)
    sl  = float(last.get("sl") or 0)
    hug = float(last.get("hug") or 0)
    slx = (sl / amp) if amp else 0
    hgx = (hug / amp) if amp else 0
    L.append(f"完整紀錄 {len(rows)} 筆")
    L.append(f"目前：均幅 {amp:.3f}%｜SL {sl:.2f}%（{slx:.1f}×）｜緊貼 {hug:.3f}%（{hgx:.2f}×）")
    if hgx and hgx < 1.0:
        L.append(f"{E.WARN} 緊貼 {hgx:.2f}× 均幅 — 比一根平均K還窄，必被雜訊掃掉")
    if slx and slx < 3:
        L.append(f"{E.WARN} SL 僅 {slx:.1f}× 均幅 — 靜態SL 會被雜訊掃掉")

    # 【SL 診斷】
    ssl = [r for r in rows if r.get("reason") == "靜態SL"]
    L.append("")
    L.append(f"【SL 診斷】靜態SL 出場 {len(ssl)} 筆（{len(ssl)/len(rows)*100:.0f}%）")
    if ssl:
        fast = [r for r in ssl if int(r.get("hold_s") or 0) <= 120]
        back = [r for r in ssl if r.get("post5") is not None and float(r["post5"]) > 0]
        n_p  = len([r for r in ssl if r.get("post5") is not None])
        L.append(f"  2分鐘內被掃 {len(fast)} 筆（{len(fast)/len(ssl)*100:.0f}%）← 雜訊掃殺")
        if n_p:
            L.append(f"  出場後回頭 {len(back)}/{n_p} 筆（{len(back)/n_p*100:.0f}%）← SL太窄鐵證")
        L.append(f"  平均 MAE {_avg([float(r.get('mae') or 0) for r in ssl]):.3f}%（SL {sl:.2f}%）")
        if amp:
            L.append(f"  ▸ 建議 SL：{amp*3:.2f}%（3× 均幅）" if slx < 3
                     else f"  ▸ SL 倍數健康，維持 {sl:.2f}%")

    # 【緊貼診斷】
    hgs = [r for r in rows if r.get("reason") == "緊貼SL"]
    L.append("")
    L.append(f"【緊貼診斷】緊貼SL 出場 {len(hgs)} 筆")
    if hgs:
        mfe  = _avg([float(r.get("mfe") or 0) for r in hgs])
        real = _avg([float(r.get("net") or 0) / (float(r.get("nv") or 1) or 1) * 100 for r in hgs])
        ps   = [float(r["post5"]) for r in hgs if r.get("post5") is not None]
        L.append(f"  平均峰值 {mfe:+.3f}% → 實收 {real:+.3f}%（回吐 {mfe-real:.3f}%）")
        if ps:
            miss = [x for x in ps if x > 0.2]
            L.append(f"  出場後5分平均還走 {_avg(ps):+.3f}%　超過0.2% 有 {len(miss)}/{len(ps)} 筆")
            if len(ps) >= 10:
                if len(miss) / len(ps) >= 0.6:
                    L.append(f"  ▸ 出太早：建議緊貼調鬆到 {max(hug*1.3, amp*1.5):.3f}%")
                elif _avg(ps) < 0.05:
                    L.append(f"  ▸ 出場點準：可試著調緊到 {max(hug*0.8, amp*1.0):.3f}%")
                else:
                    L.append("  ▸ 目前緊貼合適，維持")
            else:
                L.append(f"  ▸ 樣本 {len(ps)} 筆（需10筆才給建議）")
        else:
            L.append("  ▸ 出場後追蹤資料累積中")
    fast_h = [r for r in hgs if int(r.get("hold_s") or 0) <= 60]
    if len(hgs) >= 10 and len(fast_h) / len(hgs) >= 0.6:
        L.append(f"  {E.WARN} {len(fast_h)}/{len(hgs)} 筆在1分鐘內出場 ← 貼太緊")

    # 【SL 移動】
    mv = [int(r.get("move_n") or 0) for r in rows]
    fails = sum(int(r.get("move_fail") or 0) for r in rows)
    tfs   = sum(int(r.get("move_tf") or 0) for r in rows)
    L.append("")
    L.append(f"【SL移動】平均 {_avg(mv):.1f} 次／單｜TF逼倉 {tfs} 次｜失敗 {fails} 次")
    nomove = len([x for x in mv if x == 0])
    L.append(f"  完全沒移動 {nomove}/{len(rows)} 筆（{nomove/len(rows)*100:.0f}%）← 緊貼從未啟動")

    # 【綜合】損益用全部紀錄（舊版也有記損益，是有效的）
    nets = [float(r.get("net") or 0) for r in all_rows]
    nvs  = sum(float(r.get("nv") or 0) for r in all_rows) or 1
    win  = len([x for x in nets if x > 0])
    L.append("")
    L.append(f"【綜合】{len(all_rows)} 筆　淨 {sum(nets):+.6f}（{sum(nets)/nvs*100:+.3f}%）"
             f"｜勝率 {win/len(all_rows)*100:.0f}%")
    L.append(f"　最佳 {max(nets):+.6f}｜最差 {min(nets):+.6f}"
             f"｜平均持倉 {_avg([int(r.get('hold_s') or 0) for r in all_rows]):.0f}s")
    return L


async def cmd_tune(u, c):
    """調參報告：用實測數據回答 SL 與緊貼該設多少。
    用法：/tune [幣種] [天數]"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args or []
    sym = None; days = 3
    for x in a:
        if x.replace(".", "").isdigit():
            days = max(1, min(30, int(float(x))))
        else:
            sym = x.upper()
    rows = [r for r in _load_days(days) if r.get("reason") != "補登"]
    if sym:
        rows = [r for r in rows if r.get("sym") == sym]
    if not rows:
        await reply(u, f"{E.BOT} 近 {days} 天沒有可分析的紀錄"
                       f"{('（' + sym + '）') if sym else ''}"); return
    syms = sorted({r.get("sym") for r in rows})
    nfull = len([r for r in rows if float(r.get("amp") or 0) > 0])
    head = [f"{E.BOT} OKX原K｜{ACCT} {VERSION}",
            f"🔧 調參報告　近 {days} 天　{len(rows)} 筆",
            f"　完整紀錄 {nfull} 筆｜舊版 {len(rows)-nfull} 筆（欄位不全）",
            f"幣種：{'、'.join(syms)}"]
    await reply(u, "\n".join(head))
    for sy in syms:
        sub = [r for r in rows if r.get("sym") == sy]
        for dr in sorted({r.get("dir") for r in sub}):
            block = _tune_block(f"{sy} {E.dir_word(dr)}", [r for r in sub if r.get("dir") == dr])
            block.append(f"時間:{hhmmss()}")
            await reply(u, "\n".join(block))


# ---------- /apitest API 探測（純診斷，完全不碰交易邏輯） ----------
def _brief(r, keys=None):
    """把 OKX 回應壓成一行：code / msg / data[0] 的重點欄位。"""
    try:
        c = r.get("code"); m = (r.get("msg") or "").strip()
        d = (r.get("data") or [{}])
        d0 = d[0] if d else {}
        sc = d0.get("sCode"); sm = (d0.get("sMsg") or "").strip()
        out = f"code={c}"
        if m: out += f" msg={m[:140]}"
        if sc not in (None, ""): out += f" sCode={sc}"
        if sm: out += f" sMsg={sm[:140]}"
        if keys:
            for k in keys:
                if d0.get(k) not in (None, ""):
                    out += f" {k}={d0.get(k)}"
        return out
    except Exception as e:
        return f"<解析失敗 {type(e).__name__}>"


async def _apitest_probe(say, sym, spec, d, path):
    """對「一條進場路徑」做完整探測。
    path = "limit"（A單路徑）或 "trigger"（B單路徑）——
    這兩條路徑生成的 OCO 行為不同（trigger 的不能 amend，錯誤碼 51506），
    只測其中一條等於測不到真正的問題。
    d = "L" / "S"，多空的 SL 方向相反，也必須各測各的。"""
    iid  = spec["iid"]; tick = spec["tick"]; sz = spec["minsz"]
    ps   = "long" if d == "L" else "short"
    open_side  = "buy"  if d == "L" else "sell"
    close_side = "sell" if d == "L" else "buy"
    px = await get_last(iid)
    created = {"ord": None}
    oco_id = None

    # ── 開倉 ──
    if d == "L":
        tp_px = align(px * Decimal("1.05"), tick, "S")
        sl_px = align(px * Decimal("0.95"), tick, "L")
    else:
        tp_px = align(px * Decimal("0.95"), tick, "L")
        sl_px = align(px * Decimal("1.05"), tick, "S")
    attach = [{"tpTriggerPx": str(tp_px), "tpOrdPx": "-1",
               "slTriggerPx": str(sl_px), "slOrdPx": "-1"}]

    if path == "limit":
        # 掛在對自己不利的一側 → 立刻成交
        lim = align(px * (Decimal("1.002") if d == "L" else Decimal("0.998")), tick,
                    "S" if d == "L" else "L")
        say(f"  掛限價 {open_side} @{lim}（現價 {px}）TP {tp_px} SL {sl_px}")
        r = await api("POST", "/api/v5/trade/order", {
            "instId": iid, "tdMode": "isolated", "side": open_side, "posSide": ps,
            "ordType": "limit", "px": str(lim), "sz": str(sz),
            "clOrdId": "t" + uuid.uuid4().hex[:14], "attachAlgoOrds": attach})
        say(f"  {_brief(r, ['ordId'])}")
        created["ord"] = (r.get("data") or [{}])[0].get("ordId")
    else:
        # 【v3.7.3 修正】OKX 觸發單是「等價格走到觸發價」才觸發，不是「已越過就立刻觸發」。
        # 舊版把 buy 掛在現價下方、sell 掛在現價上方 → 等於等一個反方向的移動，
        # 兩次測試都沒吃到倉。正確做法：掛在「會被立刻走到」的那一側，只差 1 檔。
        #   buy  觸發 → 價格「漲」到觸發價才成交 → 掛現價 +1 檔
        #   sell 觸發 → 價格「跌」到觸發價才成交 → 掛現價 −1 檔
        trg = align(px + tick if d == "L" else px - tick, tick,
                    "S" if d == "L" else "L")
        say(f"  掛觸發 {open_side} 觸發@{trg}（現價 {px}，差 1 檔 → 等價格走到）TP {tp_px} SL {sl_px}")
        r = await api("POST", "/api/v5/trade/order-algo", {
            "instId": iid, "tdMode": "isolated", "side": open_side, "posSide": ps,
            "ordType": "trigger", "sz": str(sz), "triggerPx": str(trg),
            "orderPx": "-1", "triggerPxType": "last",
            "algoClOrdId": "b" + uuid.uuid4().hex[:14], "attachAlgoOrds": attach})
        say(f"  {_brief(r, ['algoId'])}")

    if r.get("code") != "0":
        say(f"  {E.LOSS} 下單失敗，本路徑略過")
        return None, created
    # 【v3.7.3】限價單掛在不利側→秒成交；觸發單要等價格走 1 檔，給它 90 秒
    wait_s = 6 if path == "limit" else 90
    t0 = time.time(); pos = None
    while time.time() - t0 < wait_s:
        await asyncio.sleep(2)
        _ok, pos = await okx_pos_ex(iid, ps, force=True)
        if pos: break
    if not pos:
        say(f"  {E.LOSS} 等 {int(time.time()-t0)} 秒仍沒吃到倉（未成交/未觸發），本路徑略過")
        return None, created
    entry_px = Decimal(str(pos.get("avgPx") or px))
    say(f"  ✅ 成交 avgPx={entry_px} 張數={pos.get('pos')}（等待 {int(time.time()-t0)} 秒）")

    # ── 自動生成的 algo 單 ──
    say("  自動生成的 algo 單：")
    found = 0
    for ot in ("oco", "conditional", "move_order_stop", "trigger"):
        rr = await api("GET", f"/api/v5/trade/orders-algo-pending?instId={iid}&ordType={ot}")
        for o in (rr.get("data") or []):
            if o.get("posSide") != ps:
                continue
            found += 1
            say(f"    [{ot}] algoId={o.get('algoId')} ordType={o.get('ordType')} "
                f"reduceOnly={o.get('reduceOnly')}")
            say(f"          sz={o.get('sz')} tp={o.get('tpTriggerPx')} sl={o.get('slTriggerPx')}")
            if ot in ("oco", "conditional") and not oco_id:
                oco_id = o.get("algoId")
    if not found:
        say("    （查不到）")

    # 【v3.7.3】amend 測試拆成「安全」與「邊界」兩段，中間隔著其他測試。
    # 原因：上次 S 方向「等於現價」那一筆 OKX 回 code=0 然後倉位就沒了
    # ——SL 貼到現價不是被拒，是當場觸發成交。倉位一死，後面全部測不到。
    # 所以危險的兩檔（等於現價／越界）一律擺到最後面。
    async def _amend_try(label, off):
        """回傳 True=持倉還在可以繼續；False=倉位已被打掉。"""
        cur  = await get_last(iid)
        t_sl = align(cur + tick * off, tick, "S" if d == "L" else "L")
        rr = await api("POST", "/api/v5/trade/amend-algos",
                       [{"instId": iid, "algoId": oco_id, "newSlTriggerPx": str(t_sl)}])
        say(f"    {label:10} 現價={cur} SL={t_sl}　{_brief(rr)}")
        await asyncio.sleep(1.2)
        _ok, _p = await okx_pos_ex(iid, ps, force=True)
        if not _p:
            say("    ⚠️ 此筆之後持倉消失 → 該 SL 是「立刻觸發成交」而非「被拒絕」")
            return False
        return True

    # ── 第一段：安全側（這段就能回答「這條路徑的 OCO 到底能不能改」）──
    say("  amend-algos 測試（安全側，不會觸發）：")
    alive = True
    if not oco_id:
        say("    （無 OCO 可測）")
    else:
        for off, label in ([(-5, "安全側5檔"), (-1, "安全側1檔")] if d == "L"
                           else [(5, "安全側5檔"), (1, "安全側1檔")]):
            alive = await _amend_try(label, off)
            if not alive: break
    if not alive:
        return ps, created

    # ── 換單測試（現行 B 單的修法）──
    if path == "trigger":
        say("  換單測試（掛自己的 OCO 取代自動那張）：")
        n_tp, n_sl = (tp_px, sl_px)
        rr = await api("POST", "/api/v5/trade/order-algo", {
            "instId": iid, "tdMode": "isolated", "side": close_side, "posSide": ps,
            "ordType": "oco", "sz": str(sz),
            "tpTriggerPx": str(n_tp), "tpOrdPx": "-1",
            "slTriggerPx": str(n_sl), "slOrdPx": "-1",
            "clOrdId": "y" + uuid.uuid4().hex[:14]})
        say(f"    掛新 OCO（舊的還在）　{_brief(rr, ['algoId'])}")
        new_id = (rr.get("data") or [{}])[0].get("algoId")
        if new_id:
            cur = await get_last(iid)
            t_sl = cur - tick * 5 if d == "L" else cur + tick * 5
            r2 = await api("POST", "/api/v5/trade/amend-algos",
                           [{"instId": iid, "algoId": new_id, "newSlTriggerPx": str(t_sl)}])
            say(f"    amend 新 OCO SL={t_sl}　{_brief(r2)}　"
                f"{'✅ 可改（換單修法有效）' if (r2.get('data') or [{}])[0].get('sCode') == '0' else '❌ 不可改'}")

    # ── 原生移動止損 ──
    # 【v3.7.3】新增 activePx（啟動價）測試。沒有啟動價，移動止損一掛上去就生效，
    # 進場後往SL方向走 0.1% 就被掃掉 —— 直接違反「往SL方向不動，有對沖單保護」。
    # 有啟動價才等於 hug_sl 的「峰值 ≥ 進場價×(1+F) 才啟動」。所以先測有啟動價的。
    act = align(entry_px * (Decimal("1") + HUG_FIXED if d == "L" else Decimal("1") - HUG_FIXED),
                tick, "S" if d == "L" else "L")
    say(f"  move_order_stop 原生移動止損（進場 {entry_px}）：")
    say(f"    ① 帶啟動價 activePx={act}（進場價 {'+' if d == 'L' else '−'}0.1%）")
    hit = None
    for ratio in ("0.001", "0.002", "0.005", "0.01"):
        rr = await api("POST", "/api/v5/trade/order-algo", {
            "instId": iid, "tdMode": "isolated", "side": close_side, "posSide": ps,
            "ordType": "move_order_stop", "sz": str(sz), "callbackRatio": ratio,
            "activePx": str(act), "reduceOnly": "true",
            "algoClOrdId": "m" + uuid.uuid4().hex[:14]})
        say(f"      callbackRatio={ratio:6} {_brief(rr, ['algoId'])}")
        if rr.get("code") == "0":
            hit = ratio; break
        await asyncio.sleep(0.5)
    if not hit:
        say("    ② 不帶啟動價（退而求其次，確認是啟動價被拒還是比例被拒）")
        for ratio in ("0.001", "0.002", "0.005", "0.01"):
            rr = await api("POST", "/api/v5/trade/order-algo", {
                "instId": iid, "tdMode": "isolated", "side": close_side, "posSide": ps,
                "ordType": "move_order_stop", "sz": str(sz), "callbackRatio": ratio,
                "algoClOrdId": "m" + uuid.uuid4().hex[:14]})
            say(f"      callbackRatio={ratio:6} {_brief(rr, ['algoId'])}")
            if rr.get("code") == "0":
                hit = ratio + "（無啟動價）"; break
            await asyncio.sleep(0.5)
    if not hit:
        rr = await api("POST", "/api/v5/trade/order-algo", {
            "instId": iid, "tdMode": "isolated", "side": close_side, "posSide": ps,
            "ordType": "move_order_stop", "sz": str(sz),
            "callbackSpread": str(tick * 10),
            "algoClOrdId": "m" + uuid.uuid4().hex[:14]})
        say(f"      callbackSpread={tick*10}　{_brief(rr, ['algoId'])}")
        if rr.get("code") == "0":
            hit = "spread"
    say(f"    → {'✅ 可用，最小 ' + hit if hit else '❌ 全部被拒'}")

    # ── 共存 ── 【v3.7.3】改列清單，不只數總數：上次 S 方向數到 1 張，
    # 光看數字看不出是「OCO 被擠掉」還是「移動止損沒掛上」。
    say("  同倉位 algo 清單：")
    tot = 0
    for ot in ("oco", "conditional", "move_order_stop", "trigger"):
        rr = await api("GET", f"/api/v5/trade/orders-algo-pending?instId={iid}&ordType={ot}")
        for o in (rr.get("data") or []):
            if o.get("posSide") != ps: continue
            tot += 1
            say(f"    [{ot}] algoId={o.get('algoId')} sz={o.get('sz')} "
                f"sl={o.get('slTriggerPx')} tp={o.get('tpTriggerPx')} "
                f"cbRatio={o.get('callbackRatio')} activePx={o.get('activePx')}")
    if not tot: say("    （一張都沒有）")
    say(f"  → 共 {tot} 張　{'✅ OCO 與移動止損可共存' if tot >= 2 else '❌ 無法共存（後掛的擠掉前面那張）'}")

    # ── 最後一段：SL 邊界（會把倉位打掉，所以擺最後）──
    # 【這題決定 #23 死鎖怎麼修】緊貼算出來的 SL 越過現價時，
    # 到底是「被 OKX 拒絕」還是「立刻成交出場」？
    # 前者要找最近的合法價位，後者表示「直接平倉」才是正解。
    say("  amend-algos 測試（邊界，可能當場觸發出場）：")
    if not oco_id:
        say("    （無 OCO 可測）")
    else:
        for off, label in ([(0, "等於現價"), (1, "越界1檔")] if d == "L"
                           else [(0, "等於現價"), (-1, "越界1檔")]):
            if not await _amend_try(label, off): break
    return ps, created


async def _apitest_clean(say, iid, ps, created):
    """清掉這條路徑建立的一切，並驗證歸零。"""
    try:
        if created.get("ord"):
            await api("POST", "/api/v5/trade/cancel-order",
                      {"instId": iid, "ordId": created["ord"]})
        # 【v3.7.3】先平倉、再看剩下什麼 —— 這樣才答得出「倉位平掉後，
        # 那些 algo 單會不會自動消失」。舊版先撤單再平倉，等於把答案擦掉了。
        # 沒有自動消失 = 殘單會卡住下一場的淨場檢查，必須在腳本裡主動撤。
        if ps:
            ok, pos = await okx_pos_ex(iid, ps, force=True)
            if pos:
                rr = await api("POST", "/api/v5/trade/order", {
                    "instId": iid, "tdMode": "isolated",
                    "side": "sell" if ps == "long" else "buy", "posSide": ps,
                    "ordType": "market", "sz": str(pos.get("pos")),
                    "clOrdId": "z" + uuid.uuid4().hex[:14]})
                say(f"  平測試倉 {pos.get('pos')} 張　{_brief(rr)}")
                await asyncio.sleep(3)
            left = []
            for ot in ("oco", "conditional", "move_order_stop", "trigger"):
                rr = await api("GET", f"/api/v5/trade/orders-algo-pending?instId={iid}&ordType={ot}")
                left += [f"{ot}:{o.get('algoId')}" for o in (rr.get("data") or [])
                         if o.get("posSide") == ps]
            say(f"  平倉後殘留 algo {len(left)} 張 → "
                + ("✅ 會自動消失，不必額外撤" if not left
                   else f"⚠️ 不會自動消失，必須主動撤：{'、'.join(left[:4])}"))
        for ot in ("oco", "conditional", "move_order_stop", "trigger"):
            rr = await api("GET", f"/api/v5/trade/orders-algo-pending?instId={iid}&ordType={ot}")
            ids = [{"instId": iid, "algoId": o["algoId"]}
                   for o in (rr.get("data") or []) if o.get("algoId")]
            if ids:
                await api("POST", "/api/v5/trade/cancel-algos", ids)
        ok, p2 = await okx_pos_ex(iid, ps or "long", force=True)
        _o, _a = await list_all_orders(iid)
        # 【v3.7.3】list_all_orders 只看 ALGO_TYPES=('trigger','oco')，
        # 看不到 move_order_stop。清場驗證自己把四種都數一遍，否則會謊報乾淨。
        extra = 0
        for ot in ("oco", "conditional", "move_order_stop", "trigger"):
            rr = await api("GET", f"/api/v5/trade/orders-algo-pending?instId={iid}&ordType={ot}")
            extra += len(rr.get("data") or [])
        clean = (not p2) and not _o and extra == 0
        say(f"  清場：持倉{'無' if not p2 else '【仍有！】'}｜限價單 {len(_o)} 張｜"
            f"algo（四型全查）{extra} 張　"
            f"{'✅ 已清空' if clean else '⚠️ 未清空，請手動檢查 OKX'}")
        return clean
    except Exception as ex:
        say(f"  {E.LOSS} 清場失敗：{type(ex).__name__}: {ex} ← 請立刻手動到 OKX 檢查")
        return False


async def _apitest_run(u, sym, dirs):
    """對指定方向、兩條進場路徑各做一次完整探測。"""
    L = []
    def say(s): L.append(s); print("[apitest]", s)

    spec = await get_spec(sym)
    iid  = spec["iid"]
    px   = await get_last(iid)
    notional = spec["minsz"] * spec["ctval"] * px
    say(f"幣種 {sym}｜{iid}")
    say(f"現價 {px}｜tick {spec['tick']}｜最小張數 {spec['minsz']}｜名目 ≈{notional:.4f} USDT")
    say(f"測試方向 {'／'.join(dirs)}　×　兩條路徑（限價=A單、觸發=B單）")
    say("")
    if notional > 5:
        say(f"{E.LOSS} 最小名目 {notional:.2f} USDT > 5，拒絕執行（成本過高）")
        return L

    say("【0】orders-algo-pending 各 ordType 是否受理（唯讀）")
    for ot in ("trigger", "oco", "conditional", "move_order_stop", "twap"):
        r = await api("GET", f"/api/v5/trade/orders-algo-pending?instId={iid}&ordType={ot}")
        say(f"  {ot:16} {_brief(r)}｜筆數={len(r.get('data') or [])}")
    say("")

    n = 0
    for d in dirs:
        for path, pname in (("limit", "A單路徑：限價單"), ("trigger", "B單路徑：觸發單")):
            n += 1
            say(f"【{n}】{pname}　方向 {E.dir_word(d)}")
            ps = None; created = {}
            try:
                ps, created = await _apitest_probe(say, sym, spec, d, path)
            except Exception as ex:
                say(f"  {E.LOSS} 探測中斷：{type(ex).__name__}: {ex}")
            finally:
                await _apitest_clean(say, iid, ps, created or {})
            say("")

    say(f"【{n+1}】程式現用的涵蓋範圍")
    say(f"  ALGO_TYPES = {ALGO_TYPES}　← cancel_all_orders / list_all_orders 只看這些")
    return L


async def cmd_apitest(u, c):
    """/apitest 幣種 [L|S|LS] —— 用最小張數實測 OKX API，把原始回應印出來。
    純診斷：不碰 loop、不碰 frame_mover、不碰任何出場邏輯。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args or []
    if not a:
        await reply(u, f"{E.BOT} 用法：/apitest 幣種 [L|S|LS]\n"
                       f"例：/apitest WIFUSDT L　（只測多）\n"
                       f"　　/apitest WIFUSDT S　（只測空）\n"
                       f"　　/apitest WIFUSDT LS　（多空都測，約 6 分鐘）\n"
                       f"每個方向都會測【限價單】和【觸發單】兩條路徑，測完自動清場。")
        return
    sym = a[0].upper()
    want = (a[1].upper() if len(a) > 1 else "L")
    dirs = [x for x in ("L", "S") if x in want] or ["L"]

    for k in (skey(sym, "L"), skey(sym, "S")):
        if k in STRATS and STRATS[k].get("pair_state", "idle") != "idle":
            await reply(u, f"{E.BOT} {E.LOSS} 本帳戶 {sym} 有策略在運行，"
                           f"請換一個閒置帳戶跑，或先 /stop {sym}")
            return
    try:
        spec = await get_spec(sym)
    except Exception:
        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return
    _, p1 = await okx_pos_ex(spec["iid"], "long", force=True)
    _, p2 = await okx_pos_ex(spec["iid"], "short", force=True)
    if p1 or p2:
        await reply(u, f"{E.BOT} {E.LOSS} 本帳戶 {sym} 已有持倉，拒絕執行（避免誤動你的倉）")
        return

    await reply(u, f"{E.BOT} 🔬 開始探測 {sym}　方向 {'／'.join(dirs)}\n"
                   f"每個方向測【限價單】+【觸發單】兩條路徑\n"
                   f"最小張數，測完自動清場，約 {len(dirs)*3} 分鐘\n"
                   f"（觸發單要等價格走 1 檔才會進場，最多等 90 秒，請耐心）")
    lines = await _apitest_run(u, sym, dirs)
    await _reply_long(u, [f"{E.BOT} 🔬 API 探測 {sym}　{VERSION}"], lines,
                      [f"時間：{hhmmss()}"])


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

    try:
        mb = os.path.getsize(path) / 1024 / 1024
        size_s = f"{mb:.1f} MB"
    except Exception:
        size_s = "-"
    await reply(u, f"{E.BOT} {E.OK} {sym} 振幅報表已產生\n"
                   f"範圍：{odt} ~ {ndt}（{got_days} 天）\n"
                   f"根數：{rows}｜大小：{size_s}\n"
                   f"檔名：{name}\n"
                   f"時間：{hhmmss()}{short_note}\n"
                   f"━━━━━━━━━━\n"
                   f"下載（Mac 終端機執行）：\n"
                   f"scp 1111bot:/srv/1111bot/data/{name} ~/Downloads/")

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
    await reply(u, f"{E.BOT} OKX原K｜{ACCT} {VERSION}\n使用說明\n━━━━━━━━━━\n"
        "/run 商品 方向 槓桿 保證金 A單埋伏% 兩單間距% TP% SL%\n"
        f"例：/run SUIUSDT L 1x 1 1.0 0 2 0.4\n週期依 /timeframe（目前 {ACCOUNT_TF}）\n"
        "/confirm 確認啟動\n/stop 商品 方向\n/stopall 停全部+清殘單\n"
        "/status 所有策略現況\n/summary 總表＋分幣種/方向戰報\n"
        "/tune [幣種] [天數] 調參報告（SL/緊貼建議）\n"
        "/apitest 幣種 [L|S|LS]　API 探測（限價+觸發兩條路徑，自動清場）\n"
        "/amp 幣種 年份  整年5m振幅報表 Excel 寄信\n"
        "/timeframe 查看/設定週期\n/coins 幣種\n"
        "━━━━━━━━━━\n"
        "【戰術】A限價 + B觸發 同時埋伏（反向同量）。\n"
        "兩單都成交時完全對沖，損益鎖死=-間距，與價格無關；\n"
        "價格衝出箱子時一邊被SL掃、一邊獨活順勢起飛。\n"
        "━━━━━━━━━━\n"
        "【SL】峰值緊貼，棘輪不後退\n"
        f"緊貼距離【固定 {float(HUG_FIXED*100):.3f}%】A/B 共用（下限 {MIN_HUG_TICKS} 檔）\n"
        f"手續費 A {float(FEE_A*100):.3f}%／B {float(FEE_B*100):.3f}%（只用於損益，不決定緊貼）\n"
        "峰值達 進場價±緊貼 才啟動，之前靜態SL緩衝區完整保留\n"
        "━━━━━━━━━━\n"
        "【TF】兩單都持倉→不動｜單邊有獲利→不動\n"
        "單邊無獲利→SL貼現價逼出場｜零持倉→撤單重新部署\n"
        "━━━━━━━━━━\n"
        f"查價 {PRICE_TICK_SEC}s｜WS：{ws_status()}\n"
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
            BotCommand("tune", "調參報告"),
            BotCommand("apitest", "API探測"),
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
    # WebSocket：價格逐筆推送（峰值永不漏頂）+ 私有頻道成交回報（loop 立刻醒來）。
    # 旁路設計：套件沒裝或連線失敗都只是降級回 REST 輪詢，不影響交易與保護。
    asyncio.create_task(ws_public_task())
    asyncio.create_task(ws_private_task())
    await startup_recover(app)
    for S in STRATS.values():
        try:
            if S.get("pair_state", "idle") != "idle":
                WS_WANT.add(S["spec"]["iid"])
        except Exception:
            pass

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
                    ("summary", cmd_summary), ("tune", cmd_tune),
                    ("apitest", cmd_apitest), ("amp", cmd_amp),
                    ("timeframe", cmd_timeframe), ("coins", cmd_coins)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.run_polling()

if __name__ == "__main__":
    main()

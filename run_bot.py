#!/usr/bin/env python3
# 設計此腳本的目的在於用bot取代我在交易所app上的一切手動行為，切記
"""1111bot｜OKX 多帳戶 Telegram bot（v20.0：網格每 2%、50 層、每 10 層一個帳戶、5 個帳戶 o1111o～o5555o；/runt、/run 一起）
  （v19.0：/run、/stop 實盤網格上線＝/runt 同一套規則，下單、成交、出場、損益全部以 OKX 為準）

【現在在用的】
  /run、/stop（v19.0）：實盤網格，規則跟 /runt 一模一樣；差別只在掛單、撤單、SL/TP 都送 OKX，成交、出場、手續費、資金費、強平價都查 OKX。
    規則寫在「v19.0 /run 實盤網格」那一區的註解（rg_ 開頭的函式）。
  /runt、/stoprunt：固定原點隱形網格（💰現貨做多＋🈴合約做空，模擬，只查 OKX 價格、帳戶，不下單）。
  規則寫在「v15.0 /runt 網格」那一區的註解（gd_ 開頭的函式），規格＝專案文件 1111bot-規劃-現貨網格.md、計畫＝1111bot-計畫-v17.0.md。
  v17.0：數量照 OKX 單位、下的時候檢查（帳戶模式、持倉、每層最少、可用）、資金從兩個帳戶真的可用模擬扣、帳戶費率、
  post-only、換格先撤再掛、SL/TP 改單照 OKX 額度（fd_ 開頭＝帳戶資金、巡邏）。v17.1 的 /run 會用同一套，成交、損益改查 OKX。
  /status、/summary、/coins、/amp、/check、/vps、/menu。

【鐵則】
  1. OKX 為唯一真相來源：損益、手續費、持倉，實盤一律查 OKX；OKX 沒有的才由 BOT 算。
  2. BOT 永遠不平倉；有持倉時由 1111 自己到 OKX 平倉。
  3. Telegram 與 WebSocket 皆為旁路：失效不影響策略本身。

【部署】GitHub 網頁上傳 run_bot.py → VPS：bash /srv/1111bot/deploy.sh test <行數> <版本>（只更新 o1111o）→ deploy.sh all。
  六個帳戶（o1111o～o6666o）共用這一份檔案，ACCT 由 systemd 的 EnvironmentFile 注入。
"""
import sys, hmac, base64, hashlib, json, time, asyncio, os, re, builtins, socket, uuid
from collections import deque
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP, ROUND_DOWN
from datetime import datetime, timezone, timedelta
import httpx
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeChat, ForceReply, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters
sys.path.insert(0, "/srv/1111bot")
from app.core import emoji as E

# ==================== 診斷紀錄器（/log 的資料來源） ====================
# 【為什麼要有】以前要 debug 只能 SSH 進 VPS 打 journalctl，還得記得 unit 名稱、
# 換算 UTC 時區 —— 門檻太高，而且腳本自己什麼都沒留。
# 這裡把腳本【已經在印的每一行】同時存進記憶體環狀緩衝，/log 直接從 TG 讀。
# 作法是覆蓋模組層的 print：不必去改幾十個呼叫點，也不會漏掉任何一行。
# 絕不影響原本的輸出 —— journald 照樣收得到。
DIAG_MAX     = 800          # 全部輸出保留幾行
DIAG_ERR_MAX = 250          # 異常行另外保留幾行（/log 預設看這個）
DIAG      = deque(maxlen=DIAG_MAX)
DIAG_ERR  = deque(maxlen=DIAG_ERR_MAX)
# 什麼叫「異常行」：API 錯誤碼、各種失敗、警告、修復、降速、例外
DIAG_PAT = re.compile(
    r"sCode=|code=-1|50011|51506|51280|51527|51000|"
    r"fail|失敗|警告|作廢|自我修復|降速|逾時|Traceback|Error|error|"
    r"空讀|中止|異常|漏看|補抓|裸倉")
_SYSPRINT = builtins.print


def print(*a, **kw):
    """覆蓋內建 print：照常輸出，同時留一份給 /log。"""
    try:
        s = " ".join(str(x) for x in a)
        # 自己格式化時間，不依賴後面才定義的 hhmmss —— 模組載入期也能用。
        # 一律 UTC+8，和 TG 顯示的時間對得起來（VPS 本身多半是 UTC）。
        line = (datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
                + " " + s)
        DIAG.append(line)
        if DIAG_PAT.search(s):
            DIAG_ERR.append(line)
    except Exception:
        pass                      # 紀錄器絕不可以把主程式弄掛
    _SYSPRINT(*a, **kw)


# ---------- API 用量與錯誤碼統計（回答「速率到底用了多少」） ----------
# 之前我只會【算】額度，沒有【量】過。這裡逐次記下每個端點的呼叫時刻，
# /log api 會算出任意 2 秒窗內的尖峰值 —— 直接對照 OKX 的限制。
API_HITS = {}                 # 端點 -> deque[呼叫時刻]
API_SCODE = {}                # sCode -> 次數
API_LAT  = {}                 # 端點 -> deque[毫秒]


def _api_note(path, ms=None, scode=None):
    try:
        k = path.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or path
        API_HITS.setdefault(k, deque(maxlen=400)).append(time.time())
        if ms is not None:
            API_LAT.setdefault(k, deque(maxlen=200)).append(int(ms))
        if scode:
            API_SCODE[scode] = API_SCODE.get(scode, 0) + 1
    except Exception:
        pass


def _peak_in_window(k, win=2.0):
    """該端點在任意 win 秒窗內的最大呼叫次數 —— 這就是會不會撞限制的指標。"""
    ts = list(API_HITS.get(k) or ())
    if not ts:
        return 0
    best = 0
    for i, t0 in enumerate(ts):
        n = 0
        for t1 in ts[i:]:
            if t1 - t0 <= win:
                n += 1
            else:
                break
        best = max(best, n)
    return best


# 原K 專用時間框架（皆整除 60 分鐘，起訖時刻自然對齊整點）
# v5.1：加 3m（1111）。v7.0：加 1m、2m。v7.1：只留 1m/3m/5m/10m/15m/30m（1111）。預設仍是 5m（ACCOUNT_TF）。
TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800}


VERSION = "v20.0.5"     # 腳本版本號：回報問題時請附上（/status 最後一行顯示）
BASE = "https://www.okx.com"
# v15.2（2026-10-18）：連 OKX 一律走 IPv4（固定 IP 13.159.58.20，搬主機也不變）。10/15 換 Lightsail 主機後 IPv6 換了一個新的，
#   OKX API Key 綁的是舊的 IPv4＋IPv6 → 走到 IPv6 的私有查詢（/coins 交易帳戶、實盤 /run）被 OKX 擋。公開資料不檢查 IP，所以價格、K 線照常。
OKX_V4 = "0.0.0.0"      # httpx 綁 IPv4（local_address）
OKX_FAM = socket.AF_INET  # WebSocket 只用 IPv4
ACCT = os.environ.get("ACCT", "o3333o")  # 由 systemd 注入
TZ8 = timezone(timedelta(hours=8))
ACCOUNT_TF = "5m"
STATE_FILE = f"/srv/1111bot/data/strategies_{ACCT}.json"


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

CHAT_ID = None
SHUTTING_DOWN = False
HTTP = None
SPEC_CACHE = {}

def inst_id(s): return s.replace("USDT", "") + "-USDT-SWAP"
def now8(): return datetime.now(TZ8)
def hhmmss(): return now8().strftime("%H:%M:%S")
def pct(v):
    """去尾零但不用科學記號：10 -> "10"（非 "1E+1"），0.50 -> "0.5"。"""
    d = Decimal(str(v)).normalize()
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return str(d)


def save_state():
    """存聊天室和 /tf 週期（strategies_{ACCT}.json）。v10.8：舊 /run 的策略、統計拿掉，只剩這兩個。
    /runt 自己存在 runtlayer_{ACCT}.json（rn_save）。關閉流程中不寫檔。"""
    if SHUTTING_DOWN:
        return
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"chat": CHAT_ID, "tf": ACCOUNT_TF}, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("save_state fail", e)

def load_state():
    """bot 重開：讀回聊天室和 /tf 週期（v10.8；原本在舊 /run 的 startup_recover 裡）。"""
    global CHAT_ID, ACCOUNT_TF
    try:
        if not os.path.exists(STATE_FILE):
            print("無存檔"); return
        data = json.load(open(STATE_FILE))
        CHAT_ID = data.get("chat"); ACCOUNT_TF = data.get("tf", "5m")
        if ACCOUNT_TF not in TF_SEC:                 # 存檔裡是已拿掉的週期 → 回到 5m
            ACCOUNT_TF = "5m"
    except Exception as e:
        print("讀存檔失敗", e)


# ---------- OKX API（全非同步，不阻塞事件迴圈） ----------
def ts_now():
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond//1000:03d}Z"

def sign(sec, ts, m, p, b=""):
    return base64.b64encode(hmac.new(sec.encode(), f"{ts}{m}{p}{b}".encode(), hashlib.sha256).digest()).decode()

async def api(method, path, body=None, acct=None):
    """私有查詢。v17.0：acct＝用哪一個帳戶的 Key（沒給＝這個帳戶；o1111o 的 bot 查 o2222o、o3333o 的餘額、帳戶模式、持倉）。"""
    a = acct or ACCT
    if not all(ACC.get(f"OKX_{a}_{k}") for k in ("API_KEY", "SECRET", "PASSPHRASE")):
        return {"code": "-1", "msg": f"{a} 沒有 API Key(accounts.env)", "data": []}
    b = json.dumps(body) if body else ""
    ts = ts_now()
    h = {"OK-ACCESS-KEY": ACC[f"OKX_{a}_API_KEY"],
         "OK-ACCESS-SIGN": sign(ACC[f"OKX_{a}_SECRET"], ts, method, path, b),
         "OK-ACCESS-TIMESTAMP": ts,
         "OK-ACCESS-PASSPHRASE": ACC[f"OKX_{a}_PASSPHRASE"],
         "Content-Type": "application/json"}
    t0 = time.time()
    try:
        r = await HTTP.request(method, BASE + path, headers=h, content=b)
        j = r.json()
        # 【v3.10】記下用量、延遲、錯誤碼 —— /log api 看得到，不必再用猜的
        sc = None
        if str(j.get("code")) not in ("0", "None", ""):
            d0 = (j.get("data") or [{}])
            sc = str((d0[0] if d0 else {}).get("sCode") or j.get("code"))
        _api_note(path, (time.time() - t0) * 1000, sc)
        return j
    except Exception as e:
        _api_note(path, (time.time() - t0) * 1000, "EXC")
        print("api fail", path, type(e).__name__, e)
        return {"code": "-1", "msg": str(e), "data": []}

async def pub(path, timeout=None):
    try:
        r = await (HTTP.get(BASE + path, timeout=timeout) if timeout else HTTP.get(BASE + path))
        return r.json()
    except Exception as e:
        print("pub fail", path, type(e).__name__, e)
        return {"code": "-1", "msg": str(e), "data": []}

# v12.5（1111 2026-10-12）：OKX 公開查詢（規格、現價）每 2 秒最多 20 次，整台 VPS（6 個帳戶）共用。
#   一個幣種查一次會撞到限制（/coins 排在後面的幣種查詢失敗、重開恢復被擋），改成一次查回全部 USDT 永續。
USDT_SWAP = "-USDT-SWAP"

def _spec_row(d):
    return {"iid": d["instId"], "tick": Decimal(d["tickSz"]), "lot": Decimal(d["lotSz"]),
            "minsz": Decimal(d["minSz"]), "ctval": Decimal(d["ctVal"]),
            "maxlev": Decimal(d["lever"]), "ctvalccy": d["ctValCcy"]}

async def okx_specs_all():
    """v12.5：一次查回全部 USDT 永續的規格，放進 SPEC_CACHE（1 次查詢，不管幾個幣種）。成功回 True。"""
    r = await pub("/api/v5/public/instruments?instType=SWAP")
    if str(r.get("code")) != "0" or not r.get("data"):
        print("[okx] 規格一次查全部失敗", r.get("code"), r.get("msg"))
        return False
    n = 0
    for d in r["data"]:
        iid = d.get("instId") or ""
        if not iid.endswith(USDT_SWAP):
            continue
        try:
            SPEC_CACHE[iid[:-len(USDT_SWAP)] + "USDT"] = _spec_row(d)
            n += 1
        except Exception:
            pass                                           # 上架前的幣種欄位是空的，略過
    return n > 0

async def okx_last_all(timeout=None):
    """v12.5：一次查回全部 USDT 永續的現價 {instId: 現價}（1 次查詢，不管幾個幣種）；查不到回 None。"""
    r = await pub("/api/v5/market/tickers?instType=SWAP", timeout)
    if str(r.get("code")) != "0" or not r.get("data"):
        print("[okx] 現價一次查全部失敗", r.get("code"), r.get("msg"))
        return None
    out = {}
    for x in r["data"]:
        try:
            if x.get("last"):
                out[x["instId"]] = Decimal(x["last"])
        except Exception:
            pass
    return out or None

async def get_spec(s):
    if s in SPEC_CACHE: return SPEC_CACHE[s]
    await okx_specs_all()                                  # v12.5：先一次查全部（之後的幣種都不用再查）
    if s in SPEC_CACHE: return SPEC_CACHE[s]
    iid = inst_id(s)
    r = await pub(f"/api/v5/public/instruments?instType=SWAP&instId={iid}")
    spec = _spec_row(r["data"][0])
    SPEC_CACHE[s] = spec
    return spec

async def get_last(iid):
    r = await pub(f"/api/v5/market/ticker?instId={iid}")
    return Decimal(r["data"][0]["last"])

# ==================== WebSocket 價格來源（v3.1） ====================
# 【為什麼需要】/runt 的成交、SL/TP 出場、最高毛利率都看逐筆成交價；REST 輪詢在「幾秒衝 2~3%」的針上
# 只有幾個樣本，WS trades 逐筆推送才不會漏。
# 【旁路原則】WS 掛掉自動降級回 REST 查價（1 秒一次，rt_px）。
# v12.5：WS 連著時，冷門幣種幾秒沒成交也不另外查（沒成交＝價格沒變）；WS 斷線才一次查回全部幣種（rt_bulk_px）。
WS_PUB_URL = "wss://ws.okx.com:8443/ws/v5/public"

WS_PX   = {}        # iid -> (Decimal 價格, epoch 時間戳)
WS_LIVE = {"pub": False, "pri": False}
WS_WANT = set()     # 目前需要訂閱的 iid
WS_CONN = {"t": 0.0, "rx": 0.0}   # v12.5：這一次公有 WS 連上的時間、最後一次收到逐筆成交的時間（任何幣種）
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


def _ws_push_px(iid, px):
    """WS 收到一筆成交價：更新快取，並【立刻】交給 /runt 判斷成交、SL/TP 出場、最高毛利率（逐筆，不漏頂）。"""
    try:
        d = Decimal(str(px))
    except Exception:
        return
    WS_PX[iid] = (d, time.time())
    if GD or RD:
        gd_on_tick(iid, d)      # v15.0：/runt 網格（現貨的價給現貨層、永續的價給空單）；v19.0：/run 實盤網格也是


async def ws_public_task():
    """公有頻道：訂閱 trades（逐筆成交），持續更新價格與峰值。"""
    wsl = _ws_lib()
    if not wsl:
        return
    while not SHUTTING_DOWN:
        try:
            async with wsl.connect(WS_PUB_URL, ping_interval=20, ping_timeout=10, family=OKX_FAM) as ws:   # v15.2：IPv4
                WS_LIVE["pub"] = True
                WS_CONN["t"] = WS_CONN["rx"] = time.time()   # v12.5：連線前收到的價格，要等這一次連線有新成交才算數
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
                        if m.get("data"):
                            WS_CONN["rx"] = time.time()
                        for t in (m.get("data") or []):
                            _ws_push_px(iid, t.get("px"))
                            health_lag(t.get("ts"))           # v13.9：OKX 成交到收到隔了多久
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[WS] 公有頻道斷線，3秒後重連", type(e).__name__, e)
            health_ws_off()                                    # v13.9：/vps 斷線次數
        WS_LIVE["pub"] = False
        await asyncio.sleep(3)


# ---------- v13.9（1111 2026-10-15 看過模擬畫面 OK）：/vps VPS 狀況 ----------
# 每支 bot 每 10 秒寫自己的狀態到 health/{帳戶}.json；/vps（任何一個帳戶打都一樣）讀整台 VPS（/proc）＋全部帳戶的狀態檔。
HEALTH_DIR   = "/srv/1111bot/data/health"
HEALTH_FILE  = f"{HEALTH_DIR}/{ACCT}.json"
HEALTH_EVERY = 10      # 每 10 秒寫一次自己的狀態
HEALTH_STALE = 60      # 狀態檔超過 60 秒沒更新＝那支 bot 沒有回應
VPS_ST_WARN  = 10      # 限速（st）超過 10% → ⚠️
VPS_WS_WARN  = 3       # 最近 1 小時 WS 斷線 3 次以上 → ⚠️
VPS_LAG_WARN = 2.0     # 價格延遲超過 2 秒 → ⚠️
HEALTH = {"off": [], "lag": 0.0, "lags": [], "cpu": []}
VPS_TYPE = {"v": None}

def health_ws_off():
    """v13.9：記一次 WS 斷線（/vps 最近 1 小時／今天）。"""
    HEALTH["off"].append(time.time())

def health_lag(ts):
    """v13.9：一筆成交從 OKX 成交到收到隔了多久（秒），記這 10 秒內最大的。"""
    try:
        d = time.time() - int(ts) / 1000
    except Exception:
        return
    if d > HEALTH["lag"]:
        HEALTH["lag"] = d

def health_day0(now=None):
    """台灣時間今天 00:00。"""
    return datetime.fromtimestamp(now or time.time(), TZ8).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

def health_load():
    """v13.9：bot 重開時接著算今天的斷線次數。"""
    try:
        with open(HEALTH_FILE) as f:
            d = json.load(f)
        t0 = health_day0()
        HEALTH["off"] = [float(x) for x in d.get("off") or [] if float(x) >= t0] + HEALTH["off"]
    except Exception:
        pass

def proc_kb(key, path="/proc/self/status"):
    """/proc 裡的一個數字（kB），讀不到＝0。"""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + ":"):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0

def health_write():
    """v13.9：寫自己的狀態：CPU（最近 1 分鐘平均）、記憶體、WS 斷線（最近 1 小時／今天）、價格延遲（最近 1 分鐘最大）。"""
    now = time.time()
    c = HEALTH["cpu"]
    c.append((now, time.process_time()))
    del c[:-7]
    cpu = (c[-1][1] - c[0][1]) / (c[-1][0] - c[0][0]) * 100 if len(c) > 1 and c[-1][0] > c[0][0] else 0.0
    t0 = health_day0(now)
    HEALTH["off"] = [x for x in HEALTH["off"] if x >= t0]
    HEALTH["lags"].append(HEALTH["lag"])
    del HEALTH["lags"][:-6]
    HEALTH["lag"] = 0.0
    d = {"acct": ACCT, "v": VERSION, "t": now, "cpu": round(cpu, 1), "rss": proc_kb("VmRSS"), "swap": proc_kb("VmSwap"),
         "ws_h": sum(1 for x in HEALTH["off"] if x >= now - 3600), "ws_d": len(HEALTH["off"]),
         "lag": round(max(HEALTH["lags"]), 1), "ws": bool(WS_LIVE["pub"]) or not WS_WANT, "off": HEALTH["off"][-500:]}
    os.makedirs(HEALTH_DIR, exist_ok=True)
    tmp = HEALTH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, HEALTH_FILE)

async def health_task():
    """v13.9：每 10 秒寫一次自己的狀態（/vps 用）。"""
    health_load()
    while not SHUTTING_DOWN:
        try:
            health_write()
        except Exception as e:
            print("[vps] 狀態寫檔失敗", type(e).__name__, e)
        await asyncio.sleep(HEALTH_EVERY)

def vps_cpu_raw():
    with open("/proc/stat") as f:
        return [int(x) for x in f.readline().split()[1:9]]   # user nice system idle iowait irq softirq steal

async def vps_cpu():
    """整台 VPS 這 1 秒：(CPU 用了幾 %, 被限速 st 幾 %)，跟 top 的 %Cpu(s) 一樣算法。"""
    a = vps_cpu_raw()
    await asyncio.sleep(1.0)
    b = vps_cpu_raw()
    d = [y - x for x, y in zip(a, b)]
    tot = sum(d) or 1
    return (d[0] + d[1] + d[2] + d[5] + d[6]) / tot * 100, d[7] / tot * 100

def vps_mem():
    """(記憶體已用 MB, 總共 MB, swap 已用 MB)，已用跟 free 指令一樣（扣掉 buff/cache）。"""
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0])
    used = m["MemTotal"] - m["MemFree"] - m.get("Buffers", 0) - m.get("Cached", 0) - m.get("SReclaimable", 0)
    return used // 1024, m["MemTotal"] // 1024, (m.get("SwapTotal", 0) - m.get("SwapFree", 0)) // 1024

def vps_disk():
    st = os.statvfs("/")
    g = 1024 ** 3
    return (st.f_blocks - st.f_bfree) * st.f_frsize / g, st.f_blocks * st.f_frsize / g

def vps_up():
    with open("/proc/uptime") as f:
        sec = int(float(f.read().split()[0]))
    return f"{sec // 86400}天{sec % 86400 // 3600}時"

async def vps_type():
    """AWS 機型（向這台機器自己的 metadata 問一次，記住；問不到就不寫）。"""
    if VPS_TYPE["v"] is None:
        VPS_TYPE["v"] = ""
        try:
            async with httpx.AsyncClient(timeout=1.0, trust_env=False) as cl:
                tk = (await cl.put("http://169.254.169.254/latest/api/token",
                                   headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})).text
                r = await cl.get("http://169.254.169.254/latest/meta-data/instance-type", headers={"X-aws-ec2-metadata-token": tk})
                if r.status_code == 200 and r.text and len(r.text) < 40:
                    VPS_TYPE["v"] = r.text.strip()
        except Exception:
            pass
    return VPS_TYPE["v"]

def vps_lines(cpu, st, mem, disk, up, typ, files, now):
    """v13.9 /vps 畫面（1111 2026-10-15 看過模擬畫面 OK）。files＝各帳戶的狀態 {帳戶: dict}。"""
    L = [f"🖥️ VPS｜{ACCT}"]
    head = ([f"機型 {typ}"] if typ else []) + ([f"開機 {up}"] if up else [])
    if head:
        L.append("|".join(head))
    L.append(RT_SEP)
    warn = []
    if cpu is not None:
        L.append(f"CPU {cpu:.1f}%|限速 {st:.1f}%")
        if st > VPS_ST_WARN:
            warn.append("⚠️ 限速中(CPU 額度用完)")
    if mem:
        L.append(f"記憶體 {mem[0]}MB/{mem[1]}MB|swap {mem[2]}MB")
    if disk:
        L.append(f"硬碟 {disk[0]:.1f}GB/{disk[1]:.1f}GB")
    L.append(RT_SEP)
    live = {a: d for a, d in files.items() if now - float(d.get("t") or 0) <= HEALTH_STALE}
    ks = [a for a in sorted(files) if a in live]
    col = lambda f: dict(zip(ks, rn_pad([f(live[a]) for a in ks])))
    c1 = col(lambda d: f"{float(d.get('cpu') or 0):.1f}%")
    c2 = col(lambda d: f"{int(d.get('rss') or 0) // 1024}MB")
    c3 = col(lambda d: f"{int(d.get('ws_h') or 0)}/{int(d.get('ws_d') or 0)}")
    c4 = col(lambda d: f"{float(d.get('lag') or 0):.1f}秒")
    for a in sorted(files):
        if a not in live:
            L.append(f"{a}|沒有回應")
            warn.append(f"⚠️ {a} 沒有回應")
            continue
        d = live[a]
        L.append(f"{a}|CPU {c1[a]}|記憶體 {c2[a]}|斷線 {c3[a]}|延遲 {c4[a]}")
        if not d.get("ws", True):
            warn.append(f"⚠️ {a} 價格連線中斷")
        if int(d.get("ws_h") or 0) >= VPS_WS_WARN:
            warn.append(f"⚠️ {a} 斷線 {int(d['ws_h'])}次(最近1小時)")
        if float(d.get("lag") or 0) > VPS_LAG_WARN:
            warn.append(f"⚠️ {a} 價格延遲 {float(d['lag']):.1f}秒")
    if not files:
        L.append("還沒有帳戶的狀態")
    return L + [RT_SEP] + (warn or ["✅ 正常"]) + [f"時間:{hhmmss()}|{VERSION}"]

def vps_files():
    out = {}
    try:
        for fn in sorted(os.listdir(HEALTH_DIR)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(HEALTH_DIR, fn)) as f:
                        d = json.load(f)
                    out[d.get("acct") or fn[:-5]] = d
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out

async def cmd_vps(u, c):
    """v13.9 /vps：整台 VPS 的狀況＋每個帳戶的 bot（在外面用手機也能看）。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    try:
        cpu, st = await vps_cpu()
    except Exception:
        cpu = st = None
    try:
        mem = vps_mem()
    except Exception:
        mem = None
    try:
        disk = vps_disk()
    except Exception:
        disk = None
    try:
        up = vps_up()
    except Exception:
        up = ""
    await reply(u, "\n".join(vps_lines(cpu, st, mem, disk, up, await vps_type(), vps_files(), time.time())))


def ws_status():
    if _WS_AVAILABLE is False:
        return "REST輪詢（未安裝websockets）"
    return "✅" if WS_LIVE["pub"] else "❌"


def align(px, tick, d):
    return (px / tick).to_integral_value(rounding=ROUND_FLOOR if d == "L" else ROUND_CEILING) * tick


# ---------- Telegram（旁路：永不阻塞交易） ----------
_BG = set()
TG_LIM = 3800      # v10.0（1111）：Telegram 單則上限 4096 字，超過整則被拒收（o2222o 組數多時 /status 沒有回應）→ 每則最多 3800 字，太長自動分頁
TG_SEP = "━━━━━━━━━━"

TG_EMO = 95        # v12.7（1111 2026-10-13）：/status 每一則最多 95 個 emoji（Telegram 手機版一則只用自己的圖案畫前 100 個，超過的用手機內建圖案 → 🛜 變黃、📣 變紅）

def tg_emo(s):
    """v12.7：一段文字有幾個 emoji（一個 emoji 算一個，變體符號、ZWJ 接起來的不另算；寧可多算，不可少算）。"""
    n, prev = 0, ""
    for ch in s:
        o = ord(ch)
        if o in (0xFE0F, 0x20E3, 0x200D) or 0x1F3FB <= o <= 0x1F3FF or prev == "\u200d":
            prev = ch
            continue
        if (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2300 <= o <= 0x23FF or 0x2B00 <= o <= 0x2BFF
                or 0x2190 <= o <= 0x21FF or 0x25A0 <= o <= 0x25FF
                or o in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x24C2, 0x2934, 0x2935, 0x3030, 0x303D, 0x3297, 0x3299)):
            n += 1
        prev = ch
    return n

def tg_pack(title, blocks, tail=(), lim=TG_LIM, emo=None):
    """v10.0：把一段一段（blocks＝每段幾行）整段裝進每一頁，不把同一段切開（一段本身就超過才逐行切）；
    分成兩頁以上時，每頁標題後面加 (1/N)，tail（例：時間）放最後一頁。
    v12.7：emo＝每一頁最多幾個 emoji（含標題；/status 用 TG_EMO），超過就換下一頁；沒給＝只看字數（跟原本一樣）。"""
    budget = lim - len(title) - 10
    ebud = emo - tg_emo(title) if emo else None
    size = lambda ls: sum(len(x) + 1 for x in ls)
    fits = lambda ls: size(ls) <= budget and (ebud is None or sum(tg_emo(x) for x in ls) <= ebud)
    pages, cur = [], []
    for b in blocks:
        b = list(b)
        while not fits(b):                                 # 一段就超過：逐行切
            if cur:
                pages.append(cur); cur = []
            k = 0
            while k < len(b) and fits(b[:k + 1]):
                k += 1
            k = max(k, 1)
            pages.append(b[:k]); b = b[k:]
        if cur and not fits(cur + b):
            pages.append(cur); cur = []
        if not cur and b and b[0] == "":                   # v11.3：新的一頁不要用空白列開頭（同幣種 S、L 之間的空白列剛好換頁）
            b[0] = RT_SEP
        cur += b
    tail = list(tail)
    if cur and not fits(cur + tail):
        pages.append(cur); cur = []
    cur += tail
    pages.append(cur)
    if len(pages) == 1:
        return ["\n".join([title] + pages[0])]
    return ["\n".join([f"{title}({i}/{len(pages)})"] + p) for i, p in enumerate(pages, 1)]

def tg_sep(ln):
    """v20.0.1：這一行是不是分隔線（━ 連起來至少 10 個；/summary 分幣種用 14 個）。"""
    return len(ln) >= 10 and set(ln) == {"━"}

def tg_split(t, lim=TG_LIM):
    """v10.0：一則太長 → 第一行當標題，在分隔線（━━━）處切成好幾頁；不長就原樣一則。
    v20.0.1：emoji 超過 TG_EMO（95）個也分頁（/summary 幣種多時，每個幣種前面多了 💛）。"""
    if len(t) <= lim and tg_emo(t) <= TG_EMO:
        return [t]
    lines = t.split("\n")
    segs, cur = [], []
    for ln in lines[1:]:
        if tg_sep(ln) and cur:
            segs.append(cur); cur = [ln]
        else:
            cur.append(ln)
    if cur:
        segs.append(cur)
    return tg_pack(lines[0], segs, (), lim, emo=TG_EMO)


async def reply(u, t):
    """指令回覆：失敗重試一次，再失敗只記錄，不拋出。
    v8.2：之前有「等你輸入」的回覆列（ForceReply）還沒收掉、現在也沒在等 → 這則回覆順便把它收掉。
    v10.0：超過 Telegram 上限就自動分頁，一頁一則依序送。"""
    ok = True
    for part in tg_split(t):
        ok = await _reply1(u, part) and ok
    return ok

async def _reply1(u, t):
    ec = getattr(u, "effective_chat", None)                 # 每日 summary 的假 Update 沒有 effective_chat
    chat = ec.id if ec else None
    clr = chat is not None and chat in FR_OPEN and chat not in WAIT
    for i in range(2):
        try:
            if clr:
                await u.message.reply_text(t, reply_markup=ReplyKeyboardRemove())
                FR_OPEN.discard(chat); return True
            await u.message.reply_text(t); return True
        except Exception as e:
            print("reply fail", i, type(e).__name__, e)
            await asyncio.sleep(2)
    return False


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


async def cmd_status(u, c):
    """/status：v11.7（1111）一個幣種一段（rn_coin_lines：L、S 合在一起、一個原點、S 的單／現價／L 的單；損益、資金費率不列）。
    v10.0：太長分頁，一段不切開。v10.8：舊 /run 的部分拿掉。
    v12.7（1111 2026-10-13）：每一則最多 95 個 emoji（超過換下一則），每個幣種都用 Telegram 自己的圖案（🛜 藍色、📣 黃色）。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    now = time.time()
    blocks = [[RT_SEP] + b for b in fd_status_blocks()] if (RD or GD) else []   # v20.0.5（1111 2026-10-30）：最上面 資金＝跟 /coins 一樣的畫面（每個帳戶一段）
    for G in sorted(RD.values(), key=lambda G: G["sym"]):  # v19.0：/run 實盤在前
        blocks.append([RT_SEP] + gd_status_lines(G, now))
    for G in sorted(GD.values(), key=lambda G: G["sym"]):  # v15.0：/runt 網格，一個幣種一段（gd_status_lines）
        blocks.append([RT_SEP] + gd_status_lines(G, now))
    blocks = blocks or [[RT_SEP, "目前沒有進行中的幣種"]]
    for part in tg_pack(f"❇️ status｜{ACCT}", blocks, [RT_SEP, f"時間:{hhmmss()}|{VERSION}"], emo=TG_EMO):   # v12.7：每一則最多 95 個 emoji
        await reply(u, part)


async def cmd_summary(u, c):
    """v8.7（1111）：/summary＝/runt 的本日損益（總表＋分幣種）。v10.8：舊 /run 的戰報拿掉。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    d = rn_today()
    real = gd_sum_msgs(d, M=GREAL, quiet=True)            # v19.0：/run 實盤在前（沒有實盤的紀錄、持倉＝不發）
    for m in real + gd_sum_msgs(d, quiet=bool(real)):      # v15.0：/runt 網格
        await reply(u, "\n".join(m))


# ---------- /log 診斷紀錄 ----------
# 【和 /summary 的分工】
#   /summary → 損益。看賺賠、看哪個幣種哪個方向表現好。
#   /check   → 腳本本身。API 回傳值、錯誤碼、速率用量、最近異常。
#              目的是 debug，不是看錢。
OKX_CODE_CN = {
    "50011": "速率超限 Too Many Requests",
    "51506": "此單天生不可修改（B單自動OCO）",
    "51280": "SL 觸發價越過現價",
    "51527": "附屬TP/SL 不存在或狀態不符",
    "51000": "參數錯誤",
    "51008": "保證金不足",
    "-1":    "網路／連線失敗",
    "EXC":   "送出時拋例外",
}
# OKX 官方限制，用來算「用掉幾成」
API_LIMIT = {"positions": (10, 2), "amend-algos": (20, 2), "order": (60, 2),
             "order-algo": (20, 2), "cancel-algos": (20, 2), "cancel-order": (60, 2),
             "orders-algo-pending": (20, 2), "orders-pending": (60, 2),
             "orders-history": (40, 2), "orders-algo-history": (20, 2), "positions-history": (10, 2)}


def _log_api_lines():
    out = ["━━━ API 用量（任意 2 秒窗的尖峰）━━━"]
    rows = []
    for k in sorted(API_HITS.keys()):
        peak = _peak_in_window(k, 2.0)
        lat  = list(API_LAT.get(k) or ())
        med  = sorted(lat)[len(lat) // 2] if lat else 0
        lim  = API_LIMIT.get(k)
        if lim:
            use = f"{peak}/{lim[0]}　{peak / lim[0] * 100:.0f}%"
            flag = "🔴" if peak >= lim[0] else ("⚠️" if peak >= lim[0] * 0.8 else "✅")
        else:
            use, flag = f"{peak}/—", "　"
        rows.append(f"{flag} {k:<22}{use:<12}延遲中位 {med}ms")
    out += rows or ["（尚無紀錄）"]
    out.append("")
    out.append("━━━ OKX 錯誤碼累計 ━━━")
    if API_SCODE:
        for code, n in sorted(API_SCODE.items(), key=lambda kv: -kv[1]):
            out.append(f"{code} × {n}　{OKX_CODE_CN.get(code, '')}")
    else:
        out.append("✅ 一次都沒有")
    return out


def _check_health_lines():
    """一頁健檢（v10.8：舊 /run 的規則、緊貼、交易紀錄拿掉，改成 /runt）。沒資料一律標 ⚪，不能長得像正常。"""
    out = []
    lat = [v for dq in API_LAT.values() for v in dq]
    med = sorted(lat)[len(lat) // 2] if lat else None
    ws_pub = bool(WS_LIVE.get("pub"))
    if med is None:
        out.append(f"{'🟢' if ws_pub else '🔴'} 行情　　WS {ws_status()}｜尚無 API 呼叫紀錄（剛重啟？）")
    else:
        ico = "🔴" if not ws_pub else ("🟡" if med >= 500 else "🟢")
        note = "" if ws_pub else "　← WS 斷線，查價退回 REST（1 秒一次）"
        out.append(f"{ico} 行情　　WS {ws_status()}｜OKX 延遲中位 {med}ms{note}")
    worst = 0.0; parts = []
    for k, (lim, win) in API_LIMIT.items():
        if k not in API_HITS:
            continue
        peak = _peak_in_window(k, win)
        r = peak / lim
        worst = max(worst, r)
        if r >= 0.5:
            parts.append(f"{k} {peak}/{lim}({r*100:.0f}%)")
    if not API_HITS:
        out.append("⚪ 速率　　尚無呼叫紀錄")
    else:
        ico = "🔴" if worst >= 1 else ("🟡" if worst >= 0.8 else "🟢")
        out.append(f"{ico} 速率　　尖峰 {worst*100:.0f}%" + ("｜" + "　".join(parts) if parts else "（全部低於一半）"))
    if not API_SCODE:
        out.append("🟢 API錯誤　一次都沒有")
    else:
        tally = "　".join(f"{k}×{v}" for k, v in sorted(API_SCODE.items(), key=lambda kv: -kv[1])[:4])
        out.append(f"{'🔴' if '50011' in API_SCODE else '🟡'} API錯誤　{tally}")
    if RD:                                                 # v19.0：/run 實盤
        live = sum(len(gd_held(G)) for G in RD.values())
        ago = time.time() - RG_SYNC["t"] if RG_SYNC["t"] else None
        ico = "🟢" if ago is not None and ago < 5 else "🔴"
        out.append(f"{ico} run實盤　{len(RD)} 個幣種｜持倉 {live} 單｜OKX對帳 " + (f"{ago:.1f}秒前" if ago is not None else "還沒有")
                   + f"｜私有WS {'✅' if any(RG_WS.values()) else '❌'}")
    live = sum(len(gd_held(G)) for G in GD.values())     # v15.0：/runt 網格
    out.append(f"🟢 runt　　{len(GD)} 個幣種｜持倉 {live} 單")
    out.append(f"{'🟢' if not DIAG_ERR else '🟡'} 異常　　{len(DIAG_ERR)} 筆（緩衝共 {len(DIAG)} 行）")
    return out


async def cmd_check(u, c):
    """/check —— 唯一的診斷入口。不帶參數給一頁健檢，有問題再往下查。v10.8：舊 /run 的 sl、rule、data 拿掉。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = (c.args or [])
    arg = (a[0].lower() if a else "")
    head = [f"{E.BOT} 🩺 健檢　{VERSION}｜{ACCT}"]
    tail = [f"時間：{hhmmss()}（UTC+8）"]

    if arg == "api":
        await _reply_long(u, head, _log_api_lines(), tail); return
    if arg in ("log", "all") or arg.isdigit():
        n = int(arg) if arg.isdigit() else 60
        n = max(5, min(n, DIAG_MAX))
        body = [f"━━━ 最近 {n} 行原始輸出 ━━━"] + list(DIAG)[-n:]
        await _reply_long(u, head, body, tail); return

    body = _check_health_lines()
    if DIAG_ERR:
        body.append("")
        body.append(f"━━━ 最近 {min(len(DIAG_ERR), 8)} 筆異常 ━━━")
        body += list(DIAG_ERR)[-8:]
    body += ["", "━━━ 要看細節 ━━━",
             "/check api　 API用量與錯誤碼",
             "/check log　 原始輸出（/check 100 = 最近100行）"]
    await _reply_long(u, head, body, tail)


# ---------- 共用工具（v7.4 起放在 /test1 區；v9.7 刪除 /amp、/test1、/stoptest1 後，留下 /runt、/price 共用的部分） ----------
# 送單時間、查價（WS 優先、REST 限速）、時間與價格格式、TG 依序發送。/runt 的規則見下面的 /runt 區。
RT_LAT      = 0.018         # 送單時間（秒）＝VPS 到 OKX 實測平均 18 ms（2026-09-30 量 20 次：14～27 ms）
RT_REST_GAP = 1.0           # WS 斷線時，一次查全部的現價最快 1 秒一次（v12.5：每個帳戶一次，不再每組各查；6 個帳戶共用 REST 額度 20 次/2 秒）
RT_SAVE_SEC = 10            # 改價次數每 10 秒存檔一次
RT_BG = set()               # 背景工作（保留參照，避免中途被回收）
RT_SEP = "━━━━━━━━━━"


def rt_bg(coro):
    """背景執行（不卡住查價迴圈）。"""
    t = asyncio.create_task(coro)
    RT_BG.add(t); t.add_done_callback(RT_BG.discard)
    return t


def rt_q(v, tick):
    """價格對齊 tick 的小數位數（1.234 → 1.2340），只影響顯示。"""
    try:
        return Decimal(str(v)).quantize(tick)
    except Exception:
        return v

def rt_p2(v):
    """% 參數至少 2 位小數（1 → 1.00、0.5 → 0.50、0.125 → 0.125），上下行好對齊。"""
    s = pct(v)
    dec = len(s.split(".")[1]) if "." in s else 0
    return s if dec >= 2 else f"{Decimal(s):.2f}"

def rt_hold_str(sec):
    sec = max(0, int(sec))
    if sec < 60: return f"{sec}秒"
    if sec < 3600: return f"{sec // 60}分{sec % 60:02d}秒"
    return f"{sec // 3600}時{sec % 3600 // 60:02d}分{sec % 60:02d}秒"


def rt_amb(px, side, off, tick):
    """掛單價：L＝現價×(1−埋伏%) 往下取、S＝現價×(1+埋伏%) 往上取（一律離現價遠一點，不縮短距離）。"""
    if side == "L":
        return align(px * (1 - off / 100), tick, "L")
    return align(px * (1 + off / 100), tick, "S")


async def rt_send(app, chat, text):
    for part in tg_split(text):                            # v10.0：太長分頁
        for i in range(2):
            try:
                await app.bot.send_message(chat, part); break
            except Exception as e:
                print("[runt] tg fail", i, e); await asyncio.sleep(2)

def rt_chain(T, fn):
    """同一組 test 的 TG 訊息排隊依序送出，不卡住迴圈、也不會先後顛倒。fn＝沒有參數的 async 函式。"""
    prev = T.get("tail")
    async def run():
        if prev is not None and not prev.done():
            await asyncio.wait([prev], timeout=15)
        try:
            await fn()
        except Exception as e:
            print("[test] send fail", type(e).__name__, e)
    T["tail"] = rt_bg(run())


RT_WS_QUIET = 60     # v12.5：WS 連著，但所有幣種 60 秒都沒有任何成交 → 當作 WS 沒在動，改用一次查全部
PX_BULK = {"t": 0.0, "got": 0.0, "px": {}, "busy": False}   # v12.5：一次查全部的現價（這個帳戶所有組共用）

def ws_fresh(v, now):
    """v12.5：WS 的價格能不能直接用：3 秒內的；或 WS 連著、還在收成交，而且這筆是這一次連線收到的（沒成交＝價格沒變）。"""
    if not v:
        return False
    if now - v[1] < 3.0:
        return True
    return WS_LIVE["pub"] and v[1] >= WS_CONN["t"] and now - WS_CONN["rx"] < RT_WS_QUIET

async def rt_bulk_px(iid):
    """v12.5：WS 斷線（或這個幣種這一次連線還沒有成交）時的現價：一次查回全部幣種，這個帳戶所有組共用，
    最快 1 秒查一次（6 個帳戶加起來每 2 秒最多 12 次，在 OKX 每 2 秒 20 次以內）；查到的 3 秒內都用它。"""
    now = time.time()
    if now - PX_BULK["t"] >= RT_REST_GAP and not PX_BULK["busy"]:
        PX_BULK["t"], PX_BULK["busy"] = now, True
        try:
            d = await okx_last_all(timeout=3.0)
            if d:
                PX_BULK["px"], PX_BULK["got"] = d, time.time()
        finally:
            PX_BULK["busy"] = False
    px = PX_BULK["px"].get(iid)
    return px if px is not None and time.time() - PX_BULK["got"] < 3.0 else None


# ---------- v12.5（1111 2026-10-12）：重開恢復不清掉任何一組 ----------
# 原本重開時每一組各自向 OKX 查規格（模擬埋伏中的再查現價），5 個帳戶同時重開會撞到 OKX 每 2 秒 20 次；
# 被擋掉的組不恢復，而且存檔被覆蓋 → 永遠回不來（有持倉的也一樣）。現在：
#   ① 恢復前規格、現價各一次查全部（rec_okx_ready），查不到每 3 秒再查，最多 10 次；
#   ② 存檔裡還沒恢復成功的那一筆原樣保留（GD_HOLD），背景每 10 秒再試，恢復了發通知。
REC_RETRY = 10

async def rec_okx_ready(syms, need_px=False):
    """v12.5：重開恢復前，規格（need_px：還有現價）先一次查全部；查不到每 3 秒再查，最多 10 次。成功回 True。"""
    for i in range(10):
        ok = all(s in SPEC_CACHE for s in syms) or await okx_specs_all()
        if ok and need_px:
            await rt_bulk_px("")
            ok = time.time() - PX_BULK["got"] < 3.0
        if ok:
            return True
        print(f"[recover] OKX 規格／現價這次沒查到,3 秒後再試({i + 1}/10)")
        await asyncio.sleep(3)
    return False


# ---------- 出場規則（rn_rule：每一單的 SL/TP；/runt 網格用，v17.1 的 /run 也用同一個）----------
# v17.0（1111 2026-10-19）：舊 /run（v11 佈局實盤）、v14 舊 /runt、接續帳戶拿掉；這一區只留網格還在用的常數、函式。
RN_STEP    = 0.25              # 每秒查價 4 次（固定）
RN_TP_STEP = 0.5               # v18.3（1111 2026-10-21）：TP 每秒最多移動 2 次（v13.9～v18.2 每秒 1 次）
RN_SL_STEP = 1.0               # v18.1（1111 2026-10-20）：SL 每秒最多移動 1 次（v17.0～v18.0.1 每秒 2 次），現貨、合約都一樣
RN_ARM     = Decimal("0.80")   # v18.1（1111 2026-10-20）：做空（S）最高毛利率 > 0.80% 才第一次設 SL/TP（v17.0～v18.0.1 0.50%）
GD_LONG_ARM = Decimal("1.50")  # v18.3（1111 2026-10-21）：做多（L、U 現貨、D 合約）最高毛利率 > 1.50% 才第一次設 SL/TP
                               #   （v18.1～v18.2 現貨 1.40%、D 0.80%）
RN_SL_DIST = Decimal("0.20")   # v18.1（1111 2026-10-20）：SL＝距離現價 −0.20%（做空＝現價 +0.20%）；v9.5～v18.0.1 是 最高毛利率 −0.20%
RN_TP_DIST = Decimal("0.20")   # v18.1（1111 2026-10-20）：移動 TP＝距離現價 +0.20%（做空＝現價 −0.20%）；v13.9～v18.0.1 是 最高毛利率 +0.10%
RN_TP_FIRST = Decimal("0.30")  # v18.3（1111 2026-10-21）：第一次設的 TP＝距離現價 +0.30%（做空＝現價 −0.30%），L、S、U、D 都一樣
RN_SL_MIN  = Decimal("0.20")   # SL 最低 +0.20%（保本；v11.3 起，v18.1 沿用）
RN_MMR_DEF = Decimal("0.01")   # v9.3 強平：查不到 OKX 維持保證金率時先用 1%（OKX 多數幣種第 1 檔）
RN_FEE_IN  = Decimal("0.02")   # 手續費 0.07%＝限價進場 0.02%
RN_FEE_OUT = Decimal("0.05")   #              ＋市價出場 0.05%
RN_FUND_Q   = 60              # v8.2 資金費：每 60 秒向 OKX 查一次資金費率（公開資料，不用 API key、不下單）
RN_FUND_PRE = 15              #            結算前 15 秒內再查一次，拿最接近結算的費率
RN_FILE    = f"/srv/1111bot/data/runtlayer_{ACCT}.json"   # 不可用 runt_{ACCT}.json（那是 v7.2 撤單保險會讀的檔）
RN_DAY_FILE = f"/srv/1111bot/data/runtday_{ACCT}.json"    # 本日已實現（同幣種同方向，v8.2）
RN_DAY_KEEP = 30               # 保留最近 30 天
RN_LOG_FILE = f"/srv/1111bot/data/runtlog_{ACCT}.jsonl"   # v8.7：每一張出場一行（/summary 出場統計、日後數據分析），保留 30 天


class RnMode:
    """v11.0：/runt（模擬）、/run（實盤）各一份：進行中的組、本日已實現、存檔、本日檔、逐單紀錄。
    規則、畫面共用同一套 rn_ 函式；一組（T）用 T["M"] 指到自己是哪一份。"""
    def __init__(self, name, layer, day, log):
        self.name = name
        self.G = {}                # key（'WLDUSDT L'）-> 一組
        self.DAY = {}              # 日期 -> 幣種 -> L/S -> 損益累計
        self.file, self.dayf, self.logf = layer, day, log

SIM = RnMode("runt", RN_FILE, RN_DAY_FILE, RN_LOG_FILE)


def rn_lev(T, lvl):
    """v9.3：這一層的槓桿。XX＝第 n 層 n+1 倍（L0 1X、L1 2X…L9 10X）；否則每一層一樣。每一層保證金都一樣。
    v9.9：YX＝2 倍（L0 2X…L9 20X）、ZX＝5 倍（L0 5X…L9 50X）。T["xx"]＝倍數（0＝固定槓桿）。
    v10.2：改名 XX1～XX5（倍數 1～5，XX3、XX4 新增），算法不變。"""
    return Decimal(lvl + 1) * int(T["xx"]) if T.get("xx") else T["lev"]


def rn_plev(T, p):
    return p.get("lev") or rn_lev(T, p["lvl"])


def rn_gliq(T, side, ps):
    """v11.3（1111：照 OKX，同幣種同方向合成一個倉位、一起強平）：ps 這幾單合起來的預估強平價（OKX 逐倉公式）。
    做多＝(倉位價值−保證金餘額)÷(數量×(1−維持保證金率−平倉手續費率))；做空＝(倉位價值＋保證金餘額)÷(數量×(1＋維持保證金率＋平倉手續費率))。
    保證金餘額＝各單保證金−開倉手續費＋資金費（收＋、付−）。往離現價近的方向取 tick（保守）。做多算出來 ≤0＝不會強平（None）。
    模擬：保證金＝每單 U、開倉手續費 0.02%、平倉 0.05%；實盤：保證金、開倉手續費＝OKX 實際，平倉＝OKX 吃單費率。"""
    if not ps:
        return None
    real = bool(T.get("real"))
    mmr = T.get("mmr") or RN_MMR_DEF
    fc = (T.get("fee_t") or Decimal("0.0005")) if real else RN_FEE_OUT / 100
    Q = N = MB = Decimal(0)
    for p in ps:
        q = p["qty"]
        Q += q
        N += q * p["ent"]
        if real:
            mg = p.get("mg") or p["ent"] * q / rn_plev(T, p)
            fee = p.get("fee_in") or Decimal(0)
        else:
            mg = p.get("amt") or T["amt"]                  # v17.0.3：網格自動調高的層＝那一層自己的保證金
            fee = q * p["ent"] * RN_FEE_IN / 100             # v18.5.2（1111 2026-10-22）：開倉手續費照實際倉位（數量×進場價），不是 保證金×設計槓桿
        MB += mg - fee + (p.get("fu") or Decimal(0))
    if Q <= 0:
        return None
    if side == "L":
        liq = (N - MB) / (Q * (1 - mmr - fc))
        return align(liq, T["tick"], "S") if liq > 0 else None
    return align((N + MB) / (Q * (1 + mmr + fc)), T["tick"], "L")


RN_MMR = {}                                    # v9.3：幣種 → OKX 逐倉第 1 檔維持保證金率（快取）

async def rn_mmr(spec):
    """v9.3：向 OKX 查這個幣種逐倉的維持保證金率（position-tiers 第 1 檔 mmr，公開資料）；查不到用 1%。"""
    iid = spec["iid"]
    if iid in RN_MMR:
        return RN_MMR[iid]
    fam = iid[:-5] if iid.endswith("-SWAP") else iid
    try:
        r = await pub(f"/api/v5/public/position-tiers?instType=SWAP&tdMode=isolated&instFamily={fam}")
        rows = [x for x in r.get("data") or [] if x.get("mmr")]
        rows.sort(key=lambda x: int(x.get("tier") or 0))
        v = Decimal(rows[0]["mmr"]) if rows else None
        if v is not None and 0 < v < 1:
            RN_MMR[iid] = v
            return v
        print("[runt] mmr 查不到,先用", RN_MMR_DEF, iid)
    except Exception as e:
        print("[runt] mmr query fail", iid, type(e).__name__, e)
    return RN_MMR_DEF


def rn_g(side, ent, px):
    """毛利率（%）＝價格漲跌%，不乘槓桿。"""
    if side == "L":
        return (px / ent - 1) * 100
    return (1 - px / ent) * 100

def rn_lvl(side, ent, g, tick):
    """毛利率 g% 對應的價格，往獲利方向取 tick（SL 一定 ≥ 該毛利率）。"""
    if side == "L":
        return align(ent * (1 + g / 100), tick, "S")
    return align(ent * (1 - g / 100), tick, "L")


def rn_sp(v):
    """帶正負號、2 位小數的 %：+0.12%、-0.07%。"""
    v = Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{'-' if v < 0 else '+'}{abs(v):.2f}%"

def rn_su(v):
    """帶正負號、4 位小數的 U。"""
    v = Decimal(v).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return f"{'-' if v < 0 else '+'}{abs(v):.4f}U"

def rn_pad(vals):
    """同一欄靠右對齊：前面補數字寬的空白（U+2007）。"""
    if not vals:
        return []
    w = max(len(v) for v in vals)
    return [" " * (w - len(v)) + v for v in vals]


def rn_q2(v):
    """% 照畫面取到 0.01%（合計＝各單畫面上的數字直接相加）。"""
    return Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def rn_q4(v):
    """U 照畫面取到 0.0001U。"""
    return Decimal(v).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def rn_today(t=None):
    """本日＝台灣時間（UTC+8）00:00～24:00。"""
    return datetime.fromtimestamp(time.time() if t is None else t, TZ8).strftime("%Y-%m-%d")


def rn_hold(sec):
    """v11.3（1111）：時間長度 15分56秒、1時03分16秒、2天3時04分05秒（進場成交的埋伏、出場通知的持倉、本輪結束的歷時）。"""
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    return (f"{d}天" if d else "") + (rt_hold_str(r) if not d else f"{r // 3600}時{r % 3600 // 60:02d}分{r % 60:02d}秒")


def rn_log_read(d, M=None):
    """v8.7：讀某一天（台灣時間）的逐單出場紀錄。v11.0：M＝SIM／REAL。"""
    out = []
    logf = (M or SIM).logf
    try:
        if os.path.exists(logf):
            with open(logf) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("d") == d:
                        out.append(r)
    except Exception as e:
        print("[runt] log read fail", e)
    return out

def rn_log_prune(M=None, path=None):
    """v8.7：bot 重開時只留最近 30 天的逐單紀錄。v11.0：M＝SIM／REAL；path＝其他同格式的檔（實盤事件紀錄）。"""
    logf = path or (M or SIM).logf
    try:
        if not os.path.exists(logf):
            return
        keep = (now8() - timedelta(days=RN_DAY_KEEP)).strftime("%Y-%m-%d")
        lines = []
        with open(logf) as f:
            for x in f:
                try:
                    if x.strip() and (json.loads(x).get("d") or "") >= keep:
                        lines.append(x)
                except Exception:
                    continue
        tmp = logf + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(lines)
        os.replace(tmp, logf)
    except Exception as e:
        print("[runt] log prune fail", e)


async def rn_fund_query(T):
    """v8.2：向 OKX 查這個幣種的資金費率與下次結算時間（公開資料）。"""
    f = T["fund"]
    try:
        r = await pub(f"/api/v5/public/funding-rate?instId={T['iid']}")
        d = r["data"][0]
        t, rate = int(d["fundingTime"]) / 1000, Decimal(d["fundingRate"])
        if (f["last"] is not None and t <= f["last"]) or t < time.time() - 2:   # OKX 還沒換到下一期（或 bot 剛重開）→ 不要重複算同一次結算
            f["q"] = time.time() + 5
            return
        f["t"], f["rate"] = t, rate
    except Exception as e:
        print("[runt] funding query fail", T["sym"], type(e).__name__, e)
    finally:
        f["qt"] = time.time()
        f["busy"] = False


def rn_rule(T, p, px, now, arm=None):
    """v18.1（1111 2026-10-20）：每次查價（0.25 秒）判斷一次，現貨、合約同一套（只有第一次設的門檻不一樣）：
    第一次設：最高毛利率（逐筆）> arm（v18.3：做多 L、U、D GD_LONG_ARM 1.50%、做空 S RN_ARM 0.80%）→ 同時設 SL＝距離現價 −0.20%、
      TP＝距離現價 +0.30%（v18.3 RN_TP_FIRST）。
    之後：SL＝距離現價 −0.20%（每秒最多 1 次）、TP＝距離現價 +0.20%（v18.3：每秒最多 2 次），都只往獲利方向移動。
    做空方向相反（SL＝現價 +0.20%、TP＝現價 −0.20%）。SL 至少離現價 0.20%（往外取 tick）；SL 最低 +0.20%（保本，RN_SL_MIN）；
    SL 至少離現價 1 跳（保本價離現價不到 1 跳＝這次不設／不動）。
    以前（v9.0～v18.0.1）：SL＝最高毛利率 −0.20%、TP＝最高毛利率 +0.10%（照最高，不是照現價）。"""
    arm = RN_ARM if arm is None else arm
    side, ent, tick = p["side"], p["ent"], T["tick"]
    isL = side == "L"
    hi = p["mfe"]                                          # 最高毛利率（逐筆）
    p["hi"] = hi
    cur_sl, cur_tp, sl_g, tp_g = rn_cur_sltp(p)
    if cur_sl is None and hi <= arm:
        return
    better = lambda a, b: (a > b) if isL else (a < b)      # a 比 b 更往獲利方向
    ds, dt = RN_SL_DIST / 100, (RN_TP_FIRST if cur_tp is None else RN_TP_DIST) / 100   # v18.3：第一次設 TP +0.30%、移動 +0.20%
    if isL:
        sl_px, tp_px = align(px * (1 - ds), tick, "L"), align(px * (1 + dt), tick, "S")
    else:
        sl_px, tp_px = align(px * (1 + ds), tick, "S"), align(px * (1 - dt), tick, "L")
    floor = rn_lvl(side, ent, RN_SL_MIN, tick)
    if better(floor, sl_px):
        sl_px = floor                                      # SL 最低 +0.20%（保本）
    lim = px - tick if isL else px + tick                  # SL 至少離現價 1 跳
    if better(sl_px, lim):
        sl_px = cur_sl                                     # 保本價離現價不到 1 跳 → 這次不設／SL 不動
    if cur_sl is not None and (sl_px is None or not better(sl_px, cur_sl)):
        sl_px = cur_sl                                     # 只往獲利方向
    if cur_sl is not None and sl_px != cur_sl and now - p.get("sl_t", 0.0) < RN_SL_STEP - 0.05:
        sl_px = cur_sl                                     # SL 離上次移動不到 1 秒 → 這次不動
    if cur_tp is not None and not better(tp_px, cur_tp):
        tp_px = cur_tp                                     # 只往獲利方向
    if cur_tp is not None and tp_px != cur_tp and now - p.get("tp_t", 0.0) < RN_TP_STEP - 0.05:
        tp_px = cur_tp                                     # TP 離上次移動不到 1 秒 → 這次不動
    if sl_px is None:
        if T.get("real"):
            p["want"] = None                               # 這次不設 → 舊的待送也作廢
        return
    if tp_px != cur_tp:
        p["tp_t"] = now                                    # TP 這次移動（第一次設也算）的時間
    if sl_px != cur_sl:
        p["sl_t"] = now                                    # SL 這次移動（第一次設也算）的時間
    if T.get("real"):                                      # 實盤：算好的 SL/TP 交給送單（OKX 接受才算數，移動次數那時才記）
        same = sl_px == cur_sl and tp_px == cur_tp
        p["want"] = None if (same and (p.get("aid") or p.get("acl_wait"))) else (sl_px, tp_px)
        return
    if sl_px == cur_sl and tp_px == cur_tp:
        return
    if cur_sl is None:
        p["t_arm"] = now
        p["arm_px"] = sl_px
    elif sl_px != cur_sl:
        p["n_mv"] += 1
    if cur_tp is not None and tp_px != cur_tp:              # TP 移動次數
        p["n_tp"] = p.get("n_tp", 0) + 1
    p["pend"] = (sl_px, tp_px, rn_g(side, ent, sl_px), rn_g(side, ent, tp_px), now + RT_LAT)


def rn_cur_sltp(p):
    """畫面用：最新決定的 SL/TP（剛送出還沒生效的也算）。"""
    if p["pend"]:
        return p["pend"][0], p["pend"][1], p["pend"][2], p["pend"][3]
    return p["sl"], p["tp"], p["sl_g"], p["tp_g"]


RN_ORD_MK = "🔹"   # v14.4（1111 2026-10-16）：掛單列 L02 時:分:秒|掛單價|掛單中🔹（v11.7～v14.3 ✏️）；/status、/stoprunt 一起


def rn_why_nm(why, n_tp=0):
    """v13.7（1111 2026-10-15）：出場原因畫面上的名稱，跟 /summary 一樣（紀錄裡照舊存 SL／SL(移動)／TP）。"""
    if why == "TP":
        return "移動TP" if int(n_tp or 0) else "設定TP"
    return {"SL": "設定SL", "SL(移動)": "移動SL"}.get(why, why)


def rn_mdt(t):
    """v10.1：日期＋時間 10/06 09:12:03（台灣時間）。"""
    return datetime.fromtimestamp(t, TZ8).strftime("%m/%d %H:%M:%S")


RN_SAVE_GAP = 1.0                       # v13.8：有事情最快 1 秒存一次（全部的組一起存）


# ==================== v15.0（1111 2026-10-17）：/runt 網格——固定原點隱形網格（現貨做多＋合約做空，模擬） ====================
# ※ v18.0（1111 2026-10-20）換新策略：現貨格、D 方向、合約帳戶、槓桿、現貨出場都改了 → 以下面 GD_DIRS 後面的 v18.0 說明為準；
#   這一段（v15～v17）留著當歷史（格子、SL/TP 數字、空單槓桿 +133%、GD_TGT 都是舊的）。
# 規格＝專案文件 claude/1111bot-規劃-現貨網格.md（一～十）。這一版只改 /runt、/stoprunt（含 /status、/summary 裡 /runt 的部分）；
#   v17.0：舊 /run、/stop 拿掉；出場規則 rn_rule 留著（v17.1 的 /run 用同一個）。
# 方向（1111 2026-10-17 恢復）：L、S、U 任意組合（LSU、S、US…，順序隨便打，畫面照打的顯示）：
#   L＝💰現貨 L00～L33（原點和原點下面）、U＝💰現貨 U01～U33（原點上面）、S＝🈴空單 S01～S33（原點上面，跟 U 同價）。沒開的那一邊不掛。
# 原點：有 L＝現價×(1−埋伏點%) 掛 💰L00 現貨買，成交價＝🩵原點；沒有 L＝/runt 那一刻查到的現價直接當原點（不買，埋伏點% 不用）。
#   原點到 /stoprunt 都不動；沒有「輪」、全部出場也不等 111 秒、不換原點。
# 格子：離原點每 3%（GD_STEP）：L01～L33（−3%～−99%）、U01～U33、S01～S33（+3%～+99%）；超過 ±99% 不掛。
#   現貨買單價往下取 tick、空單往上取 tick。
# 掛單（像洋蔥夾著現價）：隨時「現價下面最近一格、沒有持倉的」掛 1 張現貨買；「現價上面最近一格 S、沒有持倉的」掛 1 張空；
#   價格動 → 掛單跟著換到最近的那一格。一律限價（maker）；掛單價已經被現價穿過＝不掛。過了送單時間（RT_LAT）才生效。
# 出場：每一層各自照現在的規則（rn_rule，跟 /run 同一個函式：>0.40% 設 SL、SL＝最高−0.20%、TP＝最高+0.10%…），碰到＝市價出場（taker），
#   v15.7（1111 2026-10-18）：現貨（L、U）的 TP 改成最高 +0.15%、2 秒移動 1 次（gd_rule，只換現貨的參數）；空單不變。
#   v15.8（1111 2026-10-18）：現貨 >0.50% 才設 SL（移動 SL 不變）、TP 1.5 秒移動 1 次。
#   現貨只賣那一層的數量（BOT 自己的分倉帳本）。出場後那一格空出來，照掛單規則等價格回來再掛。
# 價格：現貨層看現貨（SUI-USDT）逐筆成交價、空單看永續（SUI-USDT-SWAP）逐筆成交價；WS 斷線改成一次查全部（現貨 2 秒一次）。
# 金額：現貨每層買「每層U」、空單每層保證金「每層U」（槓桿只用在空單）。損益 %＝U ÷ 每層U（現貨＝1X）。
# 空單槓桿（v15.6，1111 2026-10-18：安全第一）：/runt 不打槓桿；每一層自己的強平價剛好＝原點×2.33（+133%，GD_TGT）＝gd_slev，
#   S01 0.77X、S11 1.29X、S20 2.11X、S30 4.17X…最高 20X（GD_LCAP，1111 統一；那個幣 OKX 最高更低就照 OKX）。
#   各層強平價都 ≥ +133% → 不管開了哪幾層（一路漲、震盪、暴漲跳格）合起來（rn_gliq＝各層強平價照數量加權平均）都 ≥ +133%。
#   +133%＝22 個幣歷史上漲一倍之後一天內再漲 95% 都在 16.3% 以內（2.0×1.163）→ 1111 在 +100% 手動換帳戶有一天反應時間（規劃十三）。
# 手續費（固定常數）：現貨 maker 0.08%、taker 0.10%；合約 maker 0.02%、taker 0.05%。
#   空單資金費照 OKX 費率模擬，算進每一單（出場通知多一行資金費），不另外發通知（💰 現在是現貨的符號）。
# 幣種數量（v15.3，1111 2026-10-18）：一個帳戶不限幾個幣種（原本最多 3 個＝1111 的預算，不寫進程式，要能測極限）；同一個幣種一組。
# 強平：空單同一個幣種合起來算（rn_gliq，逐倉公式），碰到＝全部空單強平（💥 一則）。現貨沒有強平。
# 兩套帳本：BOT 分倉（每一層自己的進場價、損益）＋官方均價（OKX：現貨平均成本、空單倉位均價）。
# 存檔：runtgrid_{帳戶}.json（v14 的 runtlayer 不再用；重開時看到舊的組 → 改名 .v14 收起來、不恢復、發通知）。
#   逐單紀錄照樣寫 runtlog（多 g＝1、kind、k、nm、dir、feeu），/summary 只算有 g 的。
GD_STEP    = Decimal("2")      # v20.0（1111 2026-10-24）：每一格 2%（v15～v19 3%）
GD_N       = 50                # v20.0：L01～L50、U01～U50、S01～S50、D01～D50（v18 33 層）
SP_FEE_IN  = Decimal("0.08")   # 現貨 maker（限價買進）0.08%
SP_FEE_OUT = Decimal("0.10")   # 現貨 taker（市價賣出）0.10%
GD_DIRS    = "LSUD"           # v16.0（1111 2026-10-19）：L、S、U、D；v18.0：D＝原點下面的合約做多 D01～D33
# v18.0（1111 2026-10-20 Excel＋畫面）：新策略
#   現貨 L01～L33＝原點 −1.5% 起每 3%（GD_SP_OFF）、U01～U33＝原點 +3% 起每 3%（v18.3，跟 S 同價）；L00＝原點；全部在下 /runt 的帳戶。
#   合約 S01～S33（做空，原點 +3%…+99%）、D01～D33（做多，原點 −3%…−99%）：每 11 層一個帳戶（GD_XACCT）：
#     o1111o S01～S11＋D01～D11、o2222o S12～S22＋D12～D22、o3333o S23～S33＋D23～D33（雙向持倉＋逐倉：空單、多單各自一個倉位）。
#   槓桿：每個帳戶第一層（S01、S12、S23、D01、D12、D23）1.5X（GD_ALEV），後面每一層拉到「跟第一層同一個強平價」的最大倍數
#     （往下取 0.01X、最高 20X／OKX 更低照 OKX）；算不出來（進場價已經在強平價外面，D30～D33）＝跟前一層一樣（1111：D29 以後都跟 D29 一樣）。
#   出場（v18.1，1111 2026-10-20）：現貨、合約都有 SL/TP（rn_rule）：最高毛利率 > 現貨 1.40%／合約 0.80% 第一次設，
#     SL＝距離現價 −0.20%、TP＝距離現價 +0.20%，只往獲利方向、各自每秒 1 次；碰到＝taker。（v18.0 現貨沒有 SL/TP、>1.20% 就賣，拿掉）
#   /runt：只有 o1111o 可以下 S、D（合約延伸到 o2222o、o3333o）；其他帳戶只能 L、U（現貨在自己的帳戶）。
# v18.2（1111 2026-10-21）：兩套一模一樣：o1111o＋o2222o、o3333o；o4444o＋o5555o、o6666o（將來幣安 6 個帳戶照樣兩套）
# v20.0（1111 2026-10-24 Excel）：格子每 2%：L01～L50 −1% 起、U01～U50 +1% 起、S01～S50 +2% 起、D01～D50 跟 L 同價；
#   每 10 層一個帳戶（現貨、合約一樣）o1111o～o5555o；每個帳戶第一層 1.5X、後面同一個強平價（D48～D50 算不出來＝D47）；
#   兩套取消，每一個 bot 都可以下 LSUD（單都放在這 5 個帳戶），同一個幣只能在一個 bot 下。SL/TP、掛單規則都不變。
# v20.0（1111 2026-10-24 Excel）：v18.2 的兩套取消 → 一套 5 個帳戶；每 10 層一個帳戶，現貨（L、U）、合約（S、D）都一樣：
#   o1111o 第 1～10 層＋L00、o2222o 11～20、o3333o 21～30、o4444o 31～40、o5555o 41～50。o6666o 不交易（查詢用）。
#   每一個 bot（o1111o～o6666o）都可以下 LSUD，下的單一律放在這 5 個帳戶（同一個幣只能在一個 bot 下：gd_other_bot）。
GD_XACCT   = ("o1111o", "o2222o", "o3333o", "o4444o", "o5555o")   # 第 1～10 層、11～20、21～30、31～40、41～50 放的帳戶
GD_LIQ_IND = " " * 9                               # v18.2 開始通知 強平 第 2、3 行前面的空白（對齊 🈴S 後面的帳戶）
GD_BLK     = 10                                    # v20.0：一個帳戶 10 層（v18 11 層）
GD_ALEV    = Decimal("1.5")                        # v18.0：每個帳戶第一層的槓桿
GD_SP_OFF  = Decimal("1")                          # v20.0：L 第 k 層＝原點 −(2k−1)%、U＝+(2k−1)%、D＝−(2k−1)%（跟 L 同價）；S＝+2k%
GD_LCAP    = Decimal("20")     # v15.6：合約每一層槓桿最高 20X（1111 統一）
# v17.0（1111 2026-10-19）：/runt 照 OKX 實盤規則模擬（/run 將來用同一套，差別只在成交、出場、損益查 OKX）：
#   數量照 OKX 單位往下取（現貨數量單位、合約張數；空單保證金照樣每層U，取整只少倉位）；下的時候檢查擋下來＋建議；
#   資金＝兩個帳戶真的可用 − 模擬佔用 ＋ 模擬已實現（每分鐘讀 OKX）；手續費讀帳戶費率、現貨買進從幣扣；
#   post-only 掛上去那一刻已經穿過＝OKX 取消；換格先撤單、確認撤掉再掛；SL/TP 改單照 OKX 條件單額度。
GD_AMEND_N   = 20               # v17.0：OKX 條件單：每 2 秒 20 次，「同一個帳戶＋同一個商品」各算各的 →
GD_AMEND_WIN = 2.0              #   v18.1：這個帳戶的現貨一份、每個帳戶的永續（那個帳戶的 S、D）一份；設定、移動一起算（保守）
GD_MIN_STEP  = Decimal("0.1")   # v17.0.2（1111 2026-10-20）：每層最少多少 U：0.1U 一級往上取（v17.0～v17.0.1 0.5U 一級，把真正的數字蓋掉了）
GD_MIN_CAP   = Decimal("1000")  #   1000U 還下不了＝這個幣這一邊開不了
GD_LOT0      = Decimal("0.00000001")   # 規格沒有數量單位時（測試用）
GD_SHORT_GAP = 1800             # v17.0：可用不夠的通知，同一組同一種掛單 30 分鐘內最多一次（錢在邊緣時一直進出，免得洗版；/status 隨時看得到資金）
GD_FEE_DEF   = {"sp": (SP_FEE_IN, SP_FEE_OUT), "sw": (RN_FEE_IN, RN_FEE_OUT)}   # 讀不到帳戶費率時（% 掛單、吃單）
FD = {}                         # v17.0：帳戶 → OKX 讀到的 {avail 可用、cash 餘額、eq 權益、frozen 佔用、lv 帳戶模式、pm 持倉模式、fee 費率、bad、t}
FD_GAP      = 60                # 每 60 秒讀一次帳戶可用、帳戶模式、持倉模式（巡邏）
FD_SPEC_GAP = 3600              # 每小時查一次 OKX 規格
FD_LV = {"1": "現貨模式", "2": "合約模式", "3": "跨幣種保證金", "4": "組合保證金"}
FD_PM = {"long_short_mode": "雙向持倉", "net_mode": "單向持倉"}
GD_FILE    = f"/srv/1111bot/data/runtgrid_{ACCT}.json"
FD_SIM_DIR = "/srv/1111bot/data"                       # v18.0：6 個 bot 共用的模擬佔用紀錄 fd_sim_{帳戶}.json（每個 bot 寫自己的、讀別人的）
FD_SIM_GAP = 5                                         # 每 5 秒寫一次自己的、讀一次別人的
FD_SIM_OLD = 60                                        # 別人的紀錄超過 60 秒沒更新（那個 bot 沒在跑）＝不算
FD_SIM = {"t": 0.0, "ext": {}}                         # 別人的（帳戶 → [佔用, 已實現]）
GD_MK      = {"sp": "💰", "sw": "🈴"}   # 1111：現貨 💰、合約 🈴
GD_ORG     = "🩵"                       # 原點
GD_IN      = "🟣"                       # 進場
GD_FULL    = "🈵"                       # 最🈵金額
GD_IND     = "     "                    # 進場通知第 2、3、5 行前面的空白（1111 畫面）
GD_CXL_IND = " " * 11                   # /stoprunt 撤單第 2 行起前面的空白（對齊第一行的層名）
GD = {}                                 # 幣種 → 一組網格
GD_SAVE = {"dirty": False, "t": 0.0}
GD_HOLD = []                            # 重開時沒恢復成功的（原樣留在存檔，背景每 10 秒再試）
RD = {}                                 # v19.0：/run 實盤網格：幣種 → 一組（跟 GD 同一種格式，多實盤欄位）
RD_HOLD = []                            # v19.0：/run 重開時沒恢復成功的
RG_FILE     = f"/srv/1111bot/data/rungrid_{ACCT}.json"       # v19.0：/run 存檔
RG_LOG_FILE = f"/srv/1111bot/data/rungridlog_{ACCT}.jsonl"   # v19.0：/run 每一張出場一行（/summary 實盤）


class GMode:
    """v19.0：/runt（模擬）、/run（實盤）各一份：指令名稱、進行中的組、存檔、逐單紀錄、畫面上多的字（實盤）。
    規則、畫面共用 gd_ 函式；一組用 G["real"] 分（gd_M）。"""
    def __init__(self, cmd, stop, D, hold, real, tag, file_var, logf):
        self.cmd, self.stop, self.D, self.hold, self.real, self.tag = cmd, stop, D, hold, real, tag
        self._file, self._logf = file_var, logf

    @property
    def file(self):
        return globals()[self._file]

    @property
    def logf(self):
        return self._logf()

GSIM  = GMode("runt", "stoprunt", GD, GD_HOLD, False, "", "GD_FILE", lambda: SIM.logf)
GREAL = GMode("run", "stop", RD, RD_HOLD, True, "實盤 ", "RG_FILE", lambda: RG_LOG_FILE)


def gd_M(G):
    """這一組是 /runt（GSIM）還是 /run 實盤（GREAL）。"""
    return GREAL if G.get("real") else GSIM
SPOT_SPEC = {}                          # 幣種 → 現貨規格 {iid, tick, lot, minsz}
SPOT_BULK = {"t": 0.0, "got": 0.0, "px": {}, "busy": False}
SPOT_REST_GAP = 2.0                     # WS 斷線時，現貨一次查全部最快 2 秒一次（6 個帳戶加永續，OKX 公開查詢每 2 秒 20 次以內）

def spot_iid(sym):
    return sym[:-4] + "-USDT"

async def okx_spot_specs_all():
    """一次查回全部 USDT 現貨的規格（1 次查詢）。成功回 True。"""
    r = await pub("/api/v5/public/instruments?instType=SPOT")
    if str(r.get("code")) != "0" or not r.get("data"):
        print("[okx] 現貨規格一次查全部失敗", r.get("code"), r.get("msg"))
        return False
    n = 0
    for d in r["data"]:
        iid = d.get("instId") or ""
        if not iid.endswith("-USDT") or (d.get("state") or "live") != "live":
            continue
        try:
            SPOT_SPEC[iid[:-5] + "USDT"] = {"iid": iid, "tick": Decimal(d["tickSz"]), "lot": Decimal(d["lotSz"]),
                                            "minsz": Decimal(d["minSz"])}
            n += 1
        except Exception:
            pass
    return n > 0

async def get_spot_spec(sym):
    if sym not in SPOT_SPEC:
        await okx_spot_specs_all()
    return SPOT_SPEC.get(sym)

async def spot_bulk_px(iid):
    """WS 斷線時的現貨現價：一次查回全部現貨（這個帳戶共用，最快 2 秒一次），查到的 4 秒內都用它。"""
    now = time.time()
    if now - SPOT_BULK["t"] >= SPOT_REST_GAP and not SPOT_BULK["busy"]:
        SPOT_BULK["t"], SPOT_BULK["busy"] = now, True
        try:
            r = await pub("/api/v5/market/tickers?instType=SPOT", 3.0)
            if str(r.get("code")) == "0" and r.get("data"):
                d = {}
                for x in r["data"]:
                    try:
                        if x.get("last"):
                            d[x["instId"]] = Decimal(x["last"])
                    except Exception:
                        pass
                if d:
                    SPOT_BULK["px"], SPOT_BULK["got"] = d, time.time()
        finally:
            SPOT_BULK["busy"] = False
    px = SPOT_BULK["px"].get(iid)
    return px if px is not None and time.time() - SPOT_BULK["got"] < 2 * SPOT_REST_GAP else None

async def gd_px(G, kd):
    """現價：WS 逐筆成交價優先；斷線一次查全部（現貨、永續各自）。"""
    iid = G[kd]["iid"]
    v = WS_PX.get(iid)
    if ws_fresh(v, time.time()):
        return v[0]
    return await (spot_bulk_px(iid) if kd == "sp" else rt_bulk_px(iid))

# ---------- 名稱、格子 ----------
def gd_kinds(G):
    """這一組有的種類：sp＝現貨（L、U）、sw＝永續（S 做空、D 做多）。"""
    return [kd for kd in ("sp", "sw") if G.get(kd)]

def gd_nm(kd, k):
    """格子的名稱：現貨 k＜0＝L01～、k＝0＝L00、k＞0＝U01～；合約 k＞0＝S01～（做空）、k＜0＝D01～（v18.0 做多）。"""
    if kd == "sw":
        return f"S{k:02d}" if k > 0 else f"D{-k:02d}"
    return f"L{-k:02d}" if k <= 0 else f"U{k:02d}"

def gd_nm_key(nm):
    """累計、/summary 的排列（1111 2026-10-17）：S 一組 → U 一組 → D 一組 → L 一組（v16.0，1111 2026-10-19），各自價格由高到低。"""
    n = int(nm[1:])
    return {"S": (0, -n), "U": (1, -n), "D": (2, n), "L": (3, n)}[nm[0]]

def gd_grp(p):
    """這一單是哪一種：sp＝現貨、S＝合約做空、D＝合約做多（v18.0）。"""
    if p["kind"] == "sp":
        return "sp"
    return "S" if p["k"] > 0 else "D"

def gd_blk(k):
    """第 k 層在第幾個帳戶（v20.0：0＝o1111o 第 1～10 層＋L00、1＝o2222o 11～20 … 4＝o5555o 41～50；L、U、S、D 都一樣）。"""
    return max(abs(k) - 1, 0) // GD_BLK

def gd_atag(kd, k):
    """那一層的帳戶標示 (o2222o)（v18.0 合約；v20.0 現貨也分帳戶，一樣寫）。"""
    return f"({gd_kacct(kd, k)})"


def gd_ab(a):
    """v18.0（1111 畫面）：帳戶縮寫 o1111o → o1o。"""
    return a[:2] + a[-1]


def gd_buy(kd, k):
    """v18.0：這一格是買（現貨、D 做多：價格往下碰到）還是賣（S 做空：價格往上碰到）。"""
    return kd == "sp" or k < 0


def gd_skd(s):
    """v18.0：掛單的位置 → 種類：sp＝現貨買、S＝做空、D＝做多（S、D 都是永續）。"""
    return "sp" if s == "sp" else "sw"

def gd_off(kd, k):
    """第 k 格離原點幾 %（v20.0，1111 2026-10-24 Excel）：L00＝0；現貨 L＝−(2k−1)%、U＝+(2k−1)%；合約 S＝+2k%、D＝−(2k−1)%（跟 L 同價）。"""
    if k == 0:
        return Decimal(0)
    if kd == "sp" or k < 0:
        n = GD_STEP * abs(k) - GD_SP_OFF
        return n if k > 0 else -n
    return GD_STEP * k


def gd_lpx(o, kd, k, tick):
    """第 k 格的價格＝原點 o×(1+離原點%)：買（現貨、D）往下取 tick、賣（S）往上取 tick。"""
    return align(o * (1 + gd_off(kd, k) / 100), tick, "L" if gd_buy(kd, k) else "S")

def gd_lp(G, kd, k):
    """第 k 格的價格（原點不動，算一次記起來）。"""
    o = G["org"]["px"]
    c = G.setdefault("lvc", {})
    if c.get("o") != o:
        c.clear(); c["o"] = o
    key = (kd, k)
    if key not in c:
        c[key] = gd_lpx(o, kd, k, G[kd]["tick"])
    return c[key]

def gd_fl(x, u):
    """v17.0：照 OKX 單位往下取（現貨數量單位、合約張數單位）。"""
    return (x / u).to_integral_value(rounding=ROUND_FLOOR) * u if u else x

def gd_fee(G, key):
    """v17.0：手續費率 %（掛單, 吃單）：key＝gd_fkey（v20.0：現貨 "sp:帳戶"、合約＝帳戶；/runt 開始時讀 OKX）。"""
    return (G.get("fee") or {}).get(key) or GD_FEE_DEF["sp" if key.startswith("sp") else "sw"]


def gd_fkey(kd, k):
    """v20.0：第 k 層的費率用哪一個：現貨＝那一層的帳戶的現貨（"sp:o2222o"）、合約＝那一層的帳戶的永續（"o2222o"）。"""
    return f"sp:{gd_kacct('sp', k)}" if kd == "sp" else gd_kacct("sw", k)


def gd_pfee(G, p):
    """這一單的手續費率：那一層的帳戶的現貨／永續。"""
    return gd_fee(G, gd_fkey(p["kind"], p["k"]))

def gd_kacct(kd, k):
    """這一格下在哪一個帳戶（v20.0：現貨、合約一樣，每 10 層一個帳戶：第 1～10 層＋L00 o1111o、11～20 o2222o … 41～50 o5555o）。"""
    return GD_XACCT[gd_blk(k)]

def gd_size(G, kd, k, lp, o=None, amt=None):
    """v17.0：這一格照 OKX 單位往下取的數量。現貨：q＝每層U÷價格（數量單位往下取），買得到（≥最小數量）而且扣完手續費還賣得掉才算 ok；
    合約：張數＝每層U×這一層的槓桿÷(價格×每張幾顆)（張數單位往下取），≥最小張數才 ok；q＝張數×每張幾顆。o＝原點（槓桿用）。"""
    amt = G["amt"] if amt is None else amt
    x = G.get(kd) or {}
    lot = x.get("lot") or GD_LOT0
    mn = x.get("minsz") or Decimal(0)
    if kd == "sp":
        q = gd_fl(amt / lp, lot)
        fi = gd_fee(G, gd_fkey("sp", k))[0]                # v20.0：那一層的帳戶的現貨費率
        return {"q": q, "ct": None, "lev": Decimal(1),
                "ok": q > 0 and q >= mn and gd_fl(q * (1 - fi / 100), lot) >= mn}
    ctv = x.get("ctval") or Decimal(1)
    lev = gd_lev(G, k, o)                                  # v18.0：S、D 每個帳戶第一層 1.5X，後面同一個強平價
    ct = gd_fl(amt * lev / (lp * ctv), lot)
    return {"q": ct * ctv, "ct": ct, "lev": lev, "ok": ct > 0 and ct >= mn}

def gd_lsize(G, kd, k, lp, o=None):
    """v17.0.3（1111 2026-10-20）：這一格實際下的數量和金額。每層U 下得了＝照每層U（gd_size）；
    下不了（例：U32、U33 價格翻倍，1U 買不到 1 顆／扣完手續費賣不掉）＝自動用這一層的最低金額（OKX 最小數量、最小張數）：
      現貨＝最少要買幾顆才「買得到、扣完手續費還賣得掉」，金額＝顆數×價格；
      合約＝最小張數，保證金＝倉位÷這一層的槓桿（槓桿不變 → 強平價不變）。
    回傳 gd_size 的欄位＋amt（這一層的金額：現貨＝每層U 或最低金額、空單＝保證金）、up（True＝自動調高）。"""
    z = gd_size(G, kd, k, lp, o)
    if z["ok"]:
        z.update(amt=G["amt"], up=False)
        return z
    x = G.get(kd) or {}
    lot = x.get("lot") or GD_LOT0
    mn = x.get("minsz") or lot
    if kd == "sp":
        fi = gd_fee(G, gd_fkey("sp", k))[0] / 100
        q = (mn / (1 - fi) / lot).to_integral_value(rounding=ROUND_CEILING) * lot
        while q < mn or gd_fl(q * (1 - fi), lot) < mn:
            q += lot
        return {"q": q, "ct": None, "lev": Decimal(1), "ok": True, "amt": q * lp, "up": True}
    ctv = x.get("ctval") or Decimal(1)
    ct = (mn / lot).to_integral_value(rounding=ROUND_CEILING) * lot
    q = ct * ctv
    return {"q": q, "ct": ct, "lev": z["lev"], "ok": True, "amt": q * lp / z["lev"], "up": True}

def gd_need(G, kd, k, lp):
    """v17.0：掛這一張要佔用多少 U：現貨＝數量×價格；空單＝保證金（每層U；v17.0.3：不夠的層＝最低金額）。"""
    z = gd_lsize(G, kd, k, lp)
    return z["q"] * lp if kd == "sp" else z["amt"]

def gd_pamt(G, p):
    """v17.0.3：這一單的金額（損益 %、強平、佔用用）：自動調高的層＝那一層的最低金額，其他＝每層U。
    v18.4：每層U 可以中途改 → 用這一單進場時的每層U（p["lu"]），舊的單照原本的金額。"""
    return p.get("amt") or gd_lu(G, p)


def gd_lu(G, p):
    """v18.4：這一單進場時的每層U（中途改每層U 之前進場的單照原本的；沒記的＝現在的每層U）。"""
    return p.get("lu") or G["amt"]

def gd_qs(v, lot, extra=0):
    """數量的寫法：照數量單位的小數位數（extra＝多幾位，零頭、手續費用），往下取。"""
    e = (lot or GD_LOT0).normalize().as_tuple().exponent - extra
    e = max(min(e, 0), -8)
    return str(Decimal(v).quantize(Decimal(1).scaleb(e), rounding=ROUND_DOWN))

def gd_rng(G, s):
    """可以掛的格子（v18.0 照掛單位置）：sp＝有 L＝L33～L00、有 U＝U01～U33；S＝S01～S33；D＝D33～D01。"""
    if s == "S":
        return range(1, GD_N + 1) if G["has"]["S"] else []
    if s == "D":
        return range(-GD_N, 0) if G["has"]["D"] else []
    return range(-GD_N if G["has"]["L"] else 1, (GD_N if G["has"]["U"] else 0) + 1)

def gd_okd(G):
    """原點用哪一種價格：有現貨（L、U）＝現貨，只有 S＝永續。"""
    return "sp" if G.get("sp") else "sw"

def gd_head(G):
    """幣種 方向 每層U（1111：埋伏通知、/status、/summary、/stoprunt 的標題；v15.6 拿掉槓桿）。"""
    return f"{G['sym']} {G['dir']} {gd_prm(G)}"

def gd_alev(G, p):
    """v18.5.2（1111 2026-10-22）：合約這一單的實際槓桿＝倉位（數量×進場價）÷保證金（每層U；自動用最低金額的層＝那一層的金額）。
    數量照 OKX 單位往下取 → 實際槓桿 ≤ 設計槓桿（p["lev"]，例 WLD 1U S01：2張 1.10U ÷ 1U＝1.1X，設計 1.5X）。"""
    return p["qty"] * p["ent"] / gd_pamt(G, p)

def gd_alev_s(G, p):
    """v18.5.2：實際槓桿的寫法（四捨五入到 0.01X，去尾零：1.1X、2.37X）。"""
    return pct(gd_alev(G, p).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def gd_prm(G, p=None):
    """每層U；p＝那一單 → 用那一單自己的每層U（v18.4），合約前面加那一單的實際槓桿（v18.5.2，例 S01 1.1X 1U、S07 2.37X 5U）。"""
    if p is not None and p["kind"] == "sw":
        return f"{gd_alev_s(G, p)}X {pct(gd_lu(G, p))}U"
    return f"{pct(gd_lu(G, p) if p is not None else G['amt'])}U"

def gd_bprm(G, p):
    """v17.0.7 出場通知「本單」那一行：合約＝槓桿X 金額U、現貨＝金額U；自動用最低金額的層寫那一層實際的金額（損益 % 的分母）。"""
    a = pct(gd_lu(G, p)) if not (p.get("up") or p.get("part")) else f"{gd_pamt(G, p).quantize(Decimal('0.01'), rounding=ROUND_CEILING)}"
    return (f"{gd_alev_s(G, p)}X " if p["kind"] == "sw" else "") + f"{a}U"   # v18.5.2：實際槓桿

def gd_cap(G):
    """合約槓桿上限：20X；那個幣 OKX 最高更低照 OKX。"""
    cap = GD_LCAP
    sw = G.get("sw")
    if sw and sw.get("maxlev"):
        cap = min(cap, Decimal(sw["maxlev"]))
    return cap


def gd_k0(k):
    """第 k 層那個帳戶的第一層（v20.0：S01、S11、S21、S31、S41；D01、D11…D41）。"""
    k0 = gd_blk(k) * GD_BLK + 1
    return k0 if k > 0 else -k0


def gd_tliq(G, k, o=None):
    """v18.0：第 k 層那個帳戶的目標強平價＝那個帳戶第一層 1.5X 自己的強平價（OKX 逐倉、模擬手續費 進 0.02%、出 0.05%）：
    做空 P＝E0×(1＋A×(1−進))÷(A×(1＋維持保證金率＋出))；做多 P＝E0×(A×(1＋進)−1)÷(A×(1−維持保證金率−出))。"""
    o = G["org"]["px"] if o is None else o
    k0 = gd_k0(k)
    e0 = gd_lpx(o, "sw", k0, G["sw"]["tick"])
    a = min(GD_ALEV, gd_cap(G))
    mmr = G.get("mmr") or RN_MMR_DEF
    fi, fo = RN_FEE_IN / 100, RN_FEE_OUT / 100
    if k > 0:
        return e0 * (1 + a * (1 - fi)) / (a * (1 + mmr + fo))
    return e0 * (a * (1 + fi) - 1) / (a * (1 - mmr - fo))


def gd_lev(G, k, o=None):
    """v18.0 合約第 k 層的槓桿：那個帳戶第一層 1.5X；其他層＝自己的強平價剛好在 gd_tliq（第一層的強平價）的最大倍數：
    做空 L＝1÷(P×(1＋維持保證金率＋出)÷E − (1−進))；做多 L＝1÷(1＋進 − P×(1−維持保證金率−出)÷E)；
    往下取 0.01X（強平價只會更遠）、最高 gd_cap；算不出來（進場價已經在強平價外面，例 D30～D33）＝跟前一層一樣（1111 2026-10-20）。"""
    o = G["org"]["px"] if o is None else o
    mmr = G.get("mmr") or RN_MMR_DEF
    c = G.setdefault("levc", {})
    key = (o, k, mmr)
    if key in c:
        return c[key]
    cap = gd_cap(G)
    if k == gd_k0(k):
        lev = min(GD_ALEV, cap)
    else:
        P = gd_tliq(G, k, o)
        e = gd_lpx(o, "sw", k, G["sw"]["tick"])
        fi, fo = RN_FEE_IN / 100, RN_FEE_OUT / 100
        x = P * (1 + mmr + fo) / e - (1 - fi) if k > 0 else 1 + fi - P * (1 - mmr - fo) / e
        if x <= 0:
            lev = gd_lev(G, k - 1 if k > 0 else k + 1, o)
        else:
            lev = max(min((1 / x).quantize(Decimal("0.01"), rounding=ROUND_DOWN), cap), Decimal("0.01"))
    c[key] = lev
    return lev

def gd_md(t):
    return datetime.fromtimestamp(t, TZ8).strftime("%m/%d %H:%M")

def gd_hms(t):
    return datetime.fromtimestamp(t, TZ8).strftime("%H:%M:%S")

def gd_q(G, kd, px):
    return rt_q(px, G[kd]["tick"])

# ---------- 一組網格 ----------
def gd_new(p, sp, sw, chat, now, mmr=None):
    G = {"sym": p["sym"], "dir": p["dir"], "has": {x: x in p["dir"] for x in GD_DIRS}, "amt": p["amt"],
         "off": p["off"], "chat": chat, "t_run": now, "mmr": mmr, "sp": sp, "sw": sw,
         "org": None, "o00": None, "pos": [], "ord": {"sp": None, "S": None, "D": None}, "agg": {}, "px": {"sp": None, "sw": None},
         "ev": asyncio.Event(), "done": False, "task": None, "nxt": 0.0, "glc": None,
         "fee": None, "note": [], "short": {}, "n_po": 0, "n_amd": 0, "amd": {}}   # v17.0
    return G

def gd_pos(G, kd, k, ent, t, px_hit, t_ord):
    """一格成交 → 一個持倉（各自獨立：自己的進場價、SL/TP、最高／最低毛利率；欄位跟 rn_pos 一樣，rn_rule 直接用）。
    v17.0：數量照 OKX 單位往下取（gd_size）；現貨買進的手續費從買到的幣扣 → got＝實得顆數（賣出時照它往下取）；合約 ct＝張數。
    v18.0：side＝L（現貨、D 做多）／S（S 做空）。"""
    side = "L" if gd_buy(kd, k) else "S"
    g0 = rn_g(side, ent, px_hit)
    z = gd_lsize(G, kd, k, ent)                            # v17.0.3：每層U 不夠的層自動用最低金額
    p = {"kind": kd, "side": side, "k": k, "ent": ent, "t_in": t, "t_ord": t_ord, "lev": z["lev"], "qty": z["q"], "ct": z["ct"],
         "sl": None, "tp": None, "sl_g": None, "tp_g": None, "pend": None,
         "t_arm": None, "arm_px": None, "n_mv": 0, "n_tp": 0, "hi": None, "tp_t": 0.0, "sl_t": 0.0, "fu": Decimal(0), "fn": 0,
         "mfe": max(g0, Decimal(0)), "mae": min(g0, Decimal(0)), "told": False, "out": None}
    p["lu"] = G["amt"]                                     # v18.4：這一單進場時的每層U（之後改每層U 不影響這一單）
    if z["up"]:
        p["amt"], p["up"] = z["amt"], True                 # v17.0.3：這一單的金額＝最低金額
    if kd == "sp":
        p["got"] = z["q"] * (1 - gd_fee(G, gd_fkey("sp", k))[0] / 100)
    return p

def gd_held(G, kd=None):
    """還抱著的單（kd＝只要那一種），價格由高到低（同價 S 在上）。"""
    ps = [p for p in G["pos"] if not p["out"] and (kd is None or p["kind"] == kd)]
    return sorted(ps, key=lambda p: (-p["ent"], p["kind"] != "sw"))

def gd_liq(G, grp, b):
    """合約一個倉位（v18.0：grp＝S 做空／D 做多 × b＝第幾個帳戶）合起來的預估強平價（rn_gliq 逐倉公式；持倉、資金費沒變就不重算）。"""
    ps = [p for p in G["pos"] if p["kind"] == "sw" and not p["out"] and gd_grp(p) == grp and gd_blk(p["k"]) == b]
    if not ps:
        return None
    if G.get("real"):                                      # v19.0：實盤＝OKX 持倉的預估強平價（那個帳戶那一邊一個倉位）
        x = (G.get("okpos") or {}).get(rg_pkey(GD_XACCT[b], "S" if grp == "S" else "L")) or {}
        return x.get("liq") or None
    sig = tuple((p["k"], p["t_in"], p.get("fu")) for p in ps)
    if not isinstance(G.get("glc"), dict):
        G["glc"] = {}
    c = G["glc"].get((grp, b))
    if not c or c[0] != sig:
        T = {"amt": G["amt"], "xx": 0, "mmr": G.get("mmr"), "tick": G["sw"]["tick"]}   # 槓桿用每一單自己的（p["lev"]）
        c = G["glc"][(grp, b)] = (sig, rn_gliq(T, "S" if grp == "S" else "L", [dict(p, amt=gd_pamt(G, p)) for p in ps]))   # v18.4：每一單自己的保證金
    return c[1]

def gd_xpx(px, lvl):
    """出場成交價＝碰到的那一筆成交價（市價出場，跟 rn_touch 一樣）。lvl＝碰到的 SL/TP 價（測試用）。"""
    return px

def gd_cross(kd, o, px):
    """這一筆價格有沒有碰到（穿過）掛單價：買（現貨、D 做多）＝價格 ≤ 掛單價；賣（S 做空）＝價格 ≥ 掛單價。"""
    return px <= o["px"] if gd_buy(kd, o.get("k", 0)) else px >= o["px"]

def gd_live(kd, o, px, t):
    """v17.0 post-only：掛單送到 OKX（live）那一刻，市場已經穿過掛單價＝會吃單 → OKX 取消。
    live 之前的每一筆記下那時候有沒有穿過（o["x"]）；live 之後第一筆：live 那一刻穿過了＝'cxl'，沒有＝掛上去了 'ok'。還沒到 live＝'wait'。"""
    if o.get("chk"):
        return "ok"
    if o.get("live") is None:
        return "wait"
    if t <= o["live"]:
        o["x"] = gd_cross(kd, o, px)
        return "wait"
    if o.get("x"):
        return "cxl"
    o["chk"] = True
    return "ok"

def gd_touch(G, kd, px, t):
    """一筆價格（kd 那一種的 WS 逐筆成交價，或 WS 斷線時的查價）：掛單成交、合約強平、合約 SL/TP 出場（跟 rn_touch 同一套判斷）。
    v17.0：post-only（gd_live）；換格：舊的那一張撤單確認（cxl）之前碰到＝成交，確認之後換上新的那一張（nxt）。
    v18.0：掛單三個位置（sp 現貨買、S 做空、D 做多）；強平＝每個帳戶的 S、D 各自一個倉位。v18.1：現貨也有 SL/TP。"""
    if G["done"]:
        return
    if G.get("real"):                                      # v19.0：/run 實盤：成交、出場、強平都看 OKX（rg_sync），這裡只記現價、最高／最低毛利率
        rg_touch(G, kd, px, t)
        return
    G["px"][kd] = px
    o = G.get("o00") if kd == "sp" else None
    if o:
        st = gd_live("sp", o, px, t)
        if st == "cxl":                                    # L00 被取消 → 價格回到 L00 上面再重掛（gd_hang）
            o.update(live=None, chk=False); o.pop("x", None)
            G["n_po"] = G.get("n_po", 0) + 1
        elif st == "ok" and px <= o["px"]:                 # 💰L00 成交＝🩵原點
            G["o00"] = None
            G["org"] = {"px": o["px"], "t": t}
            G["pos"].append(gd_pos(G, "sp", 0, o["px"], t, px, o["t"]))
            G["ev"].set()
    for s in (("sp",) if kd == "sp" else ("S", "D")):
        o = G["ord"].get(s)
        if o and o.get("cxl") is not None and t >= o["cxl"]:   # 撤單確認了（途中沒碰到）→ 換上準備好的那一張
            G["ord"][s] = o = o.get("nxt")
        n = o.get("nxt") if o else None
        if n and t <= n["live"]:
            n["x"] = gd_cross(kd, n, px)                   # 新的那一張還沒送到：記下這時候有沒有穿過
        if o:
            st = gd_live(kd, o, px, t)
            if st == "cxl":                                # post-only 被取消 → 下一拍重算
                G["ord"][s] = None
                G["n_po"] = G.get("n_po", 0) + 1
            elif st == "ok" and gd_cross(kd, o, px):
                G["ord"][s] = None                         # 成交（撤單途中碰到也算成交，準備好的那一張不掛）
                if not any(p["kind"] == kd and p["k"] == o["k"] and not p["out"] for p in G["pos"]):
                    G["pos"].append(gd_pos(G, kd, o["k"], o["px"], t, px, o["t"]))
                G["ev"].set()
    if kd == "sw":
        for grp in ("S", "D"):                             # v18.0：每個帳戶的 S（做空）、D（做多）各自一個倉位
            for b in range(len(GD_XACCT)):
                liq = gd_liq(G, grp, b)
                if liq is not None and (px >= liq if grp == "S" else px <= liq):   # 合倉強平：出場價＝預估強平價
                    for p in G["pos"]:
                        if p["kind"] == "sw" and not p["out"] and gd_grp(p) == grp and gd_blk(p["k"]) == b:
                            p["out"] = (t, liq, "強平")
                    G["ev"].set()
    for p in G["pos"]:
        if p["kind"] != kd or p["out"]:
            continue
        gg = rn_g(p["side"], p["ent"], px)
        if gg > p["mfe"]:
            p["mfe"] = gg
        if gg < p["mae"]:
            p["mae"] = gg
        if p["pend"] and t >= p["pend"][4]:                # SL/TP 過了送單時間才生效
            p["sl"], p["tp"], p["sl_g"], p["tp_g"] = p["pend"][:4]; p["pend"] = None
        if p["sl"] is None:
            continue
        isL = p["side"] == "L"
        if p["tp"] is not None and ((isL and px >= p["tp"]) or (not isL and px <= p["tp"])):
            p["out"] = (t, gd_xpx(px, p["tp"]), "TP")
        elif (isL and px <= p["sl"]) or (not isL and px >= p["sl"]):
            p["out"] = (t, gd_xpx(px, p["sl"]), "SL(移動)" if p["n_mv"] else "SL")
        if p["out"]:
            G["ev"].set()


def gd_on_tick(iid, px):
    """WS 逐筆成交價（由 _ws_push_px 呼叫）：現貨的價給現貨層、永續的價給合約（S、D）。"""
    t = time.time()
    for G in list(GD.values()) + list(RD.values()):        # v19.0：模擬、實盤都要
        for kd in gd_kinds(G):
            if G[kd]["iid"] == iid:
                gd_touch(G, kd, px, t)

def gd_hang(G, now):
    """掛單（每 0.25 秒）：v18.0 一組最多 3 張：現價下面最近一格沒有持倉的 💰現貨買（L/U）、🈴做多（D）；現價上面最近一格沒有持倉的 🈴做空（S）；
    不一樣就換格。v17.0（照實盤）：換格＝舊的先撤單（cxl＝送單時間後確認），確認撤掉才換上新的（nxt，再一個送單時間才掛上去）；
    可用不夠（fd_avail）＝那一張不掛（跟實盤被拒一樣），通知一次、有錢了自動接著掛再通知；帳戶模式被改（fd_halt）＝不掛新單。
    L00 被 post-only 取消 → 價格回到 L00 上面再重掛。"""
    o = G.get("o00")
    if o and o.get("live") is None:
        px = G["px"].get("sp")
        if px is not None and px > o["px"] and not fd_halt(gd_kacct("sp", 0)):
            o.update(live=now + RT_LAT, chk=False); o.pop("x", None)
    elif o and fd_halt(gd_kacct("sp", 0)):              # v20.0：L00 在第一個帳戶（o1111o）                              # 帳戶模式被改（實盤要先撤掉所有掛單才改得了）→ L00 撤掉，改回來再掛
        o.update(live=None, chk=False); o.pop("x", None)
    if G["org"] is None:
        return
    if G.get("stop"):                                      # v18.5：停止中：掛單撤掉（確認了就清掉），不再掛新單
        for s, cur in G["ord"].items():
            if cur and cur.get("cxl") is None:
                cur["cxl"], cur["nxt"] = now + RT_LAT, None
            elif cur and now >= cur["cxl"]:
                G["ord"][s] = None
                gd_save_soon()
        return
    for s in ("sp", "S", "D"):
        kd = gd_skd(s)
        if not G.get(kd):
            continue
        px = G["px"].get(kd)
        cur = G["ord"].get(s)
        if cur and cur.get("cxl") is not None and now >= cur["cxl"]:
            G["ord"][s] = cur = cur.get("nxt")             # 撤單確認了 → 換上準備好的那一張
            gd_save_soon()
        if px is None or (cur and cur.get("cxl") is not None):
            continue                                       # 撤單中：等 OKX 確認
        if cur is not None and fd_halt(gd_kacct(kd, cur["k"])):
            cur["cxl"], cur["nxt"] = now + RT_LAT, None    # 帳戶模式被改 → 那個帳戶的掛單撤掉
            gd_save_soon()
            continue
        held = {p["k"] for p in G["pos"] if p["kind"] == kd and not p["out"]}
        buy = s != "S"
        best = None
        for k in gd_rng(G, s):
            if k in held:
                continue
            lp = gd_lp(G, kd, k)
            if buy and lp < px and (best is None or k > best[0]):
                best = (k, lp)
            elif not buy and lp > px and (best is None or k < best[0]):
                best = (k, lp)
        if best is not None and cur is not None and cur["k"] == best[0]:
            continue
        if best is not None:
            acct = gd_kacct(kd, best[0])
            if fd_halt(acct):
                best = None
            else:
                need = gd_need(G, kd, best[0], best[1])
                back = gd_need(G, kd, cur["k"], cur["px"]) if cur is not None and gd_kacct(kd, cur["k"]) == acct else Decimal(0)
                av = fd_avail(acct, back)
                if av is not None and av < need:
                    gd_short(G, s, kd, best[0], acct, av, need)
                    best = None
        if best is None:
            if cur is not None:
                cur["cxl"], cur["nxt"] = now + RT_LAT, None
                gd_save_soon()
            continue
        gd_short_ok(G, s, kd, best[0], best[1], acct, av if av is None else av - need)
        new = {"k": best[0], "px": best[1], "t": now, "live": now + RT_LAT}
        if cur is not None:
            cur["cxl"] = now + RT_LAT
            new["live"] = cur["cxl"] + RT_LAT
            cur["nxt"] = new
        else:
            G["ord"][s] = new
        gd_save_soon()

def gd_short(G, s, kd, k, acct, av, need):
    """v17.0：可用不夠，這一張沒有掛 → 通知一次（同一個掛單位置一直不夠只通知一次；30 分鐘內通知過就不再通知）。
    v18.0：s＝掛單位置（sp、S、D）；可用、佔用、已實現＝6 個 bot 合起來（fd_sim）。"""
    if s in G["short"]:
        return
    now = time.time()
    told = now - G.setdefault("short_t", {}).get(s, -1e18) >= GD_SHORT_GAP
    G["short"][s] = told
    if not told:
        return
    G["short_t"][s] = now
    f = FD.get(acct) or {}
    used, real = fd_used_all(acct), fd_real_all(acct)
    det = f"(真的{f.get('avail', Decimal(0)):.2f}U−模擬佔用{used:.2f}U" + (f"{'+' if real >= 0 else '−'}已實現{abs(real):.2f}U" if real else "") + ")"
    if G.get("real"):
        det = "(OKX)"                                      # v19.0：實盤＝OKX 真的可用
    G["note"].append("\n".join([f"{E.WARN} 可用不夠 {gd_M(G).tag}{G['sym']} {gd_nm(kd, k)} 沒有掛{gd_atag(kd, k)}",
                                 f"{acct} 可用 {av:.2f}U{det}", f"這一張要 {need:.2f}U",
                                 "有錢了會自動接著掛,到時再通知", f"時間:{hhmmss()}"]))

def gd_short_ok(G, s, kd, k, lp, acct, after):
    """v17.0：之前可用不夠（有通知過）的那一個掛單位置，這次掛上了 → 通知。"""
    if not G["short"].pop(s, False):
        return
    G["note"].append("\n".join([f"✅ 可用夠了 {gd_M(G).tag}{G['sym']} {gd_nm(kd, k)} 已掛 {gd_q(G, kd, lp)}{gd_atag(kd, k)}"]
                                + ([f"{acct} 可用 {after:.2f}U"] if after is not None else []) + [f"時間:{hhmmss()}"]))

def gd_fund_tick(G, now):
    """合約資金費：查費率（背景，每 60 秒）、到結算時間記到那一刻還抱著的合約（跟 rn_fund_settle 一樣的算法，不發通知）。
    v18.0：D 做多：費率正＝付、負＝收（跟空單相反）。"""
    sw = G.get("sw")
    if not sw:
        return
    f = sw["fund"]
    if not f["busy"] and (now >= f["q"] or (f["t"] and 0 < f["t"] - now <= RN_FUND_PRE and now - f["qt"] > RN_FUND_PRE)):
        f["busy"] = True; f["q"] = now + RN_FUND_Q
        rt_bg(rn_fund_query(sw))
    if f["t"] is None or f["rate"] is None or now < f["t"]:
        return
    px = G["px"].get("sw")
    t, rate = f["t"], f["rate"]
    f["last"], f["t"], f["q"] = t, None, 0.0              # 馬上再查下一期
    n = 0
    for p in G["pos"]:
        if p["kind"] != "sw" or p["t_in"] >= t or (p["out"] and p["out"][0] <= t) or px is None:
            continue
        p["fu"] = p.get("fu", Decimal(0)) + (1 if p["side"] == "S" else -1) * p["qty"] * px * rate   # 費率正：空單收、多單付
        p["fn"] = p.get("fn", 0) + 1
        n += 1
    if n:
        print(f"[grid] 資金費結算 {G['sym']} 費率 {rate} 合約 {n} 單")
        gd_save_soon()

# ---------- 損益 ----------
def gd_money(G, p, px):
    """以 px 出場（市價）：毛損益U、手續費U、資金費U、淨損益U 與 %（÷ 每層U；% 、U 先照畫面取整再相加，跟 rn_money 一樣）。
    v17.0（照 OKX）：費率＝帳戶費率（gd_fee）。現貨：買進手續費從幣扣（買進數量×費率，用進場價換成 U），
      賣出＝實得顆數往下取到數量單位（零頭留在帳戶，價值算在毛損益裡），賣出手續費＝賣出金額×吃單費率；
      合約：開倉手續費＝數量×進場價×掛單費率、平倉＝數量×出場價×吃單費率；強平＝損失全部保證金（每層U）。
    v18.0：D 做多＝數量×(出場價−進場價)；費率＝那一層的帳戶的永續費率。"""
    amt = gd_pamt(G, p)                                    # v17.0.3：自動調高的層用那一層的金額
    fi, fo = gd_pfee(G, p)
    extra = {}
    if p.get("real"):                                      # v19.0：實盤＝OKX 成交價、OKX 實際手續費、OKX 實際資金費
        if p["kind"] == "sp":
            gross = p["qty"] * (px - p["ent"])             # 跟模擬一樣：零頭的價值算在毛損益裡（零頭留在帳戶）
            sold = p.get("sold")
            sold = rg_D(sold) if sold is not None else gd_fl(p.get("got") or p["qty"], (G.get("sp") or {}).get("lot") or GD_LOT0)
            got = p.get("got") if p.get("got") is not None else p["qty"]
            extra = {"sell": sold, "dust": got - sold}
        else:
            gross = p["qty"] * ((p["ent"] - px) if p["side"] == "S" else (px - p["ent"]))
        fee = (p.get("fee_in") or Decimal(0)) + (p.get("fee_out") or Decimal(0))
    elif p["kind"] == "sp":
        got = p.get("got")
        got = p["qty"] * (1 - fi / 100) if got is None else got
        sell = gd_fl(got, (G.get("sp") or {}).get("lot") or GD_LOT0)
        gross = p["qty"] * (px - p["ent"])
        fee = p["qty"] * fi / 100 * p["ent"] + sell * px * fo / 100
        extra = {"sell": sell, "dust": got - sell}
    elif p.get("out") and p["out"][2] == "強平":
        gross = -amt
        fee = p["qty"] * p["ent"] * fi / 100
    else:
        gross = p["qty"] * ((p["ent"] - px) if p["side"] == "S" else (px - p["ent"]))
        fee = p["qty"] * p["ent"] * fi / 100 + p["qty"] * px * fo / 100
    fu = p.get("fu", Decimal(0))
    g, fp, fd = rn_q2(gross / amt * 100), rn_q2(fee / amt * 100), rn_q2(fu / amt * 100)
    gu, feeu, fuq = rn_q4(gross), rn_q4(fee), rn_q4(fu)
    return {"g": g, "fee": fp, "net": g - fp + fd, "gu": gu, "feeu": feeu, "netu": gu - feeu + fuq, "fu": fuq, "fund": fd,
            "fn": p.get("fn", 0), **extra}

def gd_own(G, p, px):
    """這一單自己的損益 %（現貨＝價格漲跌%；合約＝槓桿×價格漲跌%），照畫面取到 0.01%。
    v18.5.2（1111 2026-10-22）：合約用實際槓桿（gd_alev）＝跟出場的毛損益 % 同一種算法（數量×價差÷保證金）。"""
    lev = gd_alev(G, p) if p["kind"] == "sw" else p["lev"]
    return rn_q2(lev * rn_g(p["side"], p["ent"], px))

def gd_cum(G, nm):
    """這一格從 /runt 開始到現在出場單的淨利率%（每一單 % 相加）。"""
    a = G["agg"].get(nm)
    return a["net"] if a else Decimal(0)

def gd_kind_net(G, grp):
    """這一種（sp＝L、U；S；D）全部出場單的淨利率%。"""
    want = {"sp": "LU", "S": "S", "D": "D", "sw": "SD"}[grp]
    return sum((a["net"] for nm, a in G["agg"].items() if nm[0] in want), Decimal(0))

def gd_all_net(G):
    return sum((a["net"] for a in G["agg"].values()), Decimal(0))

def gd_pxs(G, pxs=None):
    """每一種的現價：pxs 有給的先用（例：進場價、出場價），沒有就用最後一筆。"""
    out = {}
    for kd in gd_kinds(G):
        v = (pxs or {}).get(kd)
        out[kd] = v if v is not None else (G["px"].get(kd) or (WS_PX.get(G[kd]["iid"]) or (None,))[0])
    return out

def gd_hold_line(G, ps, pxs, cum=None):
    """持倉損益 每一單 % 相加|(出場單的淨利率%)|N單🎯。"""
    ones = [gd_own(G, p, pxs[p["kind"]]) for p in ps if pxs.get(p["kind"]) is not None]
    mid = f"|{rn_sp(cum)}" if cum is not None else ""
    return f"持倉損益 {rn_sp(sum(ones, Decimal(0)))}{mid}|{len(ps)}單🎯"

def gd_upl(p, px):
    """這一單的未實現損益 U（不含手續費，OKX 的算法）：現貨、D 做多＝數量×(現價−進場價)；S 做空＝數量×(進場價−現價)。"""
    return p["qty"] * ((px - p["ent"]) if p["side"] == "L" else (p["ent"] - px))

def gd_hold_u_line(ps, pxs):
    """v15.4（1111 2026-10-18）/status：持倉損益 -0.0002U|1單🎯＝還抱著的單未實現損益 U 合計（不含手續費）。"""
    u = sum((gd_upl(p, pxs[p["kind"]]) for p in ps if pxs.get(p["kind"]) is not None), Decimal(0))
    return f"持倉損益 {rn_su(rn_q4(u))}|{len(ps)}單🎯"

def gd_hold_pu_line(G, ps, pxs):
    """v18.0.1（1111 2026-10-20 畫面）/status：持倉損益 -1.87%|-0.0560U|1單🎯＝還抱著的每一單 % 相加（跟出場通知的持倉損益一樣）|未實現 U 合計（不含手續費）。"""
    ok = [p for p in ps if pxs.get(p["kind"]) is not None]
    pc = sum((gd_own(G, p, pxs[p["kind"]]) for p in ok), Decimal(0))
    u = sum((gd_upl(p, pxs[p["kind"]]) for p in ok), Decimal(0))
    return f"持倉損益 {rn_sp(pc)}|{rn_su(rn_q4(u))}|{len(ps)}單🎯"

def gd_avg_line(G, kd, ps, px, full=True, pc=True, grp=None, b=None):
    """官方均價（OKX 的算法）：現貨＝平均成本（總成本÷總數量）、未實現＝數量×現價−成本、% ＝÷成本；
    合約＝倉位均價（數量加權）、未實現＝S 數量×(均價−現價)、D 數量×(現價−均價)，% ＝÷保證金合計。full＝/status（多 N單、後面 💰／🈴空單／🈴多單）。
    pc＝False（v15.4 /status）：不寫 %。v18.0：grp＝S／D、b＝第幾個帳戶（每個帳戶的空單、多單各一個倉位）。"""
    ps = [p for p in ps if p["kind"] == kd and (grp is None or gd_grp(p) == grp) and (b is None or gd_blk(p["k"]) == b)]
    if not ps or px is None:
        return []
    if G.get("real"):                                      # v19.0：實盤＝OKX 的數字（合約：OKX 倉位均價、未實現；現貨：OKX 平均成本、未實現）
        x = rg_avg_okx(G, kd, grp, b)
        if x:
            mk = GD_MK[kd] + (("D" if ps[0]["side"] == "L" else "S") if kd == "sw" else "")   # v20.0.2
            return [f"官方均價 {gd_q(G, kd, x[0])}|" + (f"{len(ps)}單|" if full else "") + rn_su(rn_q4(x[1])) + (mk if full else "")]
    Q = sum((p["qty"] for p in ps), Decimal(0))
    cost = sum((p["qty"] * p["ent"] for p in ps), Decimal(0))
    avg = cost / Q
    upl = sum((gd_upl(p, px) for p in ps), Decimal(0))
    base = cost if kd == "sp" else sum((gd_pamt(G, p) for p in ps), Decimal(0))
    s = (f"官方均價 {gd_q(G, kd, avg)}|" + (f"{len(ps)}單|" if full else "") + (f"{rn_sp(rn_q2(upl / base * 100))}|" if pc else "")
         + rn_su(rn_q4(upl)))
    mk = GD_MK[kd] + (("D" if ps[0]["side"] == "L" else "S") if kd == "sw" else "")   # v20.0.2（1111 2026-10-26 畫面）：合約 多單＝🈴D、空單＝🈴S（v18.5.3：🈴、🈴多單）
    return [s + (mk if full else "")]

def gd_rows(G, ps, pxs, cum=True, liq=False):
    """持倉列：L11 時:分:秒|這一單的%|這一格的淨利率%（cum＝False：只到這一單的%）；兩欄各自對齊。
    v18.4（1111 2026-10-21 畫面）liq＝/status：S 做空後面多 |🔴61.07%＝距離強平（現價再漲幾 %）。
    v18.4.1（1111 2026-10-21）：每個帳戶 11 單在第一層的強平價一起強平 → 只寫在那個帳戶抱著的第一層（S01、S12、S23；
      第一層出場了＝那個帳戶抱著的最低一層）；D 做多不寫（1111：不太可能被強平）。"""
    a = rn_pad([rn_sp(gd_own(G, p, pxs[p["kind"]])) if pxs.get(p["kind"]) is not None else "查價中" for p in ps])
    b = rn_pad([rn_sp(gd_cum(G, gd_nm(p["kind"], p["k"]))) for p in ps])
    first = {}
    for p in ps:
        if p["kind"] == "sw" and p["side"] == "S":
            first[gd_blk(p["k"])] = min(first.get(gd_blk(p["k"]), p["k"]), p["k"])
    d = [gd_liq_dist(G, p, pxs.get("sw")) if liq and p["kind"] == "sw" and p["side"] == "S" and first.get(gd_blk(p["k"])) == p["k"] else None
         for p in ps]
    c = iter(rn_pad([x for x in d if x is not None]))
    d = [next(c) if x is not None else None for x in d]
    return [f"{gd_nm(p['kind'], p['k'])} {gd_hms(p['t_in'])}|{x}" + (f"|{y}" if cum else "") + (f"|🔴{z}" if z else "")
            for p, x, y, z in zip(ps, a, b, d)]


def gd_liq_dist(G, p, px):
    """v18.4：這一單那個倉位（帳戶×S/D）的預估強平價離現價幾 %（S＝要再漲、D＝要再跌），62.61%。"""
    lq = gd_liq(G, gd_grp(p), gd_blk(p["k"]))
    if lq is None or not px:
        return None
    v = (lq / px - 1) * 100 if p["side"] == "S" else (1 - lq / px) * 100
    return f"{max(v, Decimal(0)):.2f}%"

def gd_agg_add(G, nm, m):
    a = G["agg"].setdefault(nm, {"n": 0, "net": Decimal(0), "gu": Decimal(0), "feeu": Decimal(0), "fu": Decimal(0),
                                 "u": Decimal(0), "fn": 0, "gp": Decimal(0), "fp": Decimal(0), "fdp": Decimal(0)})
    a["n"] += 1
    a["net"] += m["net"]; a["gu"] += m["gu"]; a["feeu"] += m["feeu"]; a["fu"] += m["fu"]; a["u"] += m["netu"]; a["fn"] += m["fn"]
    a["gp"] = a.get("gp", Decimal(0)) + m["g"]; a["fp"] = a.get("fp", Decimal(0)) + m["fee"]   # v18.4：每一單的 毛損益%、手續費%、資金費% 相加
    a["fdp"] = a.get("fdp", Decimal(0)) + m["fund"]


def gd_agg_pc(G, amt):
    """v18.4：沒有記 毛損益%、手續費%、資金費% 的格子（v18.4 以前的）＝用那時候的每層U 補上。"""
    for a in G["agg"].values():
        if "gp" not in a:
            a["gp"], a["fp"], a["fdp"] = rn_q2(a["gu"] / amt * 100), rn_q2(a["feeu"] / amt * 100), rn_q2(a["fu"] / amt * 100)

def gd_cells(names, cnt, pc):
    """每一格一行：S11 3單+2.70%（單數＋淨利率%，不空格；整格對齊）。"""
    cs = rn_pad([f"{cnt[k]}單{rn_sp(pc[k])}" for k in names])
    return [f"{k} {c}" for k, c in zip(names, cs)]

def gd_log_add(G, p, m):
    """一張出場記一行到 runtlog（/summary、數據分析；g＝1＝網格的紀錄）。"""
    try:
        t_in, (t_out, px, why) = p["t_in"], p["out"]
        r = {"d": rn_today(t_out), "g": 1, "sym": G["sym"], "dir": G["dir"], "kind": p["kind"], "side": p["side"], "k": p["k"],
             "lvl": p["k"], "nm": gd_nm(p["kind"], p["k"]), "lev": str(rn_q2(gd_alev(G, p))) if p["kind"] == "sw" else str(p["lev"]), "acct": gd_kacct(p["kind"], p["k"]), "amt": str(gd_pamt(G, p)), "up": 1 if p.get("up") else 0,
             "org": round(float(G["org"]["t"]), 3) if G.get("org") else None, "t_run": round(G["t_run"], 3),
             "ent": str(p["ent"]), "px": str(px), "t_in": round(t_in, 3), "t_out": round(t_out, 3), "hold": round(t_out - t_in, 1),
             "why": why, "n_mv": p["n_mv"], "n_tp": p.get("n_tp", 0), "mfe": str(rn_q2(p["mfe"])), "mae": str(rn_q2(p["mae"])),
             "gp": str(m["g"]), "np": str(m["net"]), "fp": str(m["fund"]), "gu": str(m["gu"]), "feeu": str(m["feeu"]),
             "u": str(m["netu"]), "fu": str(m["fu"]), "fn": m["fn"]}
        if p.get("real"):                                  # v19.0：實盤多記 OKX 單號、張數、觸發價
            r.update(real=1, oid=p.get("oid"), xoid=p.get("xoid"), aid=p.get("aid_x"), sz=str(p.get("sz")), trig=str(p.get("trig")),
                     fee_in=str(p.get("fee_in")), fee_out=str(p.get("fee_out")))
        with open(gd_M(G).logf, "a") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[grid] log fail", e)

def gd_done(G, p, m):
    gd_agg_add(G, gd_nm(p["kind"], p["k"]), m)
    gd_log_add(G, p, m)

# ---------- 畫面 ----------
def gd_start_lines(G, mpx, now, info=None):
    """① 埋伏通知（/runt 開始，1111 2026-10-17 畫面；方向寫在標題，只列有開的那幾邊）。
    v17.0（1111 2026-10-19 畫面）：級距下面接 info（gd_check 的 ✅OKX檢查、取整後最少、資金）。
    v18.0（1111 2026-10-20 畫面）：💰L/U 1.5%起每3%；🈴S、D 每個帳戶一行（槓桿 第一層～最高、帳戶縮寫）；強平(每個帳戶11單一起)。"""
    L = [f"✅ /{gd_M(G).cmd} {gd_head(G)} {rt_p2(G['off'])}%", "", f"現價 {mpx} | {gd_hms(now)}"]
    if G.get("o00"):
        L.append(f"L00 {gd_q(G, 'sp', G['o00']['px'])}|掛單中{RN_ORD_MK}")
    else:
        L.append(f"{GD_ORG}原點 {gd_q(G, gd_okd(G), G['org']['px'])}")
    L += ["", "以原點為軸心計算級距"]
    for x in "LU":
        if G["has"][x]:
            L.append(f"{GD_MK['sp']}{x}01～{x}{GD_N:02d}({pct(GD_SP_OFF)}%起每{pct(GD_STEP)}%)")   # v20.0：L、U 都 1%起每2%
    if G["has"]["L"] or G["has"]["U"]:                     # v20.0（1111 2026-10-24 畫面）
        nb = len(GD_XACCT)
        L.append(f"{GD_MK['sp']}每{GD_BLK}層一個帳戶({gd_ab(GD_XACCT[0])} 01～{GD_BLK:02d}…{gd_ab(GD_XACCT[-1])} {(nb - 1) * GD_BLK + 1:02d}～{nb * GD_BLK:02d})")
    o = (G.get("org") or G.get("o00"))["px"]               # 槓桿照原點算（有 L＝L00 的價格，成交＝原點）
    for x, nm in (("S", "做空"), ("D", "做多")):
        if G["has"][x]:
            for b, a in enumerate(GD_XACCT):
                ks = gd_blk_ks(x, b)
                lv = [gd_lev(G, k, o) for k in ks]
                L.append(f"{GD_MK['sw']}{x}{abs(ks[0]):02d}～{x}{abs(ks[-1]):02d} {nm} {pct(lv[0])}X～{max(lv):.2f}X({gd_ab(a)})")
    if G["has"]["S"] or G["has"]["D"]:
        L += ["", f"強平(每個帳戶{GD_BLK}單一起)"]
        for x in "SD":
            if G["has"][x]:
                L += [(f"{GD_MK['sw']}{x} " if b == 0 else GD_LIQ_IND) + f"{a} {gd_liq_pc(G, gd_blk_ks(x, b)[0], o)}"   # v18.2（1111 畫面）：一個帳戶一行
                      for b, a in enumerate(GD_XACCT)]
    return L + list(info or []) + ["", f"時間:{hhmmss()}"]


def gd_blk_ks(x, b):
    """v18.0：S／D 第 b 個帳戶的 11 層（k 的清單，從第一層開始）。"""
    sg = 1 if x == "S" else -1
    return [sg * (b * GD_BLK + i) for i in range(1, GD_BLK + 1)]


def gd_liq_pc(G, k, o):
    """v18.0：第 k 層那個帳戶的強平價（＝第一層 1.5X 的強平價，11 單一起）離原點 %，+69.9%／-67.3%。"""
    v = ((gd_tliq(G, k, o) / o - 1) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{'+' if v >= 0 else '-'}{abs(v)}%"

def gd_org_txt(G, with_time=True):
    o = G.get("org")
    if not o:
        return f"{GD_ORG}原點 等L00成交"
    tm = f"{gd_hms(o['t'])}|" if with_time else ""
    return f"{GD_ORG}{'L00' if G['has']['L'] else '原點'} {tm}{gd_q(G, gd_okd(G), o['px'])}"

def gd_fill_lines(G, p):
    """② 進場（1111 2026-10-17 畫面）：這一種還抱著的每一單，用這一張的成交價算。
    v18.0：合約＝那個帳戶那一邊（S 或 D）的倉位；標題後面寫帳戶 (o2222o)。"""
    kd = p["kind"]
    px = p["ent"]
    grp = gd_grp(p)
    L = [f"{GD_IN}進場 {gd_M(G).tag}{G['sym']} {gd_nm(kd, p['k'])} {gd_prm(G, p)}{GD_MK[kd]}{gd_atag(kd, p['k'])}",
         f"{GD_IND}{rn_mdt(p['t_in'])}|{gd_q(G, kd, px)}",
         f"{GD_IND}埋伏 {rn_hold(p['t_in'] - (p.get('t_ord') or p['t_in']))}"] + gd_fill_qty(G, p) + [""]   # v17.0：數量那幾行
    o = G.get("org")
    if o:
        L += [f"{GD_ORG}{rn_mdt(o['t'])}|{gd_q(G, gd_okd(G), o['px'])}", f"{GD_IND}歷時{rn_hold(p['t_in'] - o['t'])}", ""]
    b = gd_blk(p["k"])                                     # v20.0：現貨也分帳戶 → 列那個帳戶的（官方均價＝那個帳戶的平均成本）
    ps = [q for q in gd_held(G, kd) if gd_blk(q["k"]) == b and (kd == "sp" or gd_grp(q) == grp)]
    pxs = {kd: px}
    L += gd_rows(G, ps, pxs)
    L += ["", gd_hold_line(G, ps, pxs, gd_kind_net(G, grp))] + gd_avg_line(G, kd, ps, px, full=False, grp=grp if kd == "sw" else None, b=b)
    return L + ["", f"時間:{hhmmss()}"]

def gd_fill_qty(G, p):
    """v17.0（1111 2026-10-19 畫面）：進場通知多的那幾行（照 OKX 單位往下取之後的數量）：
    現貨＝買幾顆|花多少U、手續費幾顆|實得幾顆（手續費從買到的幣扣）；合約＝幾張(幾顆)|倉位U|保證金U（保證金照樣每層U）。"""
    x = G.get(p["kind"]) or {}
    lot = x.get("lot") or GD_LOT0
    if p["kind"] == "sp":
        got = p.get("got")
        got = p["qty"] if got is None else got
        up = [f"{GD_IND}(每層{pct(gd_lu(G, p))}U 不夠,自動用最低金額)"] if p.get("up") else []   # v17.0.3
        up += [f"{GD_IND}(部分成交,金額照成交的比例)"] if p.get("part") else []                    # v19.0：實盤部分成交
        return [f"{GD_IND}買{gd_qs(p['qty'], lot)}顆|{rn_q4(p['qty'] * p['ent'])}U",
                f"{GD_IND}手續費{gd_qs(p['qty'] - got, lot, 2)}顆",
                f"{GD_IND}實得{gd_qs(got, lot, 2)}顆"] + up        # v18.5.3（1111 2026-10-22）：實得換行
    ct = p.get("ct")
    ct = p["qty"] / (x.get("ctval") or 1) if ct is None else ct
    mg = pct(gd_lu(G, p)) if not (p.get("up") or p.get("part")) else f"{gd_pamt(G, p).quantize(Decimal('0.01'), rounding=ROUND_CEILING)}"
    return ([f"{GD_IND}{pct(ct)}張({pct(p['qty'])}{G['sym'][:-4]})|倉位{(p['qty'] * p['ent']).quantize(Decimal('0.01'))}U|保證金{mg}U"]
            + ([f"{GD_IND}(每層{pct(gd_lu(G, p))}U 不夠,自動用最低金額)"] if p.get("up") else [])
            + ([f"{GD_IND}(部分成交,保證金照成交的比例)"] if p.get("part") else []))

def gd_acc_liq(G, ks, pxs):
    """v18.5.1（1111 2026-10-22 畫面）：出場通知的累計 S01🟰 3單+2.85%🔴82.20%：
    每個帳戶在累計裡最下面那一格 S（通常是 S01、S12、S23）後面寫 🔴＝那個帳戶現在抱著的空單合併強平價離現價幾 %（跟 /status 一樣）；
    那個帳戶現在沒有抱空單＝不寫。"""
    if not G.get("sw"):
        return {}
    px = gd_pxs(G, pxs).get("sw")
    low = {}
    for nm in ks:
        if nm[0] == "S":
            k = int(nm[1:])
            low[gd_blk(k)] = min(low.get(gd_blk(k), k), k)
    d = {gd_nm("sw", k): gd_liq_dist(G, {"kind": "sw", "k": k, "side": "S"}, px) for k in low.values()}
    d = {nm: v for nm, v in d.items() if v is not None}
    return dict(zip(d, rn_pad(list(d.values()))))

def gd_acc_lines(G, pxs, liq=False, status=False):
    """出場通知的累計（從 /runt 開始）：每一格（S → U → L，各自價格由高到低）→ 毛損益／手續費／(資金費)／淨損益 → 持倉損益。
    毛損益%、手續費%＝合計 U ÷ 每層U；淨損益%＝每一格的淨利率%相加（跟 /status 一樣）。
    v18.5.1：liq＝出場通知：每個帳戶最下面那一格 S 後面寫 🔴 距離強平（gd_acc_liq）。"""
    A = G["agg"]
    ks = sorted(A, key=gd_nm_key)
    n = sum(A[k]["n"] for k in ks)
    gu = sum((A[k]["gu"] for k in ks), Decimal(0))
    fe = sum((A[k]["feeu"] for k in ks), Decimal(0))
    fu = sum((A[k]["fu"] for k in ks), Decimal(0))
    u = sum((A[k]["u"] for k in ks), Decimal(0))
    amt = G["amt"]
    if G.get("mix"):                                       # v18.4：中途改過每層U → % ＝每一單的 % 相加（每一單照自己的金額）
        P = lambda k: sum((A[x].get(k, Decimal(0)) for x in ks), Decimal(0))
        pg, pf, pd = P("gp"), P("fp"), P("fdp")
    else:
        pg, pf, pd = rn_q2(gu / amt * 100), rn_q2(fe / amt * 100), rn_q2(fu / amt * 100)
    rows = ([("毛損益", pg, gu), ("手續費", -pf, -fe)]
            + ([("資金費", pd, fu)] if rn_q4(fu) != 0 else []) + [("淨損益", gd_all_net(G), u)])
    x = rn_pad([rn_sp(r[1]) for r in rows])
    y = rn_pad([rn_su(r[2]) for r in rows])
    cs = rn_pad([f"{A[k]['n']}單{rn_sp(A[k]['net'])}" for k in ks])
    rd = gd_acc_liq(G, ks, pxs) if liq else {}
    cs = [c + (f"🔴{rd[k]}" if k in rd else "") for k, c in zip(ks, cs)]
    if status:                                             # v20.0.3（1111 2026-10-27 畫面）/status：每一格 → 最後一行 累計🟰19單(… 開始)；拿掉 損益 那一行
        L = [f"{k}🟰{c}" for k, c in zip(ks, cs)] + [f"累計🟰{n}單({gd_md(G['t_run'])} 開始)"]
    else:
        L = ([f"累計 {n}單({gd_md(G['t_run'])} 開始)", f"損益 {rn_sp(gd_all_net(G))}"]   # v17.0.6（1111 2026-10-20 畫面）：損益＝每一格的 % 加起來
             + [f"{k}🟰{c}" for k, c in zip(ks, cs)])                                      #   每一格 U01🟰1單+0.38%
    L += [""] + [f"{r[0]} {a}|{b}" for r, a, b in zip(rows, x, y)]
    L[-1] += E.pnl_emoji(rn_q4(u))
    return L + [gd_hold_line(G, gd_held(G), gd_pxs(G, pxs))]

def gd_exit_lines(G, p, m):
    """③ 出場（1111 2026-10-17 畫面）：一單一則。"""
    kd = p["kind"]
    t, px, why = p["out"]
    names = ["毛損益", "手續費"] + (["資金費"] if m["fn"] else []) + ["淨損益"]
    pc = rn_pad([rn_sp(m["g"]), rn_sp(-m["fee"])] + ([rn_sp(m["fund"])] if m["fn"] else []) + [rn_sp(m["net"])])
    us = rn_pad([rn_su(m["gu"]), rn_su(-m["feeu"])] + ([rn_su(m["fu"])] if m["fn"] else []) + [rn_su(m["netu"])])
    hi, lo = rn_pad([rn_sp(p["mfe"]), rn_sp(p["mae"])])
    L = [f"{E.pnl_emoji(m['netu'])}出場 {gd_M(G).tag}{G['sym']} {gd_nm(kd, p['k'])} {gd_prm(G, p)}{GD_MK[kd]}{gd_atag(kd, p['k'])}", "",
         f"進場 {rn_mdt(p['t_in'])}|{gd_q(G, kd, p['ent'])}"]
    if p["t_arm"]:
        L.append(f"設SL {rn_mdt(p['t_arm'])}|{gd_q(G, kd, p['arm_px'])}")
    L.append(f"出場 {rn_mdt(t)}|{gd_q(G, kd, px)}")
    if kd == "sp" and m.get("sell") is not None:          # v17.0：現貨賣出的顆數（實得往下取到數量單位），零頭留在帳戶
        lot = (G.get("sp") or {}).get("lot") or GD_LOT0
        ds = gd_qs(m["dust"], lot, 2)
        L.append(f"賣出 {gd_qs(m['sell'], lot)}顆")
        if Decimal(ds) > 0:
            L.append(f"零頭 {ds}顆📌")                     # v17.0.6（1111 2026-10-20 畫面）：零頭另一行
    L += [f"持倉 {rn_hold(t - p['t_in'])}", f"原因 {rn_why_nm(why, p.get('n_tp', 0))}"]
    L.append(f"移動 SL {p['n_mv']}次 | TP {p.get('n_tp', 0)}次")   # v18.1：現貨也有 SL/TP
    L += [f"最高毛利率 {hi}", f"最低毛利率 {lo}", "", f"本單{GD_MK[kd]}   {gd_nm(kd, p['k'])} {gd_bprm(G, p)}"]   # v17.0.7（1111 2026-10-20）
    L += [f"{n} {x}|{y}" + (f"|結算{m['fn']}次" if n == "資金費" else "") for n, x, y in zip(names, pc, us)]
    L[-1] += E.pnl_emoji(rn_q4(m["netu"]))
    return L + [""] + gd_acc_lines(G, {kd: px}, liq=True) + ["", f"時間:{hhmmss()}"]   # v18.5.1：累計 S01 後面 🔴

def gd_liq_lines(G, items):
    """💥 合約合倉強平（v18.0：一個帳戶的 S 或 D 一則）：這次強平的每一單一行（各損失自己的保證金）→ 這次合計 → 累計。
    標題 💥強平 SUIUSDT S01～S11 5U🈴(o1111o)（1111 畫面）。"""
    items = sorted(items, key=lambda x: -x[0]["k"])       # 價格由高到低（S11…S01、D01…D11）
    t = max(p["out"][0] for p, _ in items)
    px = items[0][0]["out"][1]
    nm = [gd_nm("sw", p["k"]) for p, _ in items]
    k0 = items[0][0]["k"]
    x = "S" if k0 > 0 else "D"
    ns = sorted(abs(p["k"]) for p, _ in items)
    rng = f"{x}{ns[0]:02d}" + (f"～{x}{ns[-1]:02d}" if len(ns) > 1 else "")
    L = [f"💥強平 {gd_M(G).tag}{G['sym']} {rng} {pct(gd_lu(G, items[0][0]))}U{GD_MK['sw']}{gd_atag('sw', k0)}", "",
         f"強平 {rn_mdt(t)}|{gd_q(G, 'sw', px)}", ""]
    a = rn_pad([rn_sp(m["net"]) for _, m in items])
    b = rn_pad([rn_su(m["netu"]) for _, m in items])
    L += [f"{n}|{x}|{y}" for n, x, y in zip(nm, a, b)]
    gu = sum((m["gu"] for _, m in items), Decimal(0))
    fe = sum((m["feeu"] for _, m in items), Decimal(0))
    u = sum((m["netu"] for _, m in items), Decimal(0))
    us = rn_pad([rn_su(gu), rn_su(-fe), rn_su(u)])
    L += ["", f"這次強平 {len(items)}單", f"毛損益 {us[0]}", f"手續費 {us[1]}", f"淨損益 {us[2]}{E.pnl_emoji(rn_q4(u))}"]
    return L + [""] + gd_acc_lines(G, {"sw": px}) + ["", f"時間:{hhmmss()}"]

GD_S01_MK = "🔴"                                           # v18.5.5（1111 2026-10-22）：/status 掛單中的 S01（S12、S23）後面
GD_L00_MK = "🩵"                                           # v18.5.4（1111 2026-10-22）：/status L00（原點）那一行後面；v20.0.3（1111 2026-10-27）⏺️ 改 🩵


def gd_ord_mk(kd, k):
    """/status 掛單列後面的 emoji：L00＝⏺️（v18.5.4）；每個帳戶空單的第一層（S01、S12、S23：強平的關鍵點）＝🔴（v18.5.5，1111 2026-10-22）；其他＝🔹。"""
    if kd == "sp" and k == 0:
        return GD_L00_MK
    if kd == "sw" and k > 0 and k == gd_k0(k):
        return GD_S01_MK
    return RN_ORD_MK


def gd_status_lines(G, now):
    """④ /status 一個幣種一段（1111 2026-10-17 畫面）：▶️開始 → 🩵原點 → 價格由高到低（S、U、L 混在一起，同價 S 在上；
    v18.0.1（1111 2026-10-20 畫面）：持倉列下面多 累計 那一段（跟出場通知一樣：從 /runt 開始一直累計，有出場單才有）；
    持倉損益 改成 每一單 % 相加|未實現U|N單🎯。
    掛單列 時:分:秒|掛單價|掛單中🔹；現價插在它的價格位置）→ 持倉損益 → 官方均價 💰／🈴。
    v15.4（1111 2026-10-18 畫面）：標題 📣持倉／📣埋伏（拿掉「通知」）；持倉損益 改成 U：還抱著的單未實現損益合計（不含手續費，
    跟官方均價的 U 同一種算法）|N單🎯（出場單的淨利率% 拿掉）；官方均價 拿掉 %：均價|N單|未實現U💰。"""
    ps = gd_held(G)
    pxs = gd_pxs(G)
    L = [f"📣{'持倉' if ps else '埋伏'} {gd_M(G).tag}{gd_head(G)}", f"▶️{gd_md(G['t_run'])}起|{rn_hold(now - G['t_run'])}",   # v16.1（1111）：開始→起
         ] + (["🚦停止中(不再掛單,持倉跑到出場為止)"] if G.get("stop") else []) + ([] if G["has"]["L"] else [gd_org_txt(G)]) + [""]
    # v18.0：拿掉 🈴D層在 o2222o 執行；v18.5 停止中；v20.0.3（1111 2026-10-27 畫面）：有 L 拿掉 🩵原點 那一行（L00 那一列後面 🩵 就是原點）；沒有 L 沒有 L00 那一列，留著
    rows = [(p["ent"], 0 if p["kind"] == "sw" else 1, "pos", p) for p in ps]
    for sl, o in G["ord"].items():
        if o:
            kd = gd_skd(sl)
            rows.append((o["px"], 0 if kd == "sw" else 1, "ord", (kd, o["k"], o)))
    if G.get("o00"):
        rows.append((G["o00"]["px"], 1, "ord", ("sp", 0, G["o00"])))
    org = G.get("org")
    if org and G["has"]["L"] and not any(r[3]["kind"] == "sp" and r[3]["k"] == 0 for r in rows if r[2] == "pos") \
            and not any(r[3][0] == "sp" and r[3][1] == 0 for r in rows if r[2] == "ord"):
        rows.append((org["px"], 1, "org", None))           # v18.5.4（1111 2026-10-22）：L00 沒持倉、沒掛單 → 照原點的價格位置也列出來
    rows.sort(key=lambda r: (-r[0], r[1]))
    prow = iter(gd_rows(G, [r[3] for r in rows if r[2] == "pos"], pxs, liq=True))   # v18.4：合約後面寫距離強平
    mpx = pxs.get(gd_okd(G))
    cur = f"現價 {gd_hms(now)}|{gd_q(G, gd_okd(G), mpx)}🛜" if mpx is not None else "現價 查價中"
    done = mpx is None
    for r in rows:
        if not done and r[0] < mpx:
            L.append(cur); done = True
        if r[2] == "pos":                                  # v18.5.4（1111 2026-10-22）：L00＝原點那一格，後面 ⏺️（持倉、掛單中、沒持倉沒掛單都是）
            L.append(next(prow) + (GD_L00_MK if r[3]["kind"] == "sp" and r[3]["k"] == 0 else ""))
        elif r[2] == "org":
            L.append(f"L00 {gd_hms(org['t'])}|{gd_q(G, 'sp', org['px'])}|未掛單{GD_L00_MK}")   # v18.5.6（1111 2026-10-23）：沒持倉的列一定寫狀態（掛單中／未掛單）
        else:
            kd, k, o = r[3]
            L.append(f"{gd_nm(kd, k)} {gd_hms(o['t'])}|{gd_q(G, kd, o['px'])}|掛單中{gd_ord_mk(kd, k)}")
    if not done or mpx is None:
        L.append(cur)
    if G.get("org"):
        acc = gd_acc_lines(G, pxs, status=True)[:-1] + [""] if G["agg"] else []   # v18.0.1：累計（這一輪一直累計）；v20.0.3：每一格在上、累計🟰N單 在下
        L += [""] + acc + [gd_hold_pu_line(G, ps, pxs)]
        secs = [(ACCT, [])]                                # v18.0（1111 畫面）：每個帳戶 💰、🈴空單、🈴多單各一行；別的帳戶空一行、(o2222o) 下面
        for b, a in enumerate(GD_XACCT):                   # v20.0：現貨也分 5 個帳戶
            ls = gd_avg_line(G, "sp", ps, pxs.get("sp"), pc=False, b=b) if G.get("sp") else []
            if G.get("sw"):
                ls += [x for g in ("S", "D") for x in gd_avg_line(G, "sw", ps, pxs.get("sw"), pc=False, grp=g, b=b)]
            if a == ACCT:
                secs[0] = (a, ls)
            else:
                secs.append((a, ls))
        L += secs[0][1]
        for a, ls in secs[1:]:
            if ls:
                L += ["", f"({a})"] + ls
    return L

def gd_stop_lines(G, now, stopping=False, cx=None):
    """⑥ /stoprunt（1111 2026-10-17 畫面）：撤掉的掛單 → 還抱著的單 → 累計出場、持倉損益、淨損益。
    v15.5（1111 2026-10-18：各層 % 相加沒有意義，跟 /status 一樣）：持倉損益 -6.4770U|11單🎯（未實現 U，不含手續費）、淨損益 +0.3906U（只寫 U）；每一層的 % 不變。
    v18.5（1111 2026-10-22）：stopping＝有持倉 → 🚦 停止中：不再掛新單，持倉照規則（SL/TP 照常移動）跑到出場為止，全部出場後策略結束。"""
    sc = gd_M(G).stop
    L = [f"🚦 /{sc} {gd_M(G).tag}{gd_head(G)} 停止中" if stopping else f"🛑 /{sc} {gd_M(G).tag}{gd_head(G)} 已停止",
         f"▶️{gd_md(G['t_run'])} 開始|歷時{rn_hold(now - G['t_run'])}"]
    cx = gd_cx_list(G) if cx is None else cx
    if cx:
        L += [f"撤單 {cx[0][2]} {gd_q(G, cx[0][3], cx[0][0])}"] + [f"{GD_CXL_IND}{nm} {gd_q(G, kd, px)}" for px, _, nm, kd in cx[1:]]
    else:
        L.append("撤單 無")
    if stopping:
        L.append("不再掛新單")
    ps = gd_held(G)
    pxs = gd_pxs(G)
    L += ["", (f"持倉{len(ps)}單(照規則跑到出場為止):" if stopping else f"持倉{len(ps)}單(手動平倉):") if ps else "持倉0單"] + gd_rows(G, ps, pxs, cum=False)
    for a in GD_XACCT if not stopping else ():             # v18.0：別的帳戶的持倉（手動平倉要去那個帳戶；v20.0 現貨也是）
        q = [p for p in ps if a != ACCT and gd_kacct(p["kind"], p["k"]) == a]
        if q:
            x1 = lambda p: gd_nm(p["kind"], p["k"])[0]
            txt = "、".join(gd_rng_txt(x, [p["k"] for p in q if x1(p) == x]) for x in "LUSD" if any(x1(p) == x for p in q))
            L.append(f"({txt} 持倉在 {a})")
    A = G["agg"]
    n = sum(a["n"] for a in A.values())
    u = sum((a["u"] for a in A.values()), Decimal(0))
    L += ["", f"累計出場 {n}單", gd_hold_u_line(ps, pxs), f"淨損益 {rn_su(u)}"]
    if stopping:
        L += ["", "全部出場後策略結束,會再通知"]
    return L + ["", f"時間:{hhmmss()}"]


def gd_cx_list(G):
    """/stoprunt、/stop 撤單那幾行：還在掛的單（價格由高到低）。"""
    cx = [(o["px"], 0 if gd_skd(sl) == "sw" else 1, gd_nm(gd_skd(sl), o["k"]), gd_skd(sl)) for sl, o in G["ord"].items() if o]
    if G.get("o00"):
        cx.append((G["o00"]["px"], 1, "L00", "sp"))
    cx.sort(key=lambda r: (-r[0], r[1]))
    return cx


def gd_stop_done_lines(G, now):
    """v18.5（1111 2026-10-22 畫面）：停止中的組，最後一單出場 → 策略結束。"""
    A = G["agg"]
    n = sum(a["n"] for a in A.values())
    u = sum((a["u"] for a in A.values()), Decimal(0))
    return [f"🛑 /{gd_M(G).stop} {gd_M(G).tag}{gd_head(G)} 已停止", f"▶️{gd_md(G['t_run'])} 開始|歷時{rn_hold(now - G['t_run'])}", "持倉全部出場",
            "", f"累計出場 {n}單", f"淨損益 {rn_su(u)}", "", f"時間:{hhmmss()}"]


def gd_stop_dup_lines(G):
    """v18.5（1111 2026-10-22）：停止中的幣又打 /stoprunt → 已經停止策略中,請勿重複。"""
    return [f"{E.WARN} {G['sym']} 已經停止策略中,請勿重複", f"持倉 {len(gd_held(G))}單 跑到出場為止,全部出場後會通知"]


def gd_stop_busy_lines(G):
    """v18.5（1111 2026-10-22 畫面）：停止中的幣打 /runt（開新的、改每層U）→ 擋。"""
    return [f"{E.WARN} {G['sym']} 停止中(還有持倉 {len(gd_held(G))}單)", f"全部出場後才能重新 /{gd_M(G).cmd}"]

def gd_status_line(G):
    """進行中的一行（/runt、/stoprunt 沒帶參數時列出來）。"""
    n = len(gd_held(G))
    st = "等L00成交" if G.get("org") is None else (f"持倉 {n}單" if n else "埋伏中")
    return f"💡{gd_head(G)} 埋伏{rt_p2(G['off'])}%|{st}" + ("|🚦停止中" if G.get("stop") else "")   # v18.5

def gd_running(M=None):
    M = M or GSIM
    return ["進行中:"] + ([gd_status_line(G) for G in sorted(M.D.values(), key=lambda G: G["sym"])] or [f"目前沒有進行中的 {M.cmd}"])

# ---------- /summary ----------
GD_SUM_SEP = "━" * 14                                      # v20.0.1（1111 2026-10-26 畫面）：/summary 分幣種的分隔線拉長
GD_SUM_MK  = "💛"                                          # v20.0.1（1111 2026-10-26 畫面）：/summary 分幣種 每個幣種名稱前面

def gd_recs(d, M=None):
    """某一天（台灣時間）網格的逐單出場紀錄（v14 以前的紀錄沒有 g，不算）。v19.0：M＝GSIM（/runt）、GREAL（/run 實盤）。"""
    return [r for r in rn_log_read(d, M or GSIM) if r.get("g")]

def gd_rD(r, k):
    try:
        return Decimal(str(r.get(k) or "0"))
    except Exception:
        return Decimal(0)

def gd_rfee(r):
    return gd_rD(r, "feeu") if r.get("feeu") is not None else gd_rD(r, "gu") - gd_rD(r, "u") + gd_rD(r, "fu")

def gd_tot_lines(pre, n, gu, fe, fu, u, mark=False):
    """總毛損益 U|均單U、總手續費 U、(總資金費 U)、總淨損益 U|均單U（1111 2026-10-17：% 改成均單＝合計 U ÷ 總成交單）。"""
    fd = rn_q4(fu) != 0
    us = rn_pad([rn_su(gu), rn_su(-fe)] + ([rn_su(fu)] if fd else []) + [rn_su(u)])
    av = rn_pad([rn_su(rn_q4(gu / n) if n else 0), rn_su(rn_q4(u / n) if n else 0)])
    L = [f"{pre}毛損益 {us[0]}|均單{av[0]}", f"{pre}手續費 {us[1]}"] + ([f"{pre}資金費 {us[2]}"] if fd else [])
    L.append(f"{pre}淨損益 {us[-1]}|均單{av[1]}" + (E.pnl_emoji(rn_q4(u)) if mark else ""))
    return L

def gd_why_lines(recs):
    """v18.3（1111 2026-10-21 畫面）/summary 總表 出場原因：每一種原因一行，後面分 L、S、U、D 層（設定TP、移動SL、移動TP 各一行；
    強平、強制平倉…有才多一行，同樣格式）。v18.4.2：設定SL33|L層4|S層20|U層9|D層0（沒有空格）。"""
    tp = lambda r: r.get("why") == "TP"
    kinds = [("設定SL", lambda r: r.get("why") == "SL"), ("設定TP", lambda r: tp(r) and not int(r.get("n_tp") or 0)),
             ("移動SL", lambda r: r.get("why") == "SL(移動)"), ("移動TP", lambda r: tp(r) and int(r.get("n_tp") or 0) > 0)]
    other = sorted({r.get("why") for r in recs if r.get("why") and r.get("why") not in ("SL", "SL(移動)", "TP")}, key=lambda w: (w != "強平", w))
    kinds += [(w, (lambda w: lambda r: r.get("why") == w)(w)) for w in other]
    rows = []
    for nm, f in kinds:
        rs = [r for r in recs if f(r)]
        rows.append((nm, [len(rs)] + [sum(1 for r in rs if (r.get("nm") or "?")[0] == x) for x in "LSUD"]))
    # v18.4.2（1111 2026-10-22 畫面）：拿掉空格、不補對齊空白（太長 TG 會換行）：設定SL33|L層4|S層20|U層9|D層0
    return [f"{nm}{v[0]}|" + "|".join(f"{x}層{v[i + 1]}" for i, x in enumerate("LSUD")) for nm, v in rows]

def gd_sum_msgs(d, full=False, M=None, quiet=False):
    """⑤ /summary（模擬 /runt 網格；1111 2026-10-17 畫面）：第一則總表、第二則分幣種（太長照分隔線分頁）。
    最🈵金額＝今天同一時間最多佔用的錢（現貨每層U、空單每層保證金U）；持倉金額＝現在還抱著的單×每層U。
    v17.0.4（1111 2026-10-20）：/status 不再接本日成績（太長）；分幣種 總成交單 下面多一行 目前損益＝每一格的 % 加起來。"""
    M = M or GSIM
    GS = M.D
    recs = gd_recs(d, M)
    t_day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ8).timestamp()
    t_end = min(time.time(), t_day + 86400)
    span = f"{d[5:7]}/{d[8:10]} 00:00~{'24:00' if full else hhmmss()[:5]}(台灣時間)"
    foot = [f"時間:{hhmmss()}|{VERSION}"]
    spans, held = [], {}
    for r in recs:
        try:
            t1 = float(r.get("t_out") or 0)
            spans.append((r.get("sym"), max(float(r.get("t_in") or t1), t_day), t1, gd_rD(r, "amt")))
        except Exception:
            continue
    held_n = {}
    for G in list(GS.values()):
        for p in gd_held(G):
            held[G["sym"]] = held.get(G["sym"], Decimal(0)) + gd_pamt(G, p)
            held_n[G["sym"]] = held_n.get(G["sym"], 0) + 1
            spans.append((G["sym"], max(p["t_in"], t_day), t_end, gd_pamt(G, p)))
    def peak_n(sym=None):
        """今天同一時間最多佔用的錢，和那時候抱著幾單（v16.2：分幣種 最🈵金額 xx單|4U）。"""
        ev = []
        for sy, a, b, m in spans:
            if sym is None or sy == sym:
                ev += [(a, 1, m, 1), (b, 0, -m, -1)]       # 同一時間：先出場、再進場
        cur = top = Decimal(0)
        n = top_n = 0
        for _, _, m, d in sorted(ev, key=lambda e: (e[0], e[1])):
            cur += m; n += d
            if cur > top:
                top, top_n = cur, n
        return top, top_n
    peak = lambda sym=None: peak_n(sym)[0]
    syms = {r.get("sym") for r in recs} | set(GS)
    head = [f"📊 summary｜{ACCT} {M.tag}| 總表", span]
    if not syms:
        return [] if full or quiet else [head + ["", "本日沒有出場的單,也沒有持倉", ""] + foot]
    n = len(recs)
    L = head + ["", f"本日總計 {n}單⭐️"]
    if recs:
        L += ["出場原因"] + gd_why_lines(recs)                 # v18.3（1111 2026-10-21 畫面）：每一種原因一行，後面分 L、S、U、D 層
    sm = lambda rs, k: sum((gd_rD(r, k) for r in rs), Decimal(0))
    fees = lambda rs: sum((gd_rfee(r) for r in rs), Decimal(0))
    L += ["", f"最{GD_FULL}金額 {pct(peak())}U|{len(syms)}幣種",
          f"持倉金額 {pct(sum(held.values(), Decimal(0)))}U|{len([s for s in held if held[s]])}幣種🎯"]
    L += gd_tot_lines("總", n, sm(recs, "gu"), fees(recs), sm(recs, "fu"), sm(recs, "u"), mark=True)
    L += [""] + foot
    X0 = [f"📊 summary｜{ACCT} {M.tag}分幣種", span]
    for sym in sorted(syms):
        rs = [r for r in recs if r.get("sym") == sym]
        G = GS.get(sym)
        if G is not None:
            title, dr = gd_head(G), G["dir"]
        else:
            r0 = max(rs, key=lambda r: float(r.get("t_out") or 0))
            dr = r0.get("dir") or ""
            title = f"{sym} {dr} {pct(gd_rD(r0, 'amt'))}U"                       # v15.6 拿掉槓桿
        grp = lambda r: (r.get("nm") or "?")[0]
        lets = "LUSD"                                      # v16.1（1111 2026-10-19）：四段都列（沒開的方向也列 0 單）
        pk, pn = peak_n(sym)
        X = [f"{GD_SUM_MK}{title}", f"最{GD_FULL}金額 {pn}單|{pct(pk)}U", f"持倉金額 {held_n.get(sym, 0)}單|{pct(held.get(sym, Decimal(0)))}U🎯",   # v16.2（1111 2026-10-19）
                        f"總成交單 {len(rs)}單"]                # v15.8（1111 2026-10-18 畫面）：後面的 |L💰…單 拿掉
        lay_n, lay_p = {}, {}
        for r in rs:
            k = r.get("nm") or "?"
            lay_n[k] = lay_n.get(k, 0) + 1
            lay_p[k] = lay_p.get(k, Decimal(0)) + gd_rD(r, "np")
        X.append(f"目前損益 {rn_sp(sum(lay_p.values(), Decimal(0)))}")   # v17.0.4（1111 2026-10-20）：下面每一格的 % 加起來
        X0 += [GD_SUM_SEP] + X
        if not rs:
            continue
        ks = sorted(lay_n, key=lambda k: gd_nm_key(k) if k[:1] in "SULD" and k[1:].isdigit() else (9, 0))
        n0 = len(X)
        X += [""] + gd_cells(ks, lay_n, lay_p)
        X += [""] + gd_tot_lines("總", len(rs), sm(rs, "gu"), fees(rs), sm(rs, "fu"), sm(rs, "u"))
        for x in lets:
            xs = [r for r in rs if grp(r) == x]
            gu, fe, fu, u = sm(xs, "gu"), fees(xs), sm(xs, "fu"), sm(xs, "u")
            fd = rn_q4(fu) != 0
            us = rn_pad([rn_su(gu), rn_su(-fe)] + ([rn_su(fu)] if fd else []) + [rn_su(u)])
            X += ["", f"{GD_MK['sw' if x in 'SD' else 'sp']}{x} {len(xs)}單",   # v15.8：💰L 7單／💰U 0單／🈴S 0單；v18.0 🈴D 不寫帳戶
                  f"毛損益 {us[0]}", f"手續費 {us[1]}"] + ([f"資金費 {us[2]}"] if fd else []) + [f"淨損益 {us[-1]}"]
        X0 += X[n0:]
    X0 += [GD_SUM_SEP] + foot
    return [L, X0]

# ---------- 迴圈 ----------
def gd_events(G, send):
    """成交 → ② 進場通知；出場 → 記帳＋③ 出場通知（強平合成一則 💥）。回傳有沒有事件。"""
    new = sorted([p for p in G["pos"] if not p["told"]], key=lambda p: p["t_in"])
    for p in new:
        p["told"] = True
        print(f"[grid] 成交 {G['sym']} {gd_nm(p['kind'], p['k'])}@{p['ent']}")
        send("\n".join(gd_fill_lines(G, p)))
    outs = sorted([p for p in G["pos"] if p["out"]], key=lambda p: p["out"][0])
    liqs = {}
    for p in outs:
        G["pos"].remove(p)
        m = gd_money(G, p, p["out"][1])
        gd_done(G, p, m)
        print(f"[grid] 出場 {gd_M(G).tag}{G['sym']} {gd_nm(p['kind'], p['k'])} {p['out'][2]} @{p['out'][1]} 淨{m['netu']}")
        if p["out"][2] == "強平":
            liqs.setdefault((gd_grp(p), gd_blk(p["k"])), []).append((p, m))   # v18.0：每個帳戶的 S、D 各自一則
        else:
            send("\n".join(gd_exit_lines(G, p, m)))
    for key in sorted(liqs, key=lambda x: (x[0] != "S", x[1])):
        send("\n".join(gd_liq_lines(G, liqs[key])))
    if new or outs:
        gd_save_soon()
    return bool(new or outs)

def gd_tick(G, send, now):
    """每 0.25 秒（查完價、gd_touch 之後）：通知 → 資金費 → 每一單的 SL/TP（gd_rules，照 OKX 改單額度）→ 換格掛單 → 資金、規格通知。"""
    gd_events(G, send)
    if G.get("stop") and not G.get("stop_wait") and not gd_held(G) and not any(G["ord"].values()) and not G.get("o00"):
        gd_stop_end(G, send, now)                          # v18.5：停止中，最後一單出場了
        return
    if G.get("real"):                                      # v19.0：/run 實盤：資金費看 OKX（rg_sync）、SL/TP 送 OKX 條件單、掛單送 OKX
        if G["org"] is not None:
            rg_rules(G, now)
        rg_hang(G, now)
    else:
        gd_fund_tick(G, now)
        if G["org"] is not None:
            gd_rules(G, now)
        gd_hang(G, now)
    for x in G["note"]:
        send(x)
    G["note"].clear()

GD_SNAP = ("sl", "tp", "sl_g", "tp_g", "pend", "t_arm", "arm_px", "n_mv", "n_tp", "hi", "tp_t", "sl_t")

def gd_rules(G, now):
    """v17.0：每一單的 SL/TP（rn_rule），照 OKX 條件單額度：每 2 秒 20 次（GD_AMEND_N），一份額度＝同一個帳戶＋同一個商品：
    v18.1：sp＝這個帳戶的現貨（L、U）一份；合約＝那個帳戶這個幣的永續一份（S、D 一起）；第一次設、移動都算一次。
    第一次設的門檻（v18.3）：做多（L、U、D）GD_LONG_ARM 1.50%、做空（S）RN_ARM 0.80%。
    同一份額度這一拍要改的超過了 → 離現價最近的（SL 離現價最近；還沒設 SL 的看進場價）先改，其他這一拍不動、下一拍再算。"""
    by = {}
    for p in G["pos"]:
        px = G["px"].get(p["kind"])
        if not p["out"] and px is not None:
            by.setdefault(gd_fkey(p["kind"], p["k"]), []).append((p, px))   # v20.0：那個帳戶的現貨一份、那個帳戶的永續一份
    for a, ps in by.items():
        q = G.setdefault("amd", {}).setdefault(a, deque())
        while q and now - q[0] >= GD_AMEND_WIN:
            q.popleft()
        ps.sort(key=lambda x: abs(x[1] - (rn_cur_sltp(x[0])[0] or x[0]["ent"])))
        for p, px in ps:
            snap = {k: p.get(k) for k in GD_SNAP}
            pend0 = p["pend"]
            rn_rule(G[p["kind"]], p, px, now, GD_LONG_ARM if p["side"] == "L" else RN_ARM)
            if p["pend"] is not None and p["pend"] is not pend0:   # 這一拍要送一次（設 SL/TP 或移動）
                if len(q) >= GD_AMEND_N:
                    p.update(snap)                         # 額度用完：這一拍不改
                    G["n_amd"] = G.get("n_amd", 0) + 1
                else:
                    q.append(now)

def gd_ws_drop(G):
    """停掉的組：別的組（網格、實盤）沒在用的幣種不再訂閱。"""
    used = set()
    for H in list(GD.values()) + list(RD.values()):        # v19.0：模擬、實盤都算
        if H is not G:
            used |= {H[kd]["iid"] for kd in gd_kinds(H)}
    for kd in gd_kinds(G):
        if G[kd]["iid"] not in used:
            WS_WANT.discard(G[kd]["iid"])

async def gd_worker(app, sym, real=False):
    """一組網格的迴圈：每 0.25 秒查價一次（WS 正常時逐筆已經判斷過）、規則、換格；成交、出場由 gd_touch（逐筆）判斷。"""
    M = GREAL if real else GSIM
    G = M.D[sym]
    on = lambda: M.D.get(sym) is G and not G["done"]
    send = lambda txt: rt_chain(G, lambda t=txt: rt_send(app, G["chat"], t))
    if real:
        RG_APP["app"] = app
    try:
        for kd in gd_kinds(G):
            WS_WANT.add(G[kd]["iid"])
        while on():
          try:
            now = time.time()
            if now < G["nxt"] and not G["ev"].is_set():
                try:
                    await asyncio.wait_for(G["ev"].wait(), timeout=G["nxt"] - now)
                except asyncio.TimeoutError:
                    pass
            G["ev"].clear()
            if not on():
                return
            gd_events(G, send)
            now = time.time()
            if now < G["nxt"]:
                continue
            G["nxt"] = now + RN_STEP - (now % RN_STEP)
            for kd in gd_kinds(G):
                px = await gd_px(G, kd)
                if px is None or not on():
                    continue
                gd_touch(G, kd, px, time.time())           # WS 斷線時靠查價判斷（WS 正常時逐筆已判斷過，重複不影響）
            if not on():
                return
            gd_tick(G, send, time.time())
          except asyncio.CancelledError:
            raise
          except Exception as e:
            if not real:
                raise
            # v19.0：實盤不能因為一個例外就丟掉這一組（OKX 上還有掛單、持倉、條件單）→ 記下來、通知（10 分鐘一次）、1 秒後接著跑
            print("[run] worker 例外(繼續跑)", sym, type(e).__name__, e)
            rg_warn(G, "worker", f"BOT 內部錯誤(已經接著跑,OKX 上的單照常):{type(e).__name__}: {e}")
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[grid] worker fail", sym, type(e).__name__, e)
        G["done"] = True
        if GD.get(sym) is G:
            GD.pop(sym, None)
        gd_ws_drop(G)
        gd_save()
        await rt_send(app, G["chat"], f"{E.LOSS} runt 異常停止 {gd_head(G)}\n{type(e).__name__}: {e}\n時間:{hhmmss()}")

def gd_start(app, G):
    gd_M(G).D[G["sym"]] = G
    G["task"] = asyncio.create_task(gd_worker(app, G["sym"], bool(G.get("real"))))
    if G.get("real"):
        RG_APP["app"] = app
        rg_wake().set()
    gd_save_soon()

# ---------- 存檔、重開恢復 ----------
def gd_save_soon():
    GD_SAVE["dirty"] = True

async def gd_save_task():
    """有事情最快 1 秒存一次、平常 10 秒存一次（最高／最低毛利率一直在變）。"""
    while not SHUTTING_DOWN:
        await asyncio.sleep(0.25)
        now = time.time()
        try:
            if (GD_SAVE["dirty"] and now - GD_SAVE["t"] >= RN_SAVE_GAP) or ((GD or RD) and now - GD_SAVE["t"] >= RT_SAVE_SEC):
                gd_save()
        except Exception as e:
            print("[grid] save task fail", type(e).__name__, e)

def gd_dump(G):
    s = lambda v: None if v is None else str(v)
    def P(p):
        sl, tp, sl_g, tp_g = rn_cur_sltp(p)                # 存檔時剛送出的 SL/TP 一律當作已生效
        return {"kind": p["kind"], "k": p["k"], "ent": str(p["ent"]), "t_in": p["t_in"], "t_ord": p.get("t_ord"), "lev": str(p["lev"]),
                "qty": str(p["qty"]), "sl": s(sl), "tp": s(tp), "sl_g": s(sl_g), "tp_g": s(tp_g), "t_arm": p["t_arm"],
                "arm_px": s(p["arm_px"]), "n_mv": p["n_mv"], "n_tp": p.get("n_tp", 0), "hi": s(p["hi"]), "tp_t": p.get("tp_t", 0.0), "sl_t": p.get("sl_t", 0.0),
                "mfe": str(p["mfe"]), "mae": str(p["mae"]), "fu": str(p.get("fu", 0)), "fn": p.get("fn", 0),
                "ct": s(p.get("ct")), "got": s(p.get("got")),                            # v17.0：張數、現貨實得顆數
                "amt": s(p.get("amt")), "up": bool(p.get("up")), "lu": s(p.get("lu")),   # v17.0.3：自動調高的層；v18.4：進場時的每層U
                **(rg_pdump(p) if G.get("real") else {})}   # v19.0：實盤的 OKX 單號、張數、手續費、條件單
    O = lambda o: None if not o else {"k": o.get("k", 0), "px": str(o["px"]), "t": o["t"], **(rg_odump(o) if G.get("real") else {})}
    return {"v": 200, "sym": G["sym"], "dir": G["dir"], "amt": str(G["amt"]), "off": str(G["off"]),   # v20.0（v18.0＝180）
            "fee": {g: [str(a), str(b)] for g, (a, b) in (G.get("fee") or {}).items()},   # v17.0：帳戶費率
            "chat": G["chat"], "t_run": G["t_run"], "mmr": s(G.get("mmr")),
            "org": {"px": str(G["org"]["px"]), "t": G["org"]["t"]} if G.get("org") else None, "o00": O(G.get("o00")),
            "ord": {kd: O(o) for kd, o in G["ord"].items()}, "pos": [P(p) for p in G["pos"] if not p["out"]],
            "agg": {nm: {k: (v if isinstance(v, int) else str(v)) for k, v in a.items()} for nm, a in G["agg"].items()},
            "fund_last": G["sw"]["fund"]["last"] if G.get("sw") else None, "mix": bool(G.get("mix")),   # v18.4：改過每層U
            "stop": G.get("stop"),                                                                     # v18.5：停止中
            **({"real": True, "ff0": G.get("ff0") or {}, "lev_set": G.get("lev_set") or {}, "seen": (G.get("seen") or [])[-300:],
                "acx": (G.get("acx") or [])[-200:]} if G.get("real") else {})}   # v19.0

def gd_save():
    GD_SAVE["dirty"], GD_SAVE["t"] = False, time.time()
    for M in (GSIM, GREAL):                                # v19.0：模擬 runtgrid、實盤 rungrid 各自一個檔
        if M.real and not M.D and not M.hold and not os.path.exists(M.file):
            continue
        try:
            data = [gd_dump(G) for G in M.D.values() if not G["done"]] + list(M.hold)
            os.makedirs(os.path.dirname(M.file), exist_ok=True)
            tmp = M.file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, M.file)
        except Exception as e:
            print("[grid] save fail", M.cmd, e)

def gd_fund0():
    return {"t": None, "rate": None, "q": 0.0, "qt": 0.0, "last": None, "busy": False}

async def gd_specs(sym, has):
    """這個幣種要用的規格：有 S／D＝永續（iid、tick、最高槓桿），有 L／U＝現貨。查不到丟例外（訊息給 1111 看）。"""
    sw = sp = None
    if has["S"] or has.get("D"):                        # v18.0：D＝永續做多
        try:
            spec = await get_spec(sym)
        except Exception:
            raise RuntimeError(f"找不到幣種 {sym}(永續合約)")
        sw = {"iid": spec["iid"], "tick": spec["tick"], "sym": sym, "maxlev": spec["maxlev"], "fund": gd_fund0(),
              "lot": spec.get("lot"), "minsz": spec.get("minsz"), "ctval": spec.get("ctval")}      # v17.0：張數單位、最小張數、每張幾顆
    if has["L"] or has["U"]:
        ss = await get_spot_spec(sym)
        if not ss:
            raise RuntimeError(f"OKX 沒有 {spot_iid(sym)} 現貨(或現貨規格查不到)")
        sp = {"iid": ss["iid"], "tick": ss["tick"], "lot": ss.get("lot"), "minsz": ss.get("minsz")}   # v17.0：數量單位、最小數量
    return sp, sw

async def gd_rec_one(app, d, names, M=None):
    """恢復存檔裡的一組（查不到 OKX 規格就丟例外，那一筆留在存檔，背景再試）。v18.0：只恢復 v18.0 的存檔。
    v19.0：M＝GREAL＝/run 實盤（多恢復 OKX 單號、條件單；之後 rg_sync 以 OKX 為準對帳）。"""
    M = M or GSIM
    D = lambda v: None if v is None else Decimal(v)
    sym = d["sym"]
    if sym in M.D:
        return
    p = {"sym": sym, "dir": d["dir"], "amt": Decimal(d["amt"]), "off": Decimal(d["off"])}
    has = {x: x in p["dir"] for x in GD_DIRS}
    sp, sw = await gd_specs(sym, has)
    G = gd_new(p, sp, sw, d["chat"], float(d["t_run"]), D(d.get("mmr")))
    if sw and G["mmr"] is None:
        G["mmr"] = await rn_mmr({"iid": sw["iid"]})
    if sw:
        sw["fund"]["last"] = d.get("fund_last")
    now = time.time()
    if M.real:
        G["real"] = True
    o = d.get("org")
    G["org"] = {"px": Decimal(o["px"]), "t": float(o["t"])} if o else None
    o = d.get("o00")
    G["o00"] = ({"px": Decimal(o["px"]), "t": float(o["t"]), "live": now} if not M.real else rg_oload(o, slot=False)) if o else None
    for sl, o in (d.get("ord") or {}).items():
        if o and sl in G["ord"] and G.get(gd_skd(sl)):
            G["ord"][sl] = {"k": int(o["k"]), "px": Decimal(o["px"]), "t": float(o["t"]), "live": now} if not M.real else rg_oload(o)
    for y in d.get("pos") or []:
        kd = y["kind"]
        if not G.get(kd):
            continue
        G["pos"].append({"kind": kd, "side": "L" if gd_buy(kd, int(y["k"])) else "S", "k": int(y["k"]), "ent": Decimal(y["ent"]),
                         "t_in": float(y["t_in"]), "t_ord": y.get("t_ord"), "lev": Decimal(y["lev"]), "qty": Decimal(y["qty"]),
                         "sl": D(y.get("sl")), "tp": D(y.get("tp")), "sl_g": D(y.get("sl_g")), "tp_g": D(y.get("tp_g")), "pend": None,
                         "t_arm": y.get("t_arm"), "arm_px": D(y.get("arm_px")), "n_mv": int(y.get("n_mv", 0)), "n_tp": int(y.get("n_tp", 0)),
                         "hi": D(y.get("hi")), "tp_t": float(y.get("tp_t") or 0.0), "sl_t": float(y.get("sl_t") or 0.0), "mfe": Decimal(y.get("mfe", "0")),
                         "mae": Decimal(y.get("mae", "0")), "fu": Decimal(y.get("fu", "0")), "fn": int(y.get("fn", 0)),
                         "ct": D(y.get("ct")), "got": D(y.get("got")), "amt": D(y.get("amt")), "up": bool(y.get("up")), "lu": D(y.get("lu")),
                         "told": True, "out": None})
        if M.real:
            rg_pload(G["pos"][-1], y)                      # v19.0：OKX 單號、張數、手續費、條件單
    G["fee"] = {g: (Decimal(a), Decimal(b)) for g, (a, b) in (d.get("fee") or {}).items()} or None   # v17.0
    for nm, a in (d.get("agg") or {}).items():
        G["agg"][nm] = {k: (v if isinstance(v, int) else Decimal(v)) for k, v in a.items()}
    G["mix"] = bool(d.get("mix"))
    G["stop"] = d.get("stop")                              # v18.5：停止中（持倉跑到出場為止）
    if M.real:
        rg_init(G)
        G["ff0"] = d.get("ff0") or {}
        G["lev_set"] = d.get("lev_set") or {}
        G["seen"], G["acx"] = list(d.get("seen") or []), list(d.get("acx") or [])
    if not G["mix"]:
        gd_agg_pc(G, G["amt"])                             # v18.4：以前的格子補上 %（那時候每層U 都一樣）
    gd_start(app, G)
    n = len(gd_held(G))
    m = sum(1 for o in G["ord"].values() if o) + (1 if G.get("o00") else 0)
    names.append(f"{gd_head(G)} 埋伏{rt_p2(G['off'])}%|持倉 {n}單" + (f"|掛單 {m}張" if m else ""))

async def gd_hold_task(app, M=None):
    """重開時沒恢復成功的組，每 10 秒再試一次，恢復了發通知；全部恢復才結束。v19.0：M＝模擬／實盤。"""
    M = M or GSIM
    while not SHUTTING_DOWN and M.hold:
        await asyncio.sleep(REC_RETRY)
        names, chat = [], None
        for d in list(M.hold):
            try:
                await gd_rec_one(app, d, names, M)
            except Exception as e:
                print("[grid] recover retry fail", M.cmd, d.get("sym"), type(e).__name__, e)
                continue
            if d in M.hold:
                M.hold.remove(d)
            chat = chat or d.get("chat")
            gd_save()
        if names and chat:
            await rt_send(app, chat, "\n".join([f"{E.BOT} {M.cmd} 補恢復成功(bot 重開時沒查到 OKX)"] + names
                                               + ([] if M.hold else ["全部恢復了"]) + [f"時間:{hhmmss()}"]))

def gd_old_v14():
    """v14 以前 /runt 的存檔（runtlayer）：v15.0 換成網格，舊的組不恢復 → 改名 .v14 收起來，回傳要通知的組。"""
    try:
        if not os.path.exists(RN_FILE):
            return []
        with open(RN_FILE) as f:
            data = json.load(f)
        if not data:
            return []
        os.replace(RN_FILE, RN_FILE + ".v14")
        return [f"{d.get('sym')} {d.get('dir') or ''}|持倉 {len(d.get('pos') or [])}單".strip() for d in data]
    except Exception as e:
        print("[grid] 舊存檔處理失敗", type(e).__name__, e)
        return []

def gd_old_run():
    """v17.0：舊 /run（v11 佈局實盤）拿掉 → 它的存檔（runlayer_帳戶.json）還有組＝改名 .v16 收起來（不碰 OKX），回傳要通知的組。"""
    f = f"/srv/1111bot/data/runlayer_{ACCT}.json"
    try:
        if not os.path.exists(f):
            return []
        with open(f) as fh:
            data = json.load(fh)
        os.replace(f, f + ".v16")
        return [f"{d.get('sym')} {d.get('dir') or ''}|持倉 {len([p for p in d.get('pos') or [] if not p.get('out')])}層".strip()
                for d in data or []]
    except Exception as e:
        print("[grid] 舊 /run 存檔處理失敗", type(e).__name__, e)
        return []

async def gd_recover(app):
    """重開後恢復：持倉、掛單、原點接著跑（關機期間沒有看盤）。規格查不到 → 那一組留在存檔，背景每 10 秒再試。"""
    rn_log_prune()                                         # 逐單出場紀錄只留 30 天
    old = gd_old_v14()
    data = []
    try:
        if os.path.exists(GD_FILE):
            with open(GD_FILE) as f:
                data = json.load(f)
    except Exception as e:
        print("[grid] recover read fail", e)
    chat0 = next((d.get("chat") for d in data if d.get("chat")), None) or CHAT_ID
    v17 = []
    if data and any(int(d.get("v") or 0) < 200 for d in data):   # v20.0：v19 以前的存檔不恢復（格子 3%→2%、帳戶 3→5 個都不一樣）
        try:
            os.replace(GD_FILE, GD_FILE + ".v19")
        except Exception as e:
            print("[grid] v19 舊存檔改名失敗", e)
        v17 = [f"{d.get('sym')} {d.get('dir') or ''}|持倉 {len(d.get('pos') or [])}單".strip() for d in data]
        data = []
    oldrun = gd_old_run()
    if data:
        await rec_okx_ready({d["sym"] for d in data if set(d.get("dir") or "") & {"S", "D"}})
        if any(set(d.get("dir") or "") & {"L", "U"} for d in data):
            for i in range(10):
                if await okx_spot_specs_all():
                    break
                await asyncio.sleep(3)
    names, bad = [], []
    for d in data:
        try:
            await gd_rec_one(app, d, names)
        except Exception as e:
            print("[grid] recover fail", d.get("sym"), type(e).__name__, e)
            GD_HOLD.append(d)
            bad.append(f"{d.get('sym')} {d.get('dir') or ''}".strip())
    gd_save()
    L = []
    if names:
        L += [f"{E.BOT} runt 已自動恢復(bot 重開)"] + names + ["持倉的 SL/TP、掛單、原點不變,關機期間沒有看盤"]
    if v17:
        L += [f"{E.WARN} v20.0 /runt 換新策略(每2%、每10層一個帳戶、5個帳戶):舊的 runt 沒有恢復(已停止,存檔改名 .v19 收起來,BOT 沒有動 OKX):"] + v17
    if oldrun:
        L += [f"{E.WARN} v17.0 舊的 /run、/stop 拿掉了:舊 /run 的存檔還有組(已收起來,BOT 沒有動 OKX,持倉請到 OKX 看):"] + oldrun
    if bad:
        L += [f"{E.WARN} 以下這次沒查到 OKX,沒有清掉(存檔保留),BOT 每 10 秒再試,恢復了會再通知:"] + bad
    if old:
        L += [f"{E.WARN} v15.0 /runt 換成網格策略:舊版的 runt 沒有恢復(已停止,持倉沒有算損益):"] + old
    if L and chat0:
        L.append(f"時間:{hhmmss()}")
        await rt_send(app, chat0, "\n".join(L))
    for a in fd_accts():                                   # v17.0：恢復完先讀一次帳戶（可用、帳戶模式），之後每分鐘
        try:
            await fd_refresh(a)
        except Exception as e:
            print("[fd] 重開讀帳戶失敗", a, type(e).__name__, e)

# ---------- v17.0（1111 2026-10-19）：每一層下不下得了、每層最少多少 U ----------
def gd_side_layers(x):
    """一邊的 33 層（加 L00）：L＝L00～L33、U＝U01～U33（現貨）；S＝S01～S33（做空）、D＝D01～D33（做多）（永續）。回傳 [(種類, k)]。"""
    if x == "L":
        return [("sp", -n) for n in range(0, GD_N + 1)]
    if x == "U":
        return [("sp", n) for n in range(1, GD_N + 1)]
    if x == "S":
        return [("sw", n) for n in range(1, GD_N + 1)]
    return [("sw", -n) for n in range(1, GD_N + 1)]

def gd_side_kd(x):
    return "sp" if x in "LU" else "sw"

def gd_first(x):
    """v17.0.3：每一邊的第一層（離原點最近）：L＝L00、U＝U01、S＝S01、D＝D01。"""
    return {"L": ("sp", 0), "U": ("sp", 1), "S": ("sw", 1), "D": ("sw", -1)}[x]

def gd_up_lines(G, o, warn=False):
    """v17.0.3：每層U 不夠、自動用最低金額的層，一邊一行（沒有＝空的）。"""
    out = []
    for x in "LUSD":
        if G["has"].get(x):
            ks = gd_bad(G, x, G["amt"], o)
            if ks:
                out.append(f"{GD_MK[gd_side_kd(x)]}{gd_rng_txt(x, ks)} 每層U 不夠,{'之後' if warn else ''}自動用最低金額")
    return out

def gd_bad(G, x, amt, o):
    """這一邊用每層 amt U、原點 o，下不了的層（k 的清單）：現貨買不到最小數量或扣完手續費賣不掉、合約不到最小張數。"""
    return [k for kd, k in gd_side_layers(x) if not gd_size(G, kd, k, gd_lpx(o, kd, k, G[kd]["tick"]), o, amt)["ok"]]

def gd_layer_min(G, kd, k, o):
    """這一層每層最少要多少 U（0.5U 一級往上取）；1000U 還不行＝None。"""
    lp = gd_lpx(o, kd, k, G[kd]["tick"])
    ok = lambda n: gd_size(G, kd, k, lp, o, GD_MIN_STEP * n)["ok"]
    hi = int(GD_MIN_CAP / GD_MIN_STEP)
    if not ok(hi):
        return None
    lo = 1
    while lo < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid + 1
    return GD_MIN_STEP * lo

def gd_side_min(G, x, o):
    """這一邊 33 層全部下得了，每層最少多少 U（None＝1000U 也不行）。"""
    out = Decimal(0)
    for kd, k in gd_side_layers(x):
        v = gd_layer_min(G, kd, k, o)
        if v is None:
            return None
        out = max(out, v)
    return out

def gd_rng_txt(x, ks):
    """層的清單 → L00、U01～U33、S01～S11（連續的寫成一段）。"""
    ns = sorted(abs(k) for k in ks)
    segs, a = [], None
    for i, n in enumerate(ns):
        if a is None:
            a = n
        if i + 1 == len(ns) or ns[i + 1] != n + 1:
            segs.append(f"{x}{a:02d}" if a == n else f"{x}{a:02d}～{x}{n:02d}")
            a = None
    return "、".join(segs)

def gd_round_lines(G, o):
    """v17.0 ① 取整後最少：每一邊照 OKX 單位往下取之後，實際數量只有應該的幾 %（最少的那一層）。"""
    r = {}
    for x in "LUSD":
        if not G["has"].get(x):
            continue
        best = None
        for kd, k in gd_side_layers(x):
            lp = gd_lpx(o, kd, k, G[kd]["tick"])
            z = gd_lsize(G, kd, k, lp, o)
            v = z["q"] * lp / (G["amt"] * z["lev"])
            if best is None or v < best[0]:
                best = (v, gd_nm(kd, k))
        r[x] = best
    pc = lambda v: f"{(v * 100).quantize(Decimal('0.1'), rounding=ROUND_DOWN)}%"
    rows = [(GD_MK[kd], [x for x in xs if x in r]) for kd, xs in (("sp", "LU"), ("sw", "SD"))]
    rows = [(mk, xs) for mk, xs in rows if xs]
    c1 = rn_pad([pc(r[xs[0]][0]) for _, xs in rows])
    two = [xs for _, xs in rows if len(xs) > 1]
    c2 = iter(rn_pad([pc(r[xs[1]][0]) for xs in two]))
    out = []
    for (mk, xs), a in zip(rows, c1):
        t = f"{mk}{xs[0]} {a}({r[xs[0]][1]})"
        if len(xs) > 1:
            t += f"|{xs[1]} {next(c2)}({r[xs[1]][1]})"
        out.append(t)
    return out


# ---------- v17.0（1111 2026-10-19）：帳戶資金、帳戶模式（兩個帳戶真的可用 − 模擬佔用 ＋ 模擬已實現） ----------
def fd_bad(lv, pm):
    """帳戶模式、持倉模式不能跑網格的原因（現貨模式不能做合約；單向持倉 S、D 會跟別的單併倉）；沒問題＝None。"""
    why = []
    if str(lv) == "1":
        why.append(FD_LV["1"])
    if pm and pm != "long_short_mode":
        why.append(FD_PM.get(pm, pm))
    return "、".join(why) or None

def fd_mode(acct):
    f = FD.get(acct) or {}
    return f"{FD_LV.get(f.get('lv'), f.get('lv') or '?')}|{FD_PM.get(f.get('pm'), f.get('pm') or '?')}"

def fd_halt(acct):
    """這個帳戶的帳戶模式／持倉模式被改成不能做 → 先不掛新單。"""
    return bool((FD.get(acct) or {}).get("bad"))

def fd_accts():
    """要讀的帳戶：這個帳戶；有進行中的組（v20.0：L、U、S、D 都分在 5 個帳戶）＝加 o1111o～o5555o。"""
    a = [ACCT]
    if any(not G.get("done") for G in list(GD.values()) + list(RD.values())):   # v19.0：實盤也算
        a += [x for x in GD_XACCT if x != ACCT]
    return a

def fd_used(acct):
    """這個 bot 在這個帳戶的模擬佔用 U：掛單（現貨＝數量×價格、合約＝保證金）＋持倉（現貨＝成本、合約＝保證金每層U）。"""
    u = Decimal(0)
    for G in list(GD.values()):
        if G.get("done"):
            continue
        for sl, o in G["ord"].items():
            kd = gd_skd(sl)
            if not o or not G.get(kd):
                continue
            n = o.get("nxt")                               # v20.0：換格途中（舊的撤單確認前、新的已經排好）兩張都算；同一個帳戶算大的那張
            xs = [o] if not n else ([o, n] if gd_kacct(kd, o["k"]) != gd_kacct(kd, n["k"])
                                    else [max((o, n), key=lambda y: gd_need(G, kd, y["k"], y["px"]))])
            for x in xs:
                if gd_kacct(kd, x["k"]) == acct:
                    u += gd_need(G, kd, x["k"], x["px"])
        o = G.get("o00")
        if o and o.get("live") is not None and acct == gd_kacct("sp", 0):   # v20.0：L00 在第一個帳戶
            u += gd_need(G, "sp", 0, o["px"])
        for p in G["pos"]:
            if not p["out"] and gd_kacct(p["kind"], p["k"]) == acct:
                u += p["qty"] * p["ent"] if p["kind"] == "sp" else gd_pamt(G, p)
    return u

def gd_nacct(nm):
    """格子名稱 → 帳戶（v20.0：L、U、S、D 都是那一層的帳戶）。"""
    return GD_XACCT[gd_blk(int(nm[1:]))]


def fd_real(acct):
    """這個 bot 在這個帳戶的模擬已實現淨損益 U（/runt 開始到現在出場的單；合約算在那一層的帳戶）。"""
    return sum((a["u"] for G in list(GD.values()) for nm, a in G["agg"].items() if gd_nacct(nm) == acct), Decimal(0))


def fd_sim_write():
    """v18.0：寫這個 bot 的模擬佔用、已實現（每個帳戶）到共用紀錄，給另外 5 個 bot 讀（每 5 秒）。"""
    try:
        d = {"t": time.time(), "acct": ACCT, "used": {a: str(fd_used(a)) for a in fd_accts()},
             "real": {a: str(fd_real(a)) for a in fd_accts()},
             "upl": {a: str(fd_upl(a)) for a in fd_accts()},                       # v20.0.4：模擬未實現（/coins、/status 權益）
             "runt": sorted(s for s, G in GD.items() if not G.get("done")),       # v20.0：同一個幣只能在一個 bot 下（gd_other_bot）
             "run": sorted(s for s, G in RD.items() if not G.get("done"))}
        f = f"{FD_SIM_DIR}/fd_sim_{ACCT}.json"
        os.makedirs(FD_SIM_DIR, exist_ok=True)
        with open(f + ".tmp", "w") as fh:
            json.dump(d, fh)
        os.replace(f + ".tmp", f)
    except Exception as e:
        print("[fd] 共用紀錄寫不了", type(e).__name__, e)


def fd_sim_read():
    """v18.0：讀另外 5 個 bot 的模擬佔用、已實現（超過 60 秒沒更新＝那個 bot 沒在跑，不算）→ FD_SIM["ext"]：帳戶 → [佔用, 已實現, 未實現(v20.0.4)]。"""
    ext, now = {}, time.time()
    try:
        names = [x for x in os.listdir(FD_SIM_DIR) if x.startswith("fd_sim_") and x.endswith(".json") and x != f"fd_sim_{ACCT}.json"]
    except Exception:
        names = []
    for x in names:
        try:
            with open(f"{FD_SIM_DIR}/{x}") as fh:
                d = json.load(fh)
            if now - float(d.get("t") or 0) > FD_SIM_OLD:
                continue
            for i, k in enumerate(("used", "real", "upl")):
                for a, v in (d.get(k) or {}).items():
                    ext.setdefault(a, [Decimal(0), Decimal(0), Decimal(0)])[i] += Decimal(v)
        except Exception as e:
            print("[fd] 共用紀錄讀不了", x, type(e).__name__, e)
    FD_SIM["ext"], FD_SIM["t"] = ext, now


def gd_other_bot(sym, M):
    """v20.0：6 個 bot 共用 o1111o～o5555o → 同一個幣同一種（runt／run）只能在一個 bot 下：別的 bot 正在跑 → 那個 bot 的帳戶；沒有＝None。
    看共用紀錄 fd_sim（每 5 秒寫一次；超過 60 秒沒更新＝那個 bot 沒在跑）。"""
    now = time.time()
    try:
        names = sorted(x for x in os.listdir(FD_SIM_DIR) if x.startswith("fd_sim_") and x.endswith(".json") and x != f"fd_sim_{ACCT}.json")
    except Exception:
        return None
    for x in names:
        try:
            with open(f"{FD_SIM_DIR}/{x}") as fh:
                d = json.load(fh)
            if now - float(d.get("t") or 0) <= FD_SIM_OLD and sym in (d.get(M.cmd) or []):
                return d.get("acct") or x[7:-5]
        except Exception:
            continue
    return None


def fd_used_all(acct):
    """v18.0：這個帳戶 6 個 bot 合起來的模擬佔用（這個 bot 即時＋別的 bot 共用紀錄）。"""
    return fd_used(acct) + (FD_SIM["ext"].get(acct) or [Decimal(0)] * 3)[0]


def fd_real_all(acct):
    """v18.0：這個帳戶 6 個 bot 合起來的模擬已實現。"""
    return fd_real(acct) + (FD_SIM["ext"].get(acct) or [Decimal(0)] * 3)[1]


def fd_upl(acct):
    """v20.0.4：這個 bot 在這個帳戶 /runt 還抱著的單的未實現損益 U（現價；跟 /status 持倉損益的 U 同一個算法 gd_upl）。"""
    u = Decimal(0)
    for G in list(GD.values()):
        if G.get("done"):
            continue
        for p in G["pos"]:
            if not p["out"] and gd_kacct(p["kind"], p["k"]) == acct:
                px = (G.get("px") or {}).get(p["kind"])
                if px is not None:
                    u += gd_upl(p, px)
    return u


def fd_money(acct):
    """v20.0.4（1111 2026-10-30）：/coins、/status 的資金（/runt、/run 同一套，以 OKX 的資金現況為準）：
      可用＝OKX 可用 − /runt 模擬佔用 ＋ /runt 模擬已實現（只有 /run＝OKX 可用）；
      佔用＝OKX 佔用（掛單＋合約保證金）＋ OKX 現貨持倉成本（OKX 把現貨算成幣，不在 USDT 佔用裡）＋ /runt 模擬佔用；
      餘額＝可用＋佔用；權益＝餘額＋OKX 未實現（合約 upl＋現貨 spotUpl）＋ /runt 模擬未實現。
    /runt 的佔用、已實現、未實現＝6 個 bot 合起來（共用紀錄 fd_sim）。還沒讀到 OKX＝None。"""
    f = FD.get(acct)
    if not f or f.get("avail") is None:
        return None
    used, real = fd_used_all(acct), fd_real_all(acct)
    upl = fd_upl(acct) + (FD_SIM["ext"].get(acct) or [Decimal(0)] * 3)[2]
    avail = f["avail"] - used + real
    occ = (f.get("frozen") or Decimal(0)) + (f.get("rcost") or Decimal(0)) + used
    cash = avail + occ
    return {"avail": avail, "used": occ, "cash": cash, "eq": cash + (f.get("rupl") or Decimal(0)) + upl}

def fd_avail(acct, back=Decimal(0)):
    """模擬可用＝真的可用（每分鐘讀 OKX）− 6 個 bot 的模擬佔用 ＋ 模擬已實現（＋back：換格撤掉那一張還回來的）。沒讀過＝None（不限）。
    v18.0：o2222o 自己的 runt 跟 o1111o 放在 o2222o 的合約不會重複用同一筆錢（共用紀錄 fd_sim）。"""
    f = FD.get(acct)
    if not f or f.get("avail") is None:
        return None
    return f["avail"] - fd_used_all(acct) + fd_real_all(acct) + back

async def fd_refresh(acct, app=None):
    """讀這個帳戶的 USDT 可用、餘額、權益、佔用＋帳戶模式、持倉模式（/runt 開始、每分鐘巡邏、/coins）。成功回 None，失敗回原因。
    巡邏時（app）帳戶模式、持倉模式變成不能做 → 那個帳戶先不掛新單、通知一次；改回來 → 通知、接著掛。"""
    b, c = await asyncio.gather(api("GET", "/api/v5/account/balance", acct=acct),   # v20.0.4：全部幣（現貨持倉成本、未實現）
                                api("GET", "/api/v5/account/config", acct=acct))
    for r in (b, c):
        if str(r.get("code")) != "0":
            return f"{r.get('code')} {r.get('msg') or ''}".strip()
    dets = (b.get("data") or [{}])[0].get("details") or []
    x = next((d for d in dets if d.get("ccy") == "USDT"), {})
    D_ = lambda k, y=None: Decimal((x if y is None else y).get(k) or "0")
    rcost, rupl = Decimal(0), D_("upl")                    # v20.0.4：OKX 現貨持倉成本（均價×數量；沒有均價＝市值）、未實現（合約 upl＋現貨 spotUpl）
    for y in dets:
        if y.get("ccy") in ("USDT", None) or D_("cashBal", y) <= 0:
            continue
        rcost += D_("openAvgPx", y) * D_("cashBal", y) if y.get("openAvgPx") not in (None, "") else D_("eqUsd", y)
        rupl += D_("spotUpl", y)
    cfg = (c.get("data") or [{}])[0]
    f = FD.setdefault(acct, {})
    old = f.get("bad")
    f.update(avail=D_("availBal"), cash=D_("cashBal"), eq=D_("eq"), frozen=D_("frozenBal"), rcost=rcost, rupl=rupl,
             lv=str(cfg.get("acctLv") or ""), pm=cfg.get("posMode") or "", iso=cfg.get("ctIsoMode") or "", perm=cfg.get("perm"), t=time.time())
    f["bad"] = fd_bad(f["lv"], f["pm"])
    if app is not None and f["bad"] != old and (f["bad"] or old):
        if f["bad"]:
            txt = [f"🚨 {acct} 變成 {f['bad']}", "所有 runt 先不掛新單(持倉的 SL/TP 照常)", "改回 合約模式|雙向持倉 會自動接著掛"]
        else:
            txt = [f"✅ {acct} 改回 {fd_mode(acct)}", "runt 接著掛單"]
        chat = CHAT_ID or next((G["chat"] for G in GD.values()), None)
        if chat:
            await rt_send(app, chat, "\n".join(txt + [f"時間:{hhmmss()}"]))
    return None

async def fd_fee(acct, sym=None):
    """讀帳戶手續費率（% 掛單、吃單）：sp＝現貨、sw＝USDT 永續。sym＝那個幣自己的費率（OKX 有些幣是另一組費率）；
    沒給＝帳戶一般的費率（/coins）。讀不到的那一種不放（用預設）。"""
    out = {}
    for kd, q in (("sp", "SPOT"), ("sw", "SWAP")):
        path = f"/api/v5/account/trade-fee?instType={q}"
        if sym:
            path += f"&instId={spot_iid(sym)}" if kd == "sp" else f"&instFamily={sym[:-4]}-USDT"
        r = await api("GET", path, acct=acct)
        try:
            d = (r.get("data") or [])[0]
            v = None
            for a, b in ([("makerU", "takerU")] if kd == "sw" else []) + [("maker", "taker")]:
                if d.get(a) not in (None, "") and d.get(b) not in (None, ""):
                    v = (d[a], d[b]); break
            if v is None and d.get("feeGroup"):
                v = (d["feeGroup"][0]["maker"], d["feeGroup"][0]["taker"])
            out[kd] = (-Decimal(v[0]) * 100, -Decimal(v[1]) * 100)   # OKX：負＝收手續費（-0.0008 → 0.08%）
        except Exception:
            print("[fd] 手續費率讀不到", acct, path, r.get("code"), r.get("msg"))
    if out and not sym:
        FD.setdefault(acct, {})["fee"] = out
    return out

async def fd_fee_for(acct, sym):
    """這個幣的費率：先讀那個幣自己的，讀不到的那一種用帳戶一般的。"""
    a = await fd_fee(acct, sym)
    if len(a) < 2:
        a = {**(await fd_fee(acct)), **a}
    return a

def fd_money_lines(acct):
    """v20.0.5（1111 2026-10-30 畫面）：一個帳戶的資金（/coins、/status 同一個畫面）：帳戶 帳戶模式|持倉模式 → 餘額、權益、可用、佔用（fd_money，2 位小數）。"""
    f = FD.get(acct) or {}
    m = fd_money(acct)
    if not m:
        return [f"{acct} 資金查詢中"]                     # 開機還沒讀到 OKX（每分鐘讀一次）
    head = f"{acct} {fd_mode(acct)}" + (f"🚨{f['bad']}" if f.get("bad") else "")
    rows = [("餘額", m["cash"]), ("權益", m["eq"]), ("可用", m["avail"]), ("佔用", m["used"])]
    return [head] + [f"{k} {v}" for (k, _), v in zip(rows, rn_pad([f"{v:.2f}U" for _, v in rows]))]


def fd_status_blocks():
    """/status 最上面的資金（v20.0.5：跟 /coins 一樣每個帳戶一段；有 /runt＝資金(模擬)、只有 /run＝資金(OKX)；數字＝fd_money＝/coins）。"""
    title = "資金(模擬)" if any(not G.get("done") for G in GD.values()) else "資金(OKX)"
    bs = [fd_money_lines(a) for a in fd_accts()]
    return [[title] + bs[0]] + bs[1:] if bs else [[title]]

async def gd_spec_check(app):
    """每小時查一次 OKX 規格；每層U 不夠的層變了 → 通知（v17.0.3：不夠的層之後自動用最低金額，不再不掛；已經持倉的照常）。"""
    await okx_specs_all()
    await okx_spot_specs_all()
    norm = lambda d: {k: set((d or {}).get(k) or ()) for k in ("sp", "sw")}   # 種類 → 每層U 不夠的層
    for G in list(GD.values()) + list(RD.values()):        # v19.0：實盤也查
        if G.get("done") or G.get("org") is None:
            continue
        o = G["org"]["px"]
        def ups():                                         # v17.0.3：每層U 不夠的層＝之後自動用最低金額（不再不掛）
            u = norm({})
            for x in G["dir"]:
                u[gd_side_kd(x)].update(gd_bad(G, x, G["amt"], o))
            return u
        old = ups()                                        # 舊的規格
        a, b = SPOT_SPEC.get(G["sym"]), SPEC_CACHE.get(G["sym"])
        if G.get("sp") and a:
            G["sp"].update(lot=a["lot"], minsz=a["minsz"])
        if G.get("sw") and b:
            G["sw"].update(lot=b["lot"], minsz=b["minsz"], ctval=b["ctval"])
        if ups() == old:
            continue
        L = ([f"{E.WARN} OKX 規格變了 {G['sym']}"] + (gd_up_lines(G, o, warn=True) or ["每一層 每層U 都夠了"]) + ["(已經持倉的照常)"])
        await rt_send(app, G["chat"], "\n".join(L + [f"時間:{hhmmss()}"]))

async def fd_task(app):
    """v17.0 巡邏：有 runt 時每 60 秒讀帳戶（可用、帳戶模式、持倉模式），每小時查 OKX 規格；v18.0：每 5 秒共用模擬佔用。"""
    t_f, t_s, t_x = 0.0, time.time(), 0.0
    while not SHUTTING_DOWN:
        await asyncio.sleep(1)
        now = time.time()
        if now - t_x >= FD_SIM_GAP:                        # v18.0：6 個 bot 共用的模擬佔用（每 5 秒寫自己的、讀別人的）
            t_x = now
            fd_sim_write()
            fd_sim_read()
        if not GD and not RD:
            continue
        if now - t_f >= FD_GAP:
            t_f = now
            for a in fd_accts():
                try:
                    e = await fd_refresh(a, app)
                    if e:
                        print("[fd] 帳戶讀不到", a, e)
                except Exception as ex:
                    print("[fd] refresh fail", a, type(ex).__name__, ex)
        if now - t_s >= FD_SPEC_GAP:
            t_s = now
            try:
                await gd_spec_check(app)
            except Exception as ex:
                print("[fd] spec check fail", type(ex).__name__, ex)


# ---------- v17.0（1111 2026-10-19）：/runt 下的時候的檢查（照實盤） ----------
async def gd_coin_busy(G, a, kds):
    """這個帳戶在這個幣有沒有別的東西：合約持倉、掛單（kds 裡有的種類）、現貨（零頭不算＝不到最小數量）。回傳 [(標題, 說明)]、錯誤。"""
    out = []
    sym, base = G["sym"], G["sym"][:-4]
    if "sw" in kds:
        r = await api("GET", f"/api/v5/account/positions?instType=SWAP&instId={G['sw']['iid']}", acct=a)
        if str(r.get("code")) != "0":
            return out, f"{r.get('code')} {r.get('msg') or ''}".strip()
        sides = []
        for d in r.get("data") or []:
            try:
                if Decimal(d.get("pos") or "0") != 0:
                    sides.append("多單" if d.get("posSide") == "long" else ("空單" if d.get("posSide") == "short" else "合約"))
            except Exception:
                pass
        if sides:
            out.append((f"{a} 有 {sym} {'、'.join(sorted(set(sides)))}持倉", "👉處理掉再下"))
    r = await api("GET", "/api/v5/trade/orders-pending", acct=a)
    if str(r.get("code")) != "0":
        return out, f"{r.get('code')} {r.get('msg') or ''}".strip()
    iids = {G[kd]["iid"] for kd in kds if G.get(kd)}
    n = sum(1 for d in r.get("data") or [] if d.get("instId") in iids)
    if n:
        out.append((f"{a} 有 {sym} 掛單 {n}張", "👉撤掉再下"))
    if G.get("real"):                                      # v19.0：實盤多查條件單（SL/TP），避免跟手動的單混在一起
        n = 0
        for t in ("oco", "conditional"):
            r = await api("GET", f"/api/v5/trade/orders-algo-pending?ordType={t}", acct=a)
            if str(r.get("code")) != "0":
                return out, f"{r.get('code')} {r.get('msg') or ''}".strip()
            n += sum(1 for d in r.get("data") or [] if d.get("instId") in iids)
        if n:
            out.append((f"{a} 有 {sym} 條件單 {n}張", "👉撤掉再下"))
    if "sp" in kds:
        r = await api("GET", f"/api/v5/account/balance?ccy={base}", acct=a)
        if str(r.get("code")) != "0":
            return out, f"{r.get('code')} {r.get('msg') or ''}".strip()
        x = next((d for d in ((r.get("data") or [{}])[0].get("details") or []) if d.get("ccy") == base), {})
        q = Decimal(x.get("cashBal") or "0")
        if q > 0 and q >= (G["sp"].get("minsz") or Decimal(0)):
            out.append((f"{a} 有 {base} 現貨 {pct(q)}顆", "👉賣掉或轉走再下"))
    return out, None

async def gd_check(G, o):
    """v17.0：/runt 開始前的檢查（照實盤，只查不下單）：
    ① 帳戶讀得到、帳戶模式＝合約模式、雙向持倉（同時讀可用、手續費率）
    ② 每一邊的第一層下得了（OKX 最小數量、扣手續費賣得掉、最小張數），不行 → 每層最少多少 U
    ③ 那個幣沒有持倉、掛單、現貨  ④ 可用夠開始時要掛的單。
    v18.0：有 S／D（只有 o1111o）＝三個帳戶（o1111o、o2222o、o3333o）都要合約模式、雙向持倉，那個幣三個帳戶都沒有合約持倉、合約掛單；
      現貨只查這個帳戶（o2222o、o3333o 的現貨不查：它們可以自己囤同一個幣）。
    回傳 (不過的原因 [(標題, [說明…])], 開始通知要多的那幾行)。"""
    amt, has = G["amt"], G["has"]
    real = bool(G.get("real"))                             # v19.0：/run 實盤（可用＝OKX 真的可用；多查條件單、逐倉保證金自動轉入）
    xs = has["S"] or has["D"]
    spot = has["L"] or has["U"]
    accts = list(GD_XACCT)                                 # v20.0：L、U、S、D 都分在 5 個帳戶 → 5 個帳戶都要查
    bad, errs, fees = [], {}, {}
    for a in accts:
        e = await fd_refresh(a)
        if e:
            errs[a] = e
        else:
            fees[a] = await fd_fee_for(a, G["sym"])
    if not real:
        fd_sim_read()                                      # v18.0：別的 bot 的模擬佔用
    G["fee"] = {}                                          # v20.0：每個帳戶的現貨（"sp:帳戶"）、永續（帳戶）
    for a in GD_XACCT:
        G["fee"][f"sp:{a}"] = (fees.get(a) or {}).get("sp") or GD_FEE_DEF["sp"]
        G["fee"][a] = (fees.get(a) or {}).get("sw") or GD_FEE_DEF["sw"]
    first = [x for x in "LUSD" if has[x] and not gd_size(G, *gd_first(x), gd_lpx(o, *gd_first(x), G[gd_side_kd(x)]["tick"]), o)["ok"]]
    if first:                                              # v17.0.3（1111 2026-10-20）：只有第一層下不了才擋；後面不夠的層自動用最低金額
        L = []
        for x in first:
            kd, k = gd_first(x)
            mn = pct(G[kd].get("minsz") or 0)
            L.append(f"{GD_MK[kd]}{gd_nm(kd, k)} {'買' if kd == 'sp' else '開'}不了(最少{mn}{'顆' if kd == 'sp' else '張'})")
        mins = [gd_layer_min(G, *gd_first(x), o) for x in "LUSD" if has[x]]
        if all(v is not None for v in mins):
            L.append(f"👉每層最少 {max(mins)}U")
        else:
            no = [x for x in "LUSD" if has[x] and gd_layer_min(G, *gd_first(x), o) is None]
            L.append(f"👉這個幣 {'、'.join(no)} 這邊每層 {pct(GD_MIN_CAP)}U 也下不了,方向拿掉 {''.join(no)}")
        bad.append(("每層U 不夠,第一層下不了", L))
    for a in accts:
        if a in errs:
            bad.append((f"查不到 {a} 的帳戶({errs[a]})", ["👉稍後再試"]))
        elif FD[a].get("bad"):
            bad.append((f"{a} 是 {FD[a]['bad']}", ["👉到 OKX 改成 合約模式|雙向持倉"]))
        elif real and FD[a].get("perm") is not None and "trade" not in str(FD[a]["perm"]):
            bad.append((f"{a} 的 API Key 沒有交易權限({FD[a]['perm']})", ["👉到 OKX API 管理把這個 Key 加上「交易」權限(IP 白名單照舊)"]))
        elif real and xs and FD[a].get("iso") == "autonomy":
            bad.append((f"{a} 逐倉保證金是手動轉入", ["👉到 OKX 設定 逐倉保證金 改成 自動轉入"]))
    for a in accts:
        kds = (["sp"] if spot else []) + (["sw"] if xs else [])   # v20.0：現貨也分帳戶 → 每個帳戶都查現貨
        if not kds or a in errs:
            continue
        busy, e = await gd_coin_busy(G, a, kds)
        if e:
            bad.append((f"查不到 {a} 的 {G['sym']} 持倉、掛單({e})", ["👉稍後再試"]))
        bad += [(t, [x]) for t, x in busy]
    need = amt * ((1 if spot else 0) + (1 if has["S"] else 0) + (1 if has["D"] else 0))   # 開始時要掛的單（L00 或 U01、S01、D01 都在第一個帳戶）
    a0 = GD_XACCT[0]
    av = (rg_avail(a0) if real else fd_avail(a0)) if a0 not in errs else None
    if av is not None and av < need:
        bad.append((f"{a0} 可用 {av:.2f}U,開始要掛的單要 {need:.2f}U", ["👉轉 U 進來,或每層U 調小"]))
    if bad:
        return bad, []
    info = ["", "✅OKX檢查"] + [f"{a} {fd_mode(a)}" for a in accts]
    what = "、".join(t for t, ok in (("現貨", spot), ("合約持倉", xs)) if ok)
    info.append(f"{G['sym']} {'五個帳戶' if len(accts) == 5 else f'{len(accts)}個帳戶'}沒有{what}、掛單")   # v20.0
    info += gd_up_lines(G, o)                              # v17.0.3：不夠的層自動用最低金額
    info += ["", "取整後最少(照OKX單位往下取)"] + gd_round_lines(G, o)
    rows = [(a, rg_avail(a) if real else fd_avail(a)) for a in accts]
    vs = rn_pad([f"{v:.2f}U" if v is not None else "查不到" for _, v in rows])
    info += ["", "資金(OKX 可用)" if real else "資金(模擬從真的可用扣)"] + [f"{a} 可用 {x}" + (f"|約夠{int(v // amt)}層" if v is not None else "")
                                                                  for (a, v), x in zip(rows, vs)]
    return [], info

def gd_reject_lines(G, o, bad):
    """v17.0（1111 2026-10-19 畫面）② /runt 被擋：一次列出全部原因＋怎麼改。"""
    L = [f"{E.WARN} /{gd_M(G).cmd} {gd_M(G).tag}{gd_head(G)} {rt_p2(G['off'])}% 沒有開始"]
    if bad and bad[0][0].startswith("每層U 不夠"):
        L.append(f"(原點用 L00 {o} 算)" if G["has"]["L"] else f"(原點用現價 {o} 算)")
    L.append("")
    for i, (t, xs) in enumerate(bad, 1):
        L += [f"{i}.{t}"] + [f" {x}" for x in xs]
    return L + [""]

async def gd_spec_fresh(has):
    """v17.0：/runt 開始時重查 OKX 規格（數量單位、最小數量、每張幾顆），查不到用之前查到的。"""
    if has["S"] or has.get("D"):
        await okx_specs_all()
    if has["L"] or has["U"]:
        await okx_spot_specs_all()




# ==================== v19.0（1111 2026-10-23）：/run 實盤網格（/runt 同一套規則，掛單、SL/TP 送 OKX；成交、出場、損益查 OKX） ====================
# 1111：/runt 測好的模式複製到 /run，在 OKX 正式交易；掛單、設定 SL/TP、移動 SL/TP、成交、毛損益、手續費、淨損益全部以 OKX 為準。
# 跟 /runt 一模一樣（共用 gd_ 函式，一行規則都沒改）：格子、原點、3 張掛單、換格、帳戶分配（v20.0：每 10 層一個帳戶、5 個帳戶，現貨也是）、每層U、自動最低金額、
#   SL/TP 規則（rn_rule：門檻、距離、每秒次數、只往獲利方向）、改單額度（每 2 秒 20 次）、/stop 跑到出場、改每層U、畫面（多「實盤」兩個字）。
# 不一樣的（全部向 OKX 要）：
#   掛單：OKX post-only 限價單（現貨 tdMode cash、合約 tdMode isolated＋posSide）；數量＝gd_lsize（跟 /runt 一樣照 OKX 單位往下取）。
#         換格＝OKX 撤單 → OKX 確認撤掉（或發現已經成交）→ 下一拍才掛新的；post-only 被 OKX 取消 → 1 秒後照現價重算。
#         被拒：餘額不夠＝跟 /runt 一樣的「可用不夠」通知（有錢了自動接著掛）；其他原因＝通知，30 秒後再試。
#   成交：OKX 回報（每 0.5 秒對帳 OKX 掛單、條件單，每 1 秒對帳持倉；私有 WS 有動靜馬上對）。進場價＝OKX 成交均價、時間＝OKX 成交時間、
#         數量＝OKX 成交數量（部分成交後撤單＝照成交的部分算一單，金額照比例）；手續費＝OKX 實際（現貨從幣扣＝實得顆數照 OKX）。
#   SL/TP：每一單一張 OKX OCO 條件單（只平那一單的數量，最新成交價觸發、市價平倉；現貨＝賣那一單實得顆數往下取）；
#         第一次設＝下 OCO、移動＝改 OCO（amend）。OKX 接受才算數（設SL 時間、移動次數）。觸發價跟現價方向不對（送單途中價格走掉）→ 下一拍重算。
#         條件單被取消（不是 BOT 撤的）→ 照原價重設一次；第二次就不再設（通知，那一單 1111 自己管理）。
#   出場：OKX 條件單觸發、平倉單成交才算出場：出場價、時間、手續費＝OKX 平倉單；原因看 OKX（tp→設定TP／移動TP、sl→設定SL／移動SL）。
#         1111 在 OKX 手動平倉／OKX 強平／自動減倉：OKX 倉位（現貨＝OKX 餘額）比 BOT 記的少 → 查 OKX 成交紀錄，從最深一層（離原點最遠）往回算；
#         原因「手動平倉」「強平」「減倉(ADL)」；那一單的條件單撤掉。
#   槓桿、保證金（合約）：OKX 逐倉一個帳戶一個幣一邊只有一個倉位、一個槓桿 →
#         /run 開始時把那個帳戶那一邊的 OKX 槓桿設成「那個帳戶那一邊最高那一層」的槓桿（例 o1111o 空單 3.43X）；每一層照模擬的張數下單
#         （張數用那一層自己的槓桿算），成交後把那個倉位的保證金補到「還抱著的每一單的每層U 加起來（−開倉手續費＋資金費）」
#         ＝跟 /runt 的強平公式（rn_gliq）一樣 → OKX 的預估強平價＝設計的強平價。出場之後 OKX 照比例退保證金 → 再調回同一個算法（多的退回可用、少的補上）。
#   資金費：OKX 倉位的累計資金費每次變動，按張數分給那一刻還抱著的每一單（OKX 實際金額）。強平價：OKX 倉位的預估強平價（/status 🔴、出場累計 🔴）。
#   資金：OKX 真的可用（每 10 秒讀一次，下單、成交、撤單後馬上再讀）；不夠＝那一張不掛，通知一次，有錢了自動接著掛再通知。
#   開始前檢查：跟 /runt 一樣（帳戶模式、雙向持倉、那個幣沒有持倉／掛單／現貨、第一層下得了、可用）＋那個幣沒有條件單、逐倉保證金自動轉入；
#         都過了才設 OKX 槓桿、開始掛單。
#   /stop：撤掉所有掛單（OKX 確認撤掉）、不再掛新單；持倉照規則（SL/TP 照常移動）跑到出場，全部出場 → 策略結束、通知。BOT 永遠不平倉。
#   重開：存檔接著跑，以 OKX 為準對帳（關機期間的成交、出場、手動平倉都會補上、補通知）。
#   自訂單號：進場單 gr＋帳戶數字…、條件單 ga＋帳戶數字…（不碰手動單、不碰別的 bot 的單）。
#   紀錄：rungrid_{帳戶}.json（存檔）、rungridlog_{帳戶}.jsonl（每一張出場，/summary）、rungridevent_{帳戶}.jsonl（每一次送單、撤單、成交、
#         出場、改單、保證金、資金費），留 30 天。
RG_EV_FILE   = f"/srv/1111bot/data/rungridevent_{ACCT}.jsonl"
RG_SYNC_GAP  = 0.5      # 每 0.5 秒對帳 OKX 掛單、條件單（掛單 60 次/2 秒、條件單 20 次/2 秒）
RG_POS_GAP   = 1.0      # 每 1 秒對帳 OKX 持倉（10 次/2 秒，同一個帳戶別的 bot 也會查）
RG_SYNC_MIN  = 0.25     # 私有 WS 叫醒時，兩次對帳最少間隔
RG_AMB_SEC   = 10       # 送單沒有回應：10 秒內用自訂單號找，找不到當作沒送出
RG_CXL_AGAIN = 3        # 撤單 3 秒還在 → 再撤一次
RG_AID_WAIT  = 3        # 條件單剛送出 3 秒內不在 OKX 清單上不算數（清單有延遲）
RG_COOL      = 30       # 掛單被拒 → 那個位置 30 秒後再試
RG_PO_COOL   = 1.0      # post-only 被 OKX 取消 → 1 秒後照現價重算
RG_MIS_CONF  = 1.0      # OKX 倉位跟 BOT 對不上要持續 1 秒（兩次對帳）才去找手動平倉／強平
RG_MIS_SEC   = 20       # 對不上超過 20 秒 → 通知
RG_BAL_GAP   = 10       # 每 10 秒讀一次 OKX 可用（下單、成交、撤單後馬上再讀）
RG_SPOT_GAP  = 3        # 每 3 秒對帳一次 OKX 現貨餘額（手動賣掉）
RG_MG_TOL    = Decimal("0.02")    # 保證金差 0.02U 以內不調
RG_MG_PCT    = Decimal("0.005")   #           或差 0.5% 以內不調
RG_MG_GAP    = 1.0      # 同一個倉位調保證金最少隔 1 秒（而且一定用調整之後才查到的 OKX 數字）
RG_ACX_WAIT  = 3.0      # 條件單不是 BOT 撤的：合約先等 3 秒（多半是倉位被平掉／強平，OKX 自己撤的 → 對帳會先處理）才算「被取消」
RG_ACX_WAIT_SP = 8.0    #                    現貨等 8 秒（現貨餘額 3 秒對帳一次、要連續兩次）
RG_SOFT = {"51277", "51278", "51279", "51280", "51281", "51282", "51283"}   # 觸發價跟現價方向不對 → 下一拍重算
RG_FUNDC = {"51008", "51119", "51127", "51131"}                               # 餘額、保證金不夠
RG_WHY_OTHER = {"full_liquidation": "強平", "partial_liquidation": "強平", "adl": "減倉(ADL)"}
RG_DIG  = (re.sub(r"\D", "", ACCT) or "0")[0]
RG_PFX  = f"gr{RG_DIG}"            # /run 進場單的自訂單號開頭
RG_APFX = f"ga{RG_DIG}"            # /run 條件單的自訂單號開頭
RG_SYNC = {"t": 0.0, "ok": None, "err": 0, "warn_t": 0.0, "pos_t": {}}
RG_HOLD = {"until": 0.0}           # OKX 回速率超限 50011 → 條件單請求暫停 2 秒
RG_APP  = {"app": None}
RG_WS   = {}                       # 帳戶 → 私有 WS 連著沒
RG_WST  = {}                       # 帳戶 → 私有 WS 的背景工作
RG_FDQ  = {}                       # 帳戶 → 要馬上讀 OKX 可用（時間）
RG_WAKE = None
WS_PRI_URL = "wss://ws.okx.com:8443/ws/v5/private"
RG_PKEEP = ("real", "acct", "oid", "sz", "fee_in", "fee_b", "ssz", "aid", "aid_x", "n_acx", "manual", "part", "xoid", "acl_wait", "t_aw")
RG_OKEEP = ("kd", "acct", "sz", "amt", "up", "lev", "lu", "clid", "oid", "cxl", "t_s", "drop", "keep")


def rg_wake():
    global RG_WAKE
    if RG_WAKE is None:
        RG_WAKE = asyncio.Event()
    return RG_WAKE

def rg_D(v):
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal(0)
    except Exception:
        return Decimal(0)

def rg_ms(v):
    """OKX 的毫秒時間 → 秒；沒有就 None。"""
    try:
        x = int(v)
        return x / 1000 if x > 0 else None
    except Exception:
        return None

def rg_d0(r):
    d = r.get("data")
    return d[0] if isinstance(d, list) and d and isinstance(d[0], dict) else {}

def rg_sc(r):
    """OKX 回應的結果碼：單筆的 sCode 優先，沒有就用整體 code。"""
    d0 = rg_d0(r)
    return str(d0.get("sCode") if d0.get("sCode") not in (None, "") else r.get("code"))

def rg_msg(r):
    return str(rg_d0(r).get("sMsg") or r.get("msg") or "").strip()

def rg_net(r):
    """網路／連線沒有回應（可能已經送到 OKX）：api() 例外時回 code -1（沒有 API Key 也是 -1，那個不算）。"""
    return str(r.get("code")) == "-1" and "API Key" not in str(r.get("msg") or "")

def rg_s(v):
    """數字給 OKX 的寫法（不用科學記號）。"""
    d = Decimal(str(v))
    return f"{d.normalize():f}" if d != 0 else "0"

def rg_ps(side):
    return "long" if side == "L" else "short"

def rg_pkey(acct, side):
    """合約倉位：帳戶|long／short（S 做空＝short、D 做多＝long）。"""
    return f"{acct}|{rg_ps(side)}"

def rg_clid(pfx):
    return f"{pfx}{uuid.uuid4().hex[:24]}"

def rg_ev(k, G=None, **kw):
    """實盤事件紀錄（rungridevent_{ACCT}.jsonl，留 30 天）：每一次送單、撤單、成交、出場、改單、保證金、資金費、對帳差異一行。"""
    try:
        r = {"d": rn_today(), "t": round(time.time(), 3), "k": k}
        if G is not None:
            r["sym"] = G["sym"]
        for a, b in kw.items():
            r[a] = str(b) if isinstance(b, Decimal) else b
        with open(RG_EV_FILE, "a") as f:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print("[run] event log fail", e)

def rg_say(G, txt):
    app = RG_APP.get("app")
    if app is not None and G.get("chat"):
        rt_chain(G, lambda t=txt: rt_send(app, G["chat"], t))

def rg_warn(G, key, txt, every=600):
    """要 1111 知道的問題（被拒、取消、對不上）：同一種 10 分鐘內只通知一次；事件紀錄每次都記。"""
    print(f"[run] 警告 {G['sym']} {txt}")
    rg_ev("warn", G, key=key, msg=txt)
    w = G.setdefault("warns", {})
    now = time.time()
    if now - w.get(key, 0) < every:
        return
    w[key] = now
    rg_say(G, f"{E.WARN} {GREAL.tag}{G['sym']}\n{txt}\n時間:{hhmmss()}")

def rg_init(G):
    """一組 /run 的實盤欄位。"""
    G["real"] = True
    for k, v in (("cool", {}), ("okpos", {}), ("ff0", {}), ("mg", {}), ("mis", {}), ("seen", []), ("acx", []), ("warns", {}),
                 ("lev_set", {}), ("okspot", {}), ("spmis", {})):   # v20.0：okspot、spmis＝每個帳戶一份
        if G.get(k) is None:
            G[k] = v
    return G

def rg_accts():
    """/run 用到的帳戶（OKX 可用、私有 WS）：有 /run 的組＝o1111o～o5555o（v20.0：現貨、合約都分 5 個帳戶）。"""
    return list(GD_XACCT) if any(not G.get("done") for G in RD.values()) else []

def rg_avail(acct):
    v = (FD.get(acct) or {}).get("avail")
    return v

def rg_fd_fresh(acct, sec=3.0):
    return time.time() - (FD.get(acct) or {}).get("t", 0) <= sec

def rg_fdq(acct):
    """下單、成交、撤單、出場、調保證金之後：馬上再讀一次 OKX 可用（rg_bal_task，1 秒內合併）。"""
    RG_FDQ.setdefault(acct, time.time())

def rg_fd_lines():
    """/status 最上面（實盤）：資金(OKX) 每個帳戶 可用|佔用（OKX 的 availBal、frozenBal）。"""
    rows = [(a, (FD.get(a) or {}).get("avail"), (FD.get(a) or {}).get("frozen")) for a in rg_accts()]
    av = rn_pad([f"{v:.2f}U" if v is not None else "查詢中" for _, v, _ in rows])
    fz = rn_pad([f"{v:.2f}U" if v is not None else "查詢中" for _, _, v in rows])
    return ["資金(OKX)"] + [f"{a} 可用 {x}|佔用 {y}" + ("🚨" + FD[a]["bad"] if fd_halt(a) else "") for (a, _, _), x, y in zip(rows, av, fz)]

def rg_avg_okx(G, kd, grp, b):
    """/status 官方均價（實盤）：合約＝OKX 倉位均價、未實現；現貨＝OKX 平均成本、未實現。沒有＝None（改用 BOT 算）。"""
    if kd == "sw":
        x = (G.get("okpos") or {}).get(rg_pkey(GD_XACCT[b] if b is not None else GD_XACCT[0], "S" if grp == "S" else "L")) or {}
        if x.get("sz") and x.get("avg"):
            return x["avg"], x.get("upl") or Decimal(0)
        return None
    x = (G.get("okspot") or {}).get(GD_XACCT[b] if b is not None else GD_XACCT[0]) or {}   # v20.0：每個帳戶的現貨
    if x.get("avg"):
        return x["avg"], x.get("upl") or Decimal(0)
    return None

def rg_pdump(p):
    """存檔：一單的實盤欄位。"""
    s = lambda v: v if isinstance(v, (int, float, bool)) or v is None else str(v)
    out = {k: s(p.get(k)) for k in RG_PKEEP if p.get(k) is not None}
    if p.get("sent"):
        out["sent"] = [None if x is None else str(x) for x in p["sent"]]
    return out

def rg_odump(o):
    s = lambda v: v if isinstance(v, (int, float, bool)) or v is None else str(v)
    return {k: s(o.get(k)) for k in RG_OKEEP if o.get(k) is not None}

def rg_pload(p, y):
    """重開：一單的實盤欄位讀回來（條件單、OKX 單號）；之後 rg_sync 以 OKX 為準對帳。"""
    D_ = lambda v: None if v in (None, "None") else Decimal(v)
    p.update(real=True, acct=y.get("acct") or gd_kacct(p["kind"], p["k"]), oid=y.get("oid"), sz=D_(y.get("sz")), fee_in=D_(y.get("fee_in")) or Decimal(0),
             fee_b=D_(y.get("fee_b")), ssz=D_(y.get("ssz")), aid=y.get("aid"), aid_x=y.get("aid_x"), t_aid=0.0,
             n_acx=int(y.get("n_acx") or 0), manual=bool(y.get("manual")), part=bool(y.get("part")), xoid=y.get("xoid"),
             acl_wait=y.get("acl_wait"), t_aw=float(y.get("t_aw") or 0) or time.time(), want=None, abusy=False, x_pend=False)
    if y.get("sent"):
        p["sent"] = tuple(D_(x) for x in y["sent"])
    elif p.get("acl_wait"):
        p["acl_wait"] = None
    if p["sz"] is None:
        p["sz"] = p.get("ct") if p["kind"] == "sw" else p["qty"]

def rg_oload(o, slot=True):
    """重開：一張進場單讀回來。掛單位置（slot）那一張根本沒送出去（沒有自訂單號、沒有單號）→ 不要（下一拍照現價重算）。"""
    if slot and not o.get("clid") and not o.get("oid"):
        return None
    D_ = lambda v: None if v in (None, "None") else Decimal(v)
    x = {"k": int(o.get("k", 0)), "px": Decimal(o["px"]), "t": float(o["t"]), "kd": o.get("kd") or "sp", "acct": o.get("acct") or gd_kacct(o.get("kd") or "sp", int(o.get("k", 0))),
         "sz": D_(o.get("sz")), "amt": D_(o.get("amt")), "up": bool(o.get("up")), "lev": D_(o.get("lev")) or Decimal(1), "lu": D_(o.get("lu")),
         "clid": o.get("clid"), "oid": o.get("oid"), "busy": False, "cxl": bool(o.get("cxl")), "cxl_t": 0.0,
         "t_s": float(o.get("t_s") or 0) or time.time(), "amb": bool(o.get("clid") and not o.get("oid")), "acc": Decimal(0),
         "drop": bool(o.get("drop")), "keep": bool(o.get("keep"))}
    return x


# ---------- 掛單、撤單 ----------
def rg_onew(G, kd, k, px, now):
    """一張進場單（還沒送）：數量、金額照 /runt（gd_lsize）。"""
    z = gd_lsize(G, kd, k, px)
    return {"k": k, "px": px, "t": now, "kd": kd, "acct": gd_kacct(kd, k), "sz": z["q"] if kd == "sp" else z["ct"],
            "amt": z["amt"], "up": bool(z["up"]), "lev": z["lev"], "lu": G["amt"], "clid": None, "oid": None, "busy": False,
            "cxl": False, "cxl_t": 0.0, "t_s": 0.0, "amb": False, "acc": Decimal(0), "drop": False, "keep": False}

def rg_body(G, o):
    x = G[o["kd"]]
    px = rg_s(gd_q(G, o["kd"], o["px"]))
    if o["kd"] == "sp":
        return {"instId": x["iid"], "tdMode": "cash", "side": "buy", "ordType": "post_only", "px": px, "sz": rg_s(o["sz"]), "clOrdId": o["clid"]}
    side = "L" if o["k"] < 0 else "S"
    return {"instId": x["iid"], "tdMode": "isolated", "side": "buy" if side == "L" else "sell", "posSide": rg_ps(side),
            "ordType": "post_only", "px": px, "sz": rg_s(o["sz"]), "clOrdId": o["clid"]}

async def rg_place(G, o):
    """送一張進場單（post-only 限價）到 OKX。回傳 (送出去了沒有, 失敗原因, sCode)。網路沒回應＝可能送到了 → amb，rg_sync 用自訂單號回查。"""
    if SHUTTING_DOWN:
        o["clid"] = None
        return False, "bot 關閉中", "STOP"
    o["clid"] = o.get("clid") or rg_clid(RG_PFX)
    o["t_s"], o["amb"], o["oid"], o["acc"] = time.time(), False, None, Decimal(0)
    r = await api("POST", "/api/v5/trade/order", rg_body(G, o), acct=o["acct"])
    d0, sc = rg_d0(r), rg_sc(r)
    rg_ev("place", G, nm=gd_nm(o["kd"], o["k"]), acct=o["acct"], px=o["px"], sz=o["sz"], clid=o["clid"], code=r.get("code"), sCode=sc,
          sMsg=rg_msg(r), oid=d0.get("ordId"))
    if str(r.get("code")) == "0" and sc == "0" and d0.get("ordId"):
        o["oid"] = d0["ordId"]
        print(f"[run] 掛單 {G['sym']} {gd_nm(o['kd'], o['k'])} {o['px']}×{o['sz']} {o['acct']} ordId={o['oid']}")
        return True, "", "0"
    if rg_net(r):
        o["amb"] = True
        print(f"[run] 掛單沒有回應 {G['sym']} {gd_nm(o['kd'], o['k'])} clOrdId={o['clid']} → 用自訂單號回查")
        return True, "", "-1"
    o["clid"] = None
    if sc == "50011":
        RG_HOLD["until"] = time.time() + 2
    return False, f"sCode={sc} {rg_msg(r)}".strip(), sc

def rg_go_place(G, slot, o):
    """背景送單；被拒 → 那個位置拿掉（L00 留著等重掛）、冷卻、通知（餘額不夠＝可用不夠的通知）。
    自訂單號先給（存檔裡就有）→ bot 在送單途中重開，也找得回這張單（rg_entry_state 用自訂單號回查）。"""
    o["busy"], o["clid"], o["t_s"], o["oid"] = True, rg_clid(RG_PFX), time.time(), None

    async def go():
        try:
            ok, why, sc = await rg_place(G, o)
        except Exception as e:
            ok, why, sc = False, f"{type(e).__name__}: {e}", "EXC"
        o["busy"] = False
        now = time.time()
        if ok:
            if o.get("cxl"):
                rg_cancel(G, o, force=True)                # 送單途中要撤 → 拿到單號馬上撤
            rg_fdq(o["acct"])
            rg_wake().set()
        else:
            if slot == "o00":
                if G.get("o00") is o:
                    o.update(clid=None, oid=None, amb=False, cxl=False)
                    if o.get("drop"):
                        G["o00"] = None
            elif G["ord"].get(slot) is o:
                G["ord"][slot] = None
            soft = "post" in why.lower()                   # post-only 會吃單 → OKX 拒絕：跟 /runt 一樣下一拍重算，不通知
            G["cool"][slot] = now + (RG_PO_COOL if soft else RG_COOL)
            nm = gd_nm(o["kd"], o["k"])
            if soft:
                G["n_po"] = G.get("n_po", 0) + 1
            elif sc in RG_FUNDC or "insufficient" in why.lower():
                rg_fdq(o["acct"])
                s = "sp" if slot == "o00" else slot
                G["short"].pop(s, None)
                gd_short(G, s, o["kd"], o["k"], o["acct"], rg_avail(o["acct"]) or Decimal(0), gd_pamt(G, o) if o["kd"] == "sw" else o["sz"] * o["px"])
            elif sc != "STOP":
                rg_warn(G, f"rej{slot}", f"{nm}{gd_atag(o['kd'], o['k'])} 掛單被 OKX 拒絕 {why}\n{RG_COOL} 秒後再試")
        gd_save_soon()
        G["ev"].set()
    rt_bg(go())

def rg_cancel(G, o, force=False):
    """撤一張還沒成交的進場單。OKX 確認撤掉了（rg_sync 查到）才從清單拿掉；撤單時剛好成交＝變成持倉，照常管理。"""
    now = time.time()
    if not force and o.get("cxl") and now - (o.get("cxl_t") or 0) < RG_CXL_AGAIN:
        return
    o["cxl"], o["cxl_t"] = True, now
    if not o.get("oid"):
        return                                             # 還沒拿到單號 → 送單回來（或 rg_sync 找到）後再撤
    oid, acct, iid = o["oid"], o["acct"], G[o["kd"]]["iid"]

    async def go():
        r = await api("POST", "/api/v5/trade/cancel-order", {"instId": iid, "ordId": oid}, acct=acct)
        rg_ev("cancel", G, nm=gd_nm(o["kd"], o["k"]), acct=acct, oid=oid, code=r.get("code"), sCode=rg_sc(r), sMsg=rg_msg(r))
        rg_wake().set()
    rt_bg(go())

def rg_algo_cancel(G, acct, iid, aid):
    G.setdefault("acx", []).append(aid)
    del G["acx"][:-200]

    async def go():
        r = await api("POST", "/api/v5/trade/cancel-algos", [{"instId": iid, "algoId": aid}], acct=acct)
        rg_ev("algo_cancel", G, acct=acct, aid=aid, code=r.get("code"), sCode=rg_sc(r), sMsg=rg_msg(r))
    rt_bg(go())


# ---------- 每 0.25 秒：現價、最高／最低毛利率（rg_touch）、SL/TP（rg_rules）、掛單（rg_hang） ----------
def rg_touch(G, kd, px, t):
    """實盤的逐筆成交價：只記現價、每一單的最高／最低毛利率（成交、出場、強平都看 OKX）。"""
    G["px"][kd] = px
    for p in G["pos"]:
        if p["kind"] != kd or p["out"]:
            continue
        gg = rn_g(p["side"], p["ent"], px)
        if gg > p["mfe"]:
            p["mfe"] = gg
        if gg < p["mae"]:
            p["mae"] = gg

def rg_onokx(o):
    """這張單在 OKX 上（或正在送）。"""
    return bool(o.get("oid") or o.get("busy") or o.get("clid"))

def rg_hang(G, now):
    """掛單（每 0.25 秒）：跟 /runt 的 gd_hang 同一套（3 個位置、現價上下最近一格沒有持倉的、換格），差別：
    換格＝送撤單，OKX 確認撤掉了（rg_sync）下一拍才掛新的；可用＝OKX 真的可用；被拒、post-only 被取消 → 冷卻後重算。"""
    o = G.get("o00")
    if o is not None:
        a0 = o["acct"]                                     # v20.0：L00 在第一個帳戶（o1111o）
        if rg_onokx(o):
            if (G.get("stop") or fd_halt(a0)) and not o.get("cxl"):
                o["drop"] = bool(G.get("stop"))
                rg_cancel(G, o)
            elif (o.get("acc") or 0) > 0 and not o.get("cxl") and (G["px"].get("sp") or 0) > o["px"]:
                rg_cancel(G, o)                            # 部分成交、價格又回到上面 → 剩下的撤掉，成交的部分＝L00（原點）
        elif G.get("stop"):
            G["o00"] = None
            gd_save_soon()
        else:
            px = G["px"].get("sp")
            if px is not None and px > o["px"] and not fd_halt(a0) and now >= G["cool"].get("o00", 0) and not SHUTTING_DOWN:
                need = o["sz"] * o["px"]
                av = rg_avail(a0)
                if av is not None and av < need:
                    if rg_fd_fresh(a0):
                        gd_short(G, "sp", "sp", 0, a0, av, need)
                    else:
                        rg_fdq(a0)
                else:
                    gd_short_ok(G, "sp", "sp", 0, o["px"], a0, None if av is None else av - need)
                    z = rg_onew(G, "sp", 0, o["px"], o["t"])      # 照現在的每層U（改過每層U 也對）
                    o.update(sz=z["sz"], amt=z["amt"], up=z["up"], lev=z["lev"], lu=z["lu"], cxl=False, keep=False)
                    rg_go_place(G, "o00", o)
                    gd_save_soon()
    if G["org"] is None:
        return
    if G.get("stop"):                                      # 停止中：掛單撤掉（OKX 確認了 rg_sync 清掉），不再掛新單
        for cur in G["ord"].values():
            if cur and not cur.get("cxl"):
                cur["drop"] = True
                rg_cancel(G, cur)
        return
    for s in ("sp", "S", "D"):
        kd = gd_skd(s)
        if not G.get(kd):
            continue
        px = G["px"].get(kd)
        cur = G["ord"].get(s)
        if px is None or (cur and cur.get("cxl")):
            continue                                       # 撤單中：等 OKX 確認
        if cur is not None and fd_halt(cur["acct"]):
            rg_cancel(G, cur)                              # 帳戶模式被改 → 那個帳戶的掛單撤掉
            continue
        if cur is not None and (cur.get("acc") or 0) > 0 and ((px > cur["px"]) if s != "S" else (px < cur["px"])):
            rg_cancel(G, cur)                              # 部分成交、價格又離開掛單價 → 剩下的撤掉，成交的部分＝一單（照比例），照常管理
            continue
        held = {p["k"] for p in G["pos"] if p["kind"] == kd and not p["out"]}
        buy = s != "S"
        best = None
        for k in gd_rng(G, s):
            if k in held:
                continue
            lp = gd_lp(G, kd, k)
            if buy and lp < px and (best is None or k > best[0]):
                best = (k, lp)
            elif not buy and lp > px and (best is None or k < best[0]):
                best = (k, lp)
        if best is not None and cur is not None and cur["k"] == best[0]:
            continue
        if best is None or cur is not None or fd_halt(gd_kacct(kd, best[0])):
            if cur is not None:
                rg_cancel(G, cur)                          # 換格／不用掛了：先撤，OKX 確認了下一拍才掛新的
                gd_save_soon()
            continue
        if now < G["cool"].get(s, 0) or SHUTTING_DOWN:
            continue
        acct = gd_kacct(kd, best[0])
        need = gd_need(G, kd, best[0], best[1])
        av = rg_avail(acct)
        if av is not None and av < need:
            if rg_fd_fresh(acct):
                gd_short(G, s, kd, best[0], acct, av, need)
            else:
                rg_fdq(acct)                               # OKX 可用是舊的 → 先再讀一次，夠了就掛、還是不夠才通知
            continue
        gd_short_ok(G, s, kd, best[0], best[1], acct, None if av is None else av - need)
        o = rg_onew(G, kd, best[0], best[1], now)
        G["ord"][s] = o
        rg_go_place(G, s, o)
        gd_save_soon()

def rg_rules(G, now):
    """每一單的 SL/TP（跟 /runt 同一個 rn_rule、同一套額度：每 2 秒 20 次，這個帳戶的現貨一份、每個帳戶的永續一份），算好送 OKX：
    還沒有條件單＝下一張 OCO、有＝改觸發價。送出去還沒回來不再送；額度用完 → 離現價最近的先送，其他下一拍。"""
    by = {}
    for p in G["pos"]:
        if p["out"] or p.get("manual"):
            continue
        px = G["px"].get(p["kind"])
        if px is not None:
            by.setdefault(f"sp:{p['acct']}" if p["kind"] == "sp" else p["acct"], []).append((p, px))   # v20.0：每個帳戶的現貨一份
    for a, ps in by.items():
        q = G.setdefault("amd", {}).setdefault(a, deque())
        while q and now - q[0] >= GD_AMEND_WIN:
            q.popleft()
        ps.sort(key=lambda x: abs(x[1] - (x[0]["sl"] or x[0]["ent"])))
        for p, px in ps:
            if (p.get("abusy") or p.get("acl_wait") or p.get("x_pend") or p.get("acx_pend") or now < p.get("t_amd", 0)
                    or now < RG_HOLD["until"]):
                continue
            snap = {k: p.get(k) for k in ("tp_t", "sl_t", "want", "hi")}
            rn_rule({"tick": G[p["kind"]]["tick"], "real": True}, p, px, now, GD_LONG_ARM if p["side"] == "L" else RN_ARM)
            w = p.get("want")
            if not w:
                continue
            if len(q) >= GD_AMEND_N:
                p.update(snap)                             # 額度用完：這一拍不送
                G["n_amd"] = G.get("n_amd", 0) + 1
                continue
            q.append(now)
            p["abusy"], p["t_amd"] = True, now
            rt_bg(rg_algo_amend(G, p, w) if p.get("aid") else rg_algo_place(G, p, w))


# ---------- 條件單（每一單一張 OCO） ----------
def rg_algo_ok(p, sl, tp):
    """OKX 接受了新的 SL/TP → 才算數：設SL 時間、SL／TP 移動次數（算法跟 /runt 的 rn_rule 一樣）。"""
    if p["sl"] is None:
        p["t_arm"] = time.time()
        p["arm_px"] = sl
    elif sl != p["sl"]:
        p["n_mv"] += 1
    if p["tp"] is not None and tp != p["tp"]:
        p["n_tp"] = p.get("n_tp", 0) + 1
    p["sl"], p["tp"] = sl, tp
    p["sl_g"] = rn_g(p["side"], p["ent"], sl)
    p["tp_g"] = None if tp is None else rn_g(p["side"], p["ent"], tp)
    if p.get("want") == (sl, tp):
        p["want"] = None

async def rg_algo_place(G, p, want):
    """這一單第一次設 SL/TP（或條件單被取消後重設）：一張 OCO（只平這一單的數量）。"""
    try:
        sl, tp = want
        kd = p["kind"]
        x = G[kd]
        acl = rg_clid(RG_APFX)
        if kd == "sp":
            sz = gd_fl(p.get("got") if p.get("got") is not None else p["qty"], x.get("lot") or GD_LOT0)
            p["ssz"] = sz
            body = {"instId": x["iid"], "tdMode": "cash", "side": "sell", "ordType": "oco", "sz": rg_s(sz), "algoClOrdId": acl}
        else:
            sz = p["sz"]
        if sz <= 0 or sz < (x.get("minsz") or 0):                    # 數量低於 OKX 最小單位（部分成交、手動平掉一部分剩下的）→ 條件單下不了
            p["manual"], p["want"] = True, None
            rg_warn(G, f"tiny{gd_nm(kd, p['k'])}", f"{gd_nm(kd, p['k'])}{gd_atag(kd, p['k'])} 數量 {pct(sz)} 低於 OKX 最小 {pct(x.get('minsz') or 0)},"
                    "條件單下不了,BOT 不幫這一單設 SL/TP,請自己處理", every=1)
            return
        if kd == "sw":
            body = {"instId": x["iid"], "tdMode": "isolated", "side": "sell" if p["side"] == "L" else "buy", "posSide": rg_ps(p["side"]),
                    "ordType": "oco", "sz": rg_s(p["sz"]), "algoClOrdId": acl}
        body.update(slTriggerPx=rg_s(sl), slOrdPx="-1", slTriggerPxType="last", tpTriggerPx=rg_s(tp), tpOrdPx="-1", tpTriggerPxType="last")
        r = await api("POST", "/api/v5/trade/order-algo", body, acct=p["acct"])
        d0, sc = rg_d0(r), rg_sc(r)
        rg_ev("algo_place", G, nm=gd_nm(kd, p["k"]), acct=p["acct"], sl=sl, tp=tp, sz=body["sz"], acl=acl, code=r.get("code"), sCode=sc,
              sMsg=rg_msg(r), aid=d0.get("algoId"))
        if str(r.get("code")) == "0" and sc == "0" and d0.get("algoId"):
            if p["out"]:                                   # 等回應的時候這一單已經出場 → 撤掉
                rg_algo_cancel(G, p["acct"], x["iid"], d0["algoId"])
                return
            p["aid"], p["t_aid"] = d0["algoId"], time.time()
            rg_algo_ok(p, sl, tp)
            print(f"[run] 設 SL/TP {G['sym']} {gd_nm(kd, p['k'])} SL {sl} TP {tp} algoId={p['aid']}")
        elif rg_net(r):
            p["acl_wait"], p["t_aw"], p["sent"] = acl, time.time(), (sl, tp)
        elif sc in RG_SOFT:
            if p.get("want") == want:
                p["want"] = None
            p["t_amd"] = time.time() + 0.25
        else:
            if sc == "50011":
                RG_HOLD["until"] = time.time() + 2
            p["t_amd"] = time.time() + 2
            rg_warn(G, f"algo{sc}", f"{gd_nm(kd, p['k'])}{gd_atag(kd, p['k'])} 設 SL/TP 被 OKX 拒絕 sCode={sc} {rg_msg(r)}".strip()
                    + "\n這一單暫時沒有 SL/TP,BOT 會繼續重試")
    except Exception as e:
        print("[run] algo place fail", G["sym"], type(e).__name__, e)
        p["t_amd"] = time.time() + 2
    finally:
        p["abusy"] = False
        gd_save_soon()

async def rg_algo_amend(G, p, want):
    """SL/TP 移動：改 OKX 條件單的觸發價（只改有變的那一邊）。"""
    try:
        sl, tp = want
        x = G[p["kind"]]
        aid = p["aid"]
        body = {"instId": x["iid"], "algoId": aid}
        if sl != p["sl"]:
            body.update(newSlTriggerPx=rg_s(sl), newSlOrdPx="-1", newSlTriggerPxType="last")
        if tp != p["tp"]:
            body.update(newTpTriggerPx=rg_s(tp), newTpOrdPx="-1", newTpTriggerPxType="last")
        if len(body) == 2:
            if p.get("want") == want:
                p["want"] = None
            return
        r = await api("POST", "/api/v5/trade/amend-algos", body, acct=p["acct"])
        sc = rg_sc(r)
        rg_ev("algo_amend", G, nm=gd_nm(p["kind"], p["k"]), acct=p["acct"], aid=aid, sl=sl, tp=tp, sCode=sc, sMsg=rg_msg(r))
        if p["out"] or p.get("aid") != aid:
            return
        if sc == "0":
            rg_algo_ok(p, sl, tp)
            p["n_amf"] = 0
        elif sc in RG_SOFT:
            if p.get("want") == want:
                p["want"] = None
            p["t_amd"] = time.time() + 0.25
        else:
            if sc == "50011":
                RG_HOLD["until"] = time.time() + 2
            p["t_amd"] = time.time() + 2
            p["t_aid"] = 0                                 # 可能已經觸發了 → 下一次對帳馬上查這張條件單
            p["n_amf"] = p.get("n_amf", 0) + 1
            print(f"[run] 改 SL/TP 失敗 {G['sym']} {gd_nm(p['kind'], p['k'])} sCode={sc} {rg_msg(r)}")
            if p["n_amf"] >= 3 and sc not in ("50011", "51527"):   # 連續 3 次被拒（不是太快、不是已經觸發）→ 通知（10 分鐘一次）
                rg_warn(G, f"amf{sc}", f"{gd_nm(p['kind'], p['k'])}{gd_atag(p['kind'], p['k'])} 移動 SL/TP 被 OKX 拒絕 sCode={sc} {rg_msg(r)}".strip()
                        + "\nOKX 上的 SL/TP 留在上一次的位置,BOT 繼續重試")
    except Exception as e:
        print("[run] algo amend fail", G["sym"], type(e).__name__, e)
        p["t_amd"] = time.time() + 2
    finally:
        p["abusy"] = False


# ---------- 對帳（OKX 為準） ----------
def rg_pos_new(G, o, od):
    """OKX 進場單成交（全部，或撤單時已部分成交）→ 一單持倉：進場價＝OKX 成交均價、數量＝OKX 成交數量、手續費＝OKX 實際。"""
    kd, k = o["kd"], o["k"]
    ent = rg_D(od.get("avgPx")) or o["px"]
    acc = rg_D(od.get("accFillSz"))
    t_in = rg_ms(od.get("fillTime")) or rg_ms(od.get("uTime")) or time.time()
    side = "L" if gd_buy(kd, k) else "S"
    pxn = G["px"].get(kd) or ent
    g0 = rn_g(side, ent, pxn)
    fee = -rg_D(od.get("fee"))                             # OKX：負＝付手續費
    fccy = (od.get("feeCcy") or "").upper()
    p = {"kind": kd, "side": side, "k": k, "ent": ent, "t_in": t_in, "t_ord": o["t"], "lev": o.get("lev") or Decimal(1),
         "sl": None, "tp": None, "sl_g": None, "tp_g": None, "pend": None,
         "t_arm": None, "arm_px": None, "n_mv": 0, "n_tp": 0, "hi": None, "tp_t": 0.0, "sl_t": 0.0, "fu": Decimal(0), "fn": 0,
         "mfe": max(g0, Decimal(0)), "mae": min(g0, Decimal(0)), "told": False, "out": None,
         "real": True, "acct": o["acct"], "oid": od.get("ordId"), "sz": acc, "aid": None, "want": None, "abusy": False}
    if kd == "sp":
        base = G["sym"][:-4].upper()
        if fccy in ("", base):                             # OKX 現貨買進：手續費從買到的幣扣
            p.update(qty=acc, got=acc - fee, fee_b=fee, fee_in=fee * ent, ct=None)
        else:                                              # 帳戶設成用 USDT 付手續費：拿到的幣＝買的量
            p.update(qty=acc, got=acc, fee_b=Decimal(0), fee_in=fee, ct=None)
    else:
        p.update(qty=acc * (G["sw"].get("ctval") or Decimal(1)), ct=acc, fee_in=fee)
    p["lu"] = o.get("lu") or G["amt"]
    full = o["amt"] if o.get("up") else p["lu"]
    if o.get("sz") and acc < o["sz"]:
        p["amt"], p["part"] = full * acc / o["sz"], True   # 部分成交：金額照成交的比例
    elif o.get("up"):
        p["amt"] = full
    if o.get("up"):
        p["up"] = True
    rg_ev("fill", G, nm=gd_nm(kd, k), acct=o["acct"], oid=od.get("ordId"), ent=ent, sz=acc, fee=od.get("fee"), feeCcy=fccy, t_in=t_in,
          state=od.get("state"))
    rg_gen(G, p)
    print(f"[run] 成交 {G['sym']} {gd_nm(kd, k)} {ent}×{acc} 手續費 {od.get('fee')} {fccy}")
    return p

def rg_gen(G, p):
    """合約倉位有成交、出場 → 這個倉位的版本 +1（調保證金的「OKX 沒有變」只在同一個版本裡判斷）。"""
    if p["kind"] == "sw":
        k = rg_pkey(p["acct"], p["side"])
        g = G.setdefault("gen", {})
        g[k] = g.get(k, 0) + 1

def rg_close(G, p, od, why, trig=None, t_trig=None, part=None, cancel_algo=False, sold=None):
    """一單出場（OKX 平倉單成交）：出場價＝OKX 成交均價、時間＝OKX 成交時間、手續費＝那張平倉單的（part＝這一單占的比例）。"""
    f = part if part is not None else Decimal(1)
    xpx = rg_D(od.get("avgPx")) or rg_D(od.get("px"))
    t_out = rg_ms(od.get("fillTime")) or rg_ms(od.get("uTime")) or time.time()
    if cancel_algo and p.get("aid"):
        rg_algo_cancel(G, p["acct"], G[p["kind"]]["iid"], p["aid"])
    fee = -rg_D(od.get("fee")) * f
    if p["kind"] == "sp" and (od.get("feeCcy") or "").upper() == G["sym"][:-4].upper():
        fee = fee * xpx                                    # 手續費是幣 → 換成 U
    if p["kind"] == "sp":
        p["sold"] = sold if sold is not None else rg_D(od.get("accFillSz")) * f
    p.update(fee_out=fee, xoid=od.get("ordId"), trig=trig, t_trig=t_trig, aid_x=p.get("aid") or p.get("aid_x"), aid=None, want=None,
             x_pend=False, out=(t_out, xpx, why))
    if od.get("ordId") and od["ordId"] not in G.setdefault("seen", []):
        G["seen"].append(od["ordId"])
        del G["seen"][:-300]
    rg_gen(G, p)
    rg_ev("exit", G, nm=gd_nm(p["kind"], p["k"]), acct=p["acct"], why=why, xoid=od.get("ordId"), px=xpx, sz=p.get("sz"), fee=fee,
          trig=trig, t_out=t_out, category=od.get("category"), sold=p.get("sold"))
    print(f"[run] 出場 {G['sym']} {gd_nm(p['kind'], p['k'])} {why} @{xpx}")
    rg_fdq(p["acct"])
    G["ev"].set()

def rg_split(G, p, take):
    """一單只平掉一部分（OKX 部分成交、手動平掉一部分）：平掉的那部分變成另一筆（照比例分金額、手續費、資金費），
    剩下的照常管理（條件單張數不對了 → 撤掉，用同樣的 SL/TP 重設剩下的數量）。合約 take＝張數、現貨 take＝顆數（實得）。"""
    base = p["sz"] if p["kind"] == "sw" else (p.get("got") or p["qty"])
    f = take / base
    amt = gd_pamt(G, p)
    q = dict(p)
    q.update(qty=p["qty"] * f, fee_in=(p.get("fee_in") or Decimal(0)) * f, fu=p.get("fu", Decimal(0)) * f, amt=amt * f, part=True,
             aid=None, want=None, abusy=False)
    p.update(qty=p["qty"] - q["qty"], fee_in=(p.get("fee_in") or Decimal(0)) - q["fee_in"], fu=p.get("fu", Decimal(0)) - q["fu"],
             amt=amt - q["amt"], part=True)
    if p["kind"] == "sw":
        q.update(sz=take, ct=take)
        p.update(sz=p["sz"] - take, ct=p["sz"] - take)
    else:
        q.update(got=take, sz=q["qty"], fee_b=(p.get("fee_b") or Decimal(0)) * f)
        p.update(got=p["got"] - take, sz=p["qty"], fee_b=(p.get("fee_b") or Decimal(0)) - q["fee_b"])
    if p.get("aid"):
        rg_algo_cancel(G, p["acct"], G[p["kind"]]["iid"], p["aid"])
        p["aid"] = None
        p["want"] = (p["sl"], p["tp"]) if p["sl"] is not None and not p.get("manual") else None
    G["pos"].append(q)
    return q

def rg_alloc(G, od, n, why, ps, tot):
    """一張不是 /run 條件單的平倉單（手動平倉、強平、減倉）平掉 n（張／顆）→ 從最深一層（離原點最遠）往回算。
    現貨：一單賣到只剩低於 OKX 最小數量（零頭）＝整單出場（剩下的算零頭留在帳戶）。"""
    rem = n
    unit = lambda p: p["sz"] if p["kind"] == "sw" else (p.get("got") or p["qty"])
    for p in sorted(ps, key=lambda p: (-abs(gd_off(p["kind"], p["k"])), -p["t_in"])):
        if rem <= 0:
            break
        u = unit(p)
        take = min(rem, u)
        mn = (G.get(p["kind"]) or {}).get("minsz") or Decimal(0)
        whole = take >= u or (p["kind"] == "sp" and u - take < mn)
        q = p if whole else rg_split(G, p, take)
        rg_close(G, q, od, why, part=take / tot if tot else None, cancel_algo=True, sold=take if p["kind"] == "sp" else None)
        rem -= take
    return n - rem

async def rg_entry_state(G, o, P):
    """一張進場單在 OKX 的狀態。回傳 None＝還在掛／還不確定；('fill', OKX 訂單)＝成交了（或撤掉前部分成交）；'gone'＝沒成交就沒了。"""
    acct, iid = o["acct"], G[o["kd"]]["iid"]
    A = P.get(acct)
    if A is None:
        return None
    now = time.time()
    if not o.get("oid"):
        if o.get("busy") and not o.get("amb"):
            return None                                    # 還在送
        if not o.get("clid"):
            return None                                    # 根本沒送到 OKX（被拒，rg_go_place 會處理）
        x = A["pcl"].get(o["clid"])
        if x:
            o["oid"], o["amb"] = x["ordId"], False
        else:
            r = await api("GET", f"/api/v5/trade/order?instId={iid}&clOrdId={o['clid']}", acct=acct)
            if str(r.get("code")) == "0" and rg_d0(r).get("ordId"):
                o["oid"], o["amb"] = rg_d0(r)["ordId"], False
            elif now - (o.get("t_s") or 0) > RG_AMB_SEC:
                rg_ev("amb_gone", G, nm=gd_nm(o["kd"], o["k"]), clid=o["clid"])
                return "gone"
            else:
                return None
        if o.get("cxl"):
            rg_cancel(G, o, force=True)
    x = A["pend"].get(o["oid"])
    if x:
        o["acc"] = rg_D(x.get("accFillSz"))
        if o.get("cxl") and now - (o.get("cxl_t") or 0) > RG_CXL_AGAIN:
            rg_cancel(G, o, force=True)                    # 撤單 3 秒還在 → 再撤一次
        return None
    r = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={o['oid']}", acct=acct)
    od = rg_d0(r)
    if str(r.get("code")) != "0" or not od:
        return None
    st = od.get("state")
    if st in ("live", "partially_filled"):
        o["acc"] = rg_D(od.get("accFillSz"))
        return None
    acc = rg_D(od.get("accFillSz"))
    rg_ev("entry_end", G, nm=gd_nm(o["kd"], o["k"]), acct=acct, oid=o["oid"], state=st, acc=acc, avgPx=od.get("avgPx"), fee=od.get("fee"),
          fillTime=od.get("fillTime"), cancelSource=od.get("cancelSource"), mine=bool(o.get("cxl")))
    return ("fill", od) if acc > 0 else "gone"

async def rg_algo_gone(G, p):
    """這一單的條件單不在 OKX 掛單清單了：觸發（→ 平倉單成交才算出場）或被取消（→ 重設一次）。"""
    aid, acct, iid = p["aid"], p["acct"], G[p["kind"]]["iid"]
    r = await api("GET", f"/api/v5/trade/orders-algo-history?ordType=oco&algoId={aid}", acct=acct)
    if str(r.get("code")) != "0":
        return
    a = rg_d0(r)
    if not a:
        p["gone_n"] = p.get("gone_n", 0) + 1
        if p["gone_n"] < 20:
            return
        a = {"state": "canceled"}
    st = a.get("state")
    if st in ("live", "pause"):
        return
    nm = gd_nm(p["kind"], p["k"]) + gd_atag(p["kind"], p["k"])
    if st in ("effective", "partially_effective"):
        ol = a.get("ordIdList") or []
        oid = (ol[0] if ol else None) or a.get("ordId")
        if not oid:
            p["x_pend"] = True
            return
        r2 = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={oid}", acct=acct)
        od = rg_d0(r2)
        if str(r2.get("code")) != "0" or not od:
            p["x_pend"] = True
            return
        acc = rg_D(od.get("accFillSz"))
        if od.get("state") in ("live", "partially_filled"):
            p["x_pend"] = True
            return
        if acc <= 0:
            st = "order_failed"                            # 觸發了但平倉單沒成交 → 當作條件單沒了
        else:
            hit = (a.get("actualSide") or "").lower()
            if hit not in ("tp", "sl"):                    # OKX 沒給 → 用成交價離哪個觸發價近判斷
                xp = rg_D(od.get("avgPx"))
                hit = "tp" if p["tp"] is not None and abs(xp - p["tp"]) < abs(xp - (p["sl"] or xp)) else "sl"
            trig = rg_D(a.get("tpTriggerPx") if hit == "tp" else a.get("slTriggerPx")) or (p["tp"] if hit == "tp" else p["sl"])
            why = "TP" if hit == "tp" else ("SL(移動)" if p["n_mv"] else "SL")
            whole = p["sz"] if p["kind"] == "sw" else (p.get("ssz") or acc)
            if p["kind"] == "sw" and acc < whole:
                q = rg_split(G, p, acc)                    # 只平掉一部分：剩下的重設 SL/TP
            else:
                q = p
                if p["kind"] == "sp" and acc < whole:
                    rg_warn(G, f"part{nm}", f"{nm} 現貨只賣掉 {pct(acc)}顆(條件單 {pct(whole)}顆),剩下的留在帳戶,請自己處理")
            rg_close(G, q, od, why, trig, rg_ms(a.get("triggerTime")), sold=acc if p["kind"] == "sp" else None)
            return
    p["aid_x"], p["aid"], p["x_pend"] = aid, None, False
    rg_ev("algo_gone", G, nm=nm, aid=aid, state=st)
    if aid in (G.get("acx") or []):
        return
    if st == "order_failed":
        p["t_amd"] = time.time() + 10                      # 先讓對帳處理（多半已經被手動平掉／賣掉），10 秒後才重設
        rg_warn(G, f"afail{nm}", f"{nm} 的 SL/TP 觸發了但 OKX 平倉失敗(多半這一單已經被平掉／賣掉),BOT 用 OKX 倉位對帳")
        return
    p["acx_pend"] = time.time()                            # 先等對帳（倉位被平掉／強平時 OKX 會自己撤條件單），還在才算被取消（rg_acx_due）

def rg_acx_due(G, p):
    """條件單不是 BOT 撤的、等了幾秒這一單還在 → 算「被取消」：重設一次；第二次就不再設（通知）。"""
    p.pop("acx_pend", None)
    nm = gd_nm(p["kind"], p["k"]) + gd_atag(p["kind"], p["k"])
    p["n_acx"] = p.get("n_acx", 0) + 1
    if p["n_acx"] >= 2:
        p["manual"], p["want"] = True, None
        rg_warn(G, f"acx{nm}", f"{nm} 的 SL/TP 在 OKX 被取消第 {p['n_acx']} 次,BOT 不再幫這一單設,請自己管理", every=1)
    else:
        p["want"] = (p["sl"], p["tp"]) if p["sl"] is not None else None
        rg_warn(G, f"acx{nm}", f"{nm} 的 SL/TP 在 OKX 被取消了,BOT 照原價重設一次", every=1)

def rg_pos_sync(G, acct, side, x, t):
    """OKX 合約倉位（那個帳戶那一邊一個倉位）：張數、均價、標記價格、預估強平價、未實現、保證金、累計資金費。
    累計資金費有變動 → 那一次的資金費按張數分給當時抱著的每一單（OKX 實際金額）。"""
    key = rg_pkey(acct, side)
    okp = G.setdefault("okpos", {})
    if not x:
        okp[key] = {"t": t, "sz": Decimal(0), "avg": None, "mark": None, "liq": None, "upl": Decimal(0), "margin": None, "pid": None}
        G.setdefault("ff0", {}).pop(key, None)            # 倉位歸零：資金費從頭算
        return
    ff = rg_D(x.get("fundingFee")) if x.get("fundingFee") not in (None, "") else None
    okp[key] = {"t": t, "sz": abs(rg_D(x.get("pos"))), "avg": rg_D(x.get("avgPx")) or None, "mark": rg_D(x.get("markPx")) or None,
                "liq": rg_D(x.get("liqPx")) or None, "upl": rg_D(x.get("upl")), "pid": x.get("posId"), "lever": x.get("lever"),
                "margin": rg_D(x.get("margin")) if x.get("margin") not in (None, "") else None}
    if ff is None:
        return
    b = G.setdefault("ff0", {}).get(key)
    if not b or b[0] != x.get("posId"):
        G["ff0"][key] = [x.get("posId"), str(ff)]
        if ff != 0:
            rg_ev("ff_base", G, key=key, ff=ff)
        return
    if ff != Decimal(b[1]):
        G["ff0"][key] = [x.get("posId"), str(ff)]
        rg_funding(G, acct, side, ff - Decimal(b[1]))

def rg_funding(G, acct, side, amt):
    """OKX 資金費（實際）：按張數分給這一刻抱著的每一單，算進各單淨損益（跟 /runt 一樣不另外發通知）。"""
    live = [p for p in G["pos"] if not p["out"] and p["kind"] == "sw" and p["acct"] == acct and p["side"] == side]
    tot = sum((p["sz"] for p in live), Decimal(0))
    rg_ev("funding", G, acct=acct, side=side, amt=amt, n=len(live))
    if not live or not tot:
        return
    for p in live:
        p["fu"] = p.get("fu", Decimal(0)) + amt * p["sz"] / tot
        p["fn"] = p.get("fn", 0) + 1
    print(f"[run] 資金費 {G['sym']} {acct} {rg_ps(side)} {amt}")
    gd_save_soon()

def rg_ents(G, kd, acct=None, side=None):
    """還在 OKX 上的進場單（kd 那一種；合約可以再限定帳戶、哪一邊）。"""
    out = []
    for o in list(G["ord"].values()) + [G.get("o00")]:
        if not o or o["kd"] != kd:
            continue
        if acct is not None and o["acct"] != acct:
            continue
        if side is not None and ("L" if gd_buy(kd, o["k"]) else "S") != side:
            continue
        out.append(o)
    return out

async def rg_attr(G, kd, acct, side, diff):
    """OKX 倉位（現貨＝OKX 餘額）比 BOT 記的少、又不是 /run 條件單平的 → 查 OKX 成交紀錄找平倉單（手動平倉／強平／減倉），從最深一層往回算。"""
    iid = G[kd]["iid"]
    r = await api("GET", f"/api/v5/trade/orders-history?instType={'SWAP' if kd == 'sw' else 'SPOT'}&instId={iid}&limit=50", acct=acct)
    if str(r.get("code")) != "0":
        return 0
    cs = "sell" if side == "L" else "buy"
    seen = set(G.get("seen") or [])
    ps = [p for p in G["pos"] if not p["out"] and p["kind"] == kd and p["acct"] == acct and p["side"] == side]
    mine = {p["aid"]: p for p in ps if p.get("aid")}
    t_min = (G["t_run"] - 5) * 1000
    cand = []
    for od in r.get("data") or []:
        if od.get("side") != cs or od.get("ordId") in seen:
            continue
        if kd == "sw" and od.get("posSide") != rg_ps(side):
            continue
        if int(od.get("cTime") or 0) < t_min or rg_D(od.get("accFillSz")) <= 0:
            continue
        if od.get("state") not in ("filled", "canceled", "mmp_canceled"):
            continue
        if od.get("algoId") and od["algoId"] in mine:      # /run 自己的條件單觸發的 → 交給條件單那邊（馬上查）
            mine[od["algoId"]]["t_aid"] = 0
            continue
        if str(od.get("algoClOrdId") or "").startswith(RG_APFX) or str(od.get("clOrdId") or "").startswith(RG_PFX):
            continue
        cand.append(od)
    cand.sort(key=lambda od: int(od.get("fillTime") or od.get("uTime") or 0))
    done, rem = 0, diff
    for od in cand:
        if rem <= 0:
            break
        why = RG_WHY_OTHER.get(od.get("category") or "", "手動平倉")
        tot = rg_D(od.get("accFillSz"))
        n = min(tot, rem) if kd == "sw" else min(tot, sum((p.get("got") or p["qty"] for p in ps if not p["out"]), Decimal(0)))
        #   現貨：OKX 餘額裡有之前留下的零頭，差額會比賣掉的少 → 照那張賣單真的賣掉的顆數算（最多到 BOT 抱著的）
        got = rg_alloc(G, od, n, why, [p for p in ps if not p["out"]], tot)
        if od.get("ordId") and od["ordId"] not in G.setdefault("seen", []):
            G["seen"].append(od["ordId"])
        rem -= got
        done += 1
        if why != "手動平倉":
            rg_warn(G, f"oth{od.get('ordId')}", f"OKX {why} {acct} {pct(got)}{'張' if kd == 'sw' else '顆'}(單號 {od.get('ordId')}),從最深一層往回算", every=1)
    return done

async def rg_mis_swap(G, acct, side, A, now):
    """OKX 合約倉位張數 vs BOT（抱著的單＋還在掛、已部分成交的進場單）。對上了回 True（可以調保證金）；
    對不上 → 再確認一拍，OKX 少了 → 找手動平倉／強平／減倉；20 秒還對不上 → 通知。"""
    key = rg_pkey(acct, side)
    have = ((G.get("okpos") or {}).get(key) or {}).get("sz") or Decimal(0)
    live = [p for p in G["pos"] if not p["out"] and p["kind"] == "sw" and p["acct"] == acct and p["side"] == side]
    ents = rg_ents(G, "sw", acct, side)
    bot = sum((p["sz"] for p in live), Decimal(0)) + sum(((o.get("acc") or Decimal(0)) for o in ents), Decimal(0))
    mis = G.setdefault("mis", {})
    if have == bot:
        if mis.get(key) and mis[key].get("told"):
            rg_ev("mismatch_ok", G, key=key, have=have, bot=bot)
        mis.pop(key, None)
        return not any((o.get("acc") or 0) > 0 for o in ents)
    busy = (any(p.get("x_pend") or p.get("abusy") or (p.get("aid") and p["aid"] not in A["alg"]) for p in live)
            or any(o.get("amb") or (o.get("busy") and not o.get("oid")) for o in ents))
    m = mis.get(key)
    if not m or m["bot"] != bot or m["have"] != have:
        mis[key] = {"t": now, "bot": bot, "have": have}
        return False
    if busy or now - m["t"] < RG_MIS_CONF:
        return False
    if have < bot and await rg_attr(G, "sw", acct, side, bot - have):
        mis.pop(key, None)
        return False
    if now - m["t"] > RG_MIS_SEC and not m.get("told"):
        m["told"] = True
        rg_ev("mismatch", G, key=key, have=have, bot=bot)
        rg_warn(G, f"mis{key}", f"{acct} {'空單' if side == 'S' else '多單'} OKX 倉位 {pct(have)}張,BOT 記 {pct(bot)}張,對不上\n"
                + ("OKX 成交紀錄還找不到平倉單,BOT 繼續核對" if have < bot else "OKX 多出來的不是 /run 下的單,BOT 不會管它"), every=1800)
    return False

def rg_mg_target(G, ps, x=None):
    """這個倉位該有的保證金＝每一單的每層U 加起來 − 開倉手續費 ＋ 資金費（跟 /runt 的強平公式 rn_gliq 的保證金餘額一樣）。
    x＝OKX 倉位：OKX 部分平倉後均價不變（均價成本），跟 BOT 每一單自己的進場價加起來不一樣 → 差多少補多少（調少），
    讓 OKX 的預估強平價＝/runt 公式（rn_gliq）算的強平價（每個帳戶 11 單在第一層的強平價一起強平）。"""
    mb = sum((gd_pamt(G, p) - (p.get("fee_in") or Decimal(0)) + (p.get("fu") or Decimal(0)) for p in ps), Decimal(0))
    if x and x.get("avg") and ps:
        q = sum((p["qty"] for p in ps), Decimal(0))
        n_bot = sum((p["qty"] * p["ent"] for p in ps), Decimal(0))
        n_okx = x["avg"] * q
        mb += (n_bot - n_okx) if ps[0]["side"] == "S" else (n_okx - n_bot)
    return mb

def rg_mg_check(G, acct, side, now):
    """合約倉位的保證金調到 rg_mg_target（成交後補上、出場後 OKX 照比例退了之後調回來）→ OKX 預估強平價＝設計的強平價。
    OKX 數字是調整之後才查到的才用（不會重複調）；調了 OKX 保證金沒變 → 停下來、通知（不會一直調）。"""
    key = rg_pkey(acct, side)
    x = (G.get("okpos") or {}).get(key) or {}
    if not x.get("sz") or x.get("margin") is None:
        return
    ps = [p for p in G["pos"] if not p["out"] and p["kind"] == "sw" and p["acct"] == acct and p["side"] == side]
    if not ps or any(p.get("x_pend") for p in ps):
        return
    g = G.setdefault("mg", {}).setdefault(key, {})
    if g.get("busy") or now < g.get("stuck", 0) or now - g.get("t", 0) < RG_MG_GAP or x["t"] <= g.get("done_t", 0):
        return
    gen = (G.get("gen") or {}).get(key, 0)
    last = g.pop("last", None)
    if last and abs(x["margin"] - last[1]) < last[0] / 2 and x["sz"] == last[2] and gen == last[3] and x.get("pid") == last[4]:
        g["stuck"] = now + 60                              # 上一次調了，OKX 的保證金沒有變 → 1 分鐘內不再調、通知
        rg_warn(G, f"mgstuck{key}", f"{acct} {'空單' if side == 'S' else '多單'} 調保證金之後 OKX 沒有變,BOT 1 分鐘後再試(強平價以 OKX 為準)")
        return
    tgt = rg_mg_target(G, ps, x)
    diff = tgt - x["margin"]
    if abs(diff) < max(RG_MG_TOL, tgt * RG_MG_PCT) or tgt <= 0:
        return
    add = diff > 0
    amt = abs(diff).quantize(Decimal("0.01"), rounding=ROUND_CEILING if add else ROUND_DOWN)
    if add and amt > tgt + 1:
        amt = (tgt + 1).quantize(Decimal("0.01"), rounding=ROUND_DOWN)   # 保險：一次最多補到目標
    if amt <= 0:
        return
    g.update(busy=True, t=now)
    iid = G["sw"]["iid"]

    async def go():
        try:
            r = await api("POST", "/api/v5/account/position/margin-balance",
                          {"instId": iid, "posSide": rg_ps(side), "type": "add" if add else "reduce", "amt": rg_s(amt)}, acct=acct)
            sc = rg_sc(r)
            rg_ev("margin", G, key=key, type="add" if add else "reduce", amt=amt, tgt=tgt, okx=x["margin"], sCode=sc, sMsg=rg_msg(r))
            if str(r.get("code")) == "0" and sc in ("0", "None"):
                g["last"] = (amt, x["margin"], x["sz"], gen, x.get("pid"))
                print(f"[run] 保證金 {G['sym']} {key} {'+' if add else '-'}{amt}U(目標 {tgt:.4f}U)")
            else:
                g["t"] = time.time() + 30                  # 30 秒後再試
                rg_warn(G, f"mg{key}", f"{acct} {'空單' if side == 'S' else '多單'} {'補' if add else '退'}保證金 {amt}U 被 OKX 拒絕 sCode={sc} {rg_msg(r)}".strip()
                        + "\n強平價以 OKX 為準,BOT 30 秒後再試")
        except Exception as e:
            print("[run] margin fail", G["sym"], type(e).__name__, e)
            g["t"] = time.time() + 30
        finally:
            g["busy"], g["done_t"] = False, time.time()
            rg_fdq(acct)
    rt_bg(go())

async def rg_spot_check(G, now, accts):
    """每 3 秒：每個帳戶的 OKX 現貨餘額（v20.0：現貨也分 5 個帳戶；/status 官方均價用 OKX 平均成本）；
    比 BOT 在那個帳戶抱著的實得顆數少 → 1111 手動賣掉了 → 查那個帳戶的 OKX 賣單，從最深一層往回算。accts＝這次對帳查得到的帳戶。"""
    if now - (G.get("sp_t") or 0) < RG_SPOT_GAP:
        return
    G["sp_t"] = now
    base = G["sym"][:-4]
    oks = G["okspot"] if isinstance(G.get("okspot"), dict) else G.setdefault("okspot", {})
    mis = G["spmis"] if isinstance(G.get("spmis"), dict) else {}
    G["spmis"] = mis
    held = {}
    for p in G["pos"]:
        if not p["out"] and p["kind"] == "sp":
            held.setdefault(p["acct"], []).append(p)
    todo = [a for a in accts if a in held or a in oks]
    if not todo:
        return
    rs = await asyncio.gather(*(api("GET", f"/api/v5/account/balance?ccy={base}", acct=a) for a in todo))
    tol = ((G.get("sp") or {}).get("lot") or GD_LOT0) * 2
    for a, r in zip(todo, rs):
        if str(r.get("code")) != "0":
            continue
        ps = [p for p in held.get(a) or [] if not p["out"]]
        if not ps:
            oks.pop(a, None)
            mis.pop(a, None)
            continue
        x = next((d for d in ((r.get("data") or [{}])[0].get("details") or []) if d.get("ccy") == base), {})
        have = rg_D(x.get("cashBal"))
        oks[a] = {"t": now, "bal": have, "avg": rg_D(x.get("openAvgPx")) or None, "upl": rg_D(x.get("spotUpl"))}
        if any(p.get("x_pend") or p.get("abusy") for p in ps) or any((o.get("acc") or 0) > 0 for o in rg_ents(G, "sp", a)):
            mis.pop(a, None)
            continue
        bot = sum((p.get("got") or p["qty"] for p in ps), Decimal(0))
        if have >= bot - tol:
            mis.pop(a, None)
            continue
        m = mis.get(a)
        if not m or m["bot"] != bot or m["have"] != have:
            mis[a] = {"t": now, "bot": bot, "have": have}
            continue
        if await rg_attr(G, "sp", a, "L", bot - have):
            mis.pop(a, None)
            continue
        if now - m["t"] > RG_MIS_SEC and not m.get("told"):
            m["told"] = True
            rg_ev("spot_mismatch", G, acct=a, have=have, bot=bot)
            rg_warn(G, f"spmis{a}", f"{a} OKX 現貨 {base} {pct(have)}顆,BOT 記 {pct(bot)}顆,對不上\nOKX 成交紀錄還找不到賣單,BOT 繼續核對", every=1800)

def rg_err(G, where, e):
    """對帳的某一段出錯：只記下來、10 分鐘通知一次，其他段照常（不會因為一個例外整組卡住）。"""
    print(f"[run] sync {where} fail", G.get("sym"), type(e).__name__, e)
    rg_warn(G, f"err{where}", f"BOT 內部錯誤({where},已經接著跑,OKX 上的單照常):{type(e).__name__}: {e}")

async def rg_sync_group(G, P, now):
    # ① 進場單（L00、3 個位置）
    for slot, o in [("o00", G.get("o00"))] + list(G["ord"].items()):
        if not o or P.get(o["acct"]) is None:
            continue
        try:
            await rg_sync_entry(G, slot, o, P, now)
        except Exception as e:
            rg_err(G, "entry", e)
    # ② 條件單
    for p in list(G["pos"]):
        if p["out"] or not p.get("real"):
            continue
        A = P.get(p["acct"])
        if A is None:
            continue
        try:
            await rg_sync_algo(G, p, A, now)
        except Exception as e:
            rg_err(G, "algo", e)
    # ③ 合約倉位：資金費、對帳、保證金
    if G.get("sw"):
        for a in GD_XACCT:
            A = P.get(a)
            if not A or A.get("pos") is None:
                continue
            for side in ("S", "L"):
                try:
                    key = rg_pkey(a, side)
                    x = A["pos"].get((G["sw"]["iid"], rg_ps(side)))
                    used = any(not p["out"] and p["kind"] == "sw" and p["acct"] == a and p["side"] == side for p in G["pos"]) or rg_ents(G, "sw", a, side)
                    if not x and not used and key not in (G.get("okpos") or {}):
                        continue
                    rg_pos_sync(G, a, side, x, A["t"])
                    if await rg_mis_swap(G, a, side, A, now):
                        rg_mg_check(G, a, side, now)
                except Exception as e:
                    rg_err(G, "pos", e)
    # ④ 現貨餘額（v20.0：每個帳戶）
    if G.get("sp"):
        try:
            await rg_spot_check(G, now, [a for a in GD_XACCT if P.get(a) is not None])
        except Exception as e:
            rg_err(G, "spot", e)
    # ⑤ 條件單被取消（不是 BOT 撤的、這一單還在）
    for p in list(G["pos"]):
        if not p["out"] and p.get("acx_pend") and now - p["acx_pend"] >= (RG_ACX_WAIT_SP if p["kind"] == "sp" else RG_ACX_WAIT):
            rg_acx_due(G, p)
    # ⑥ 孤兒單（每 60 秒）
    try:
        await rg_orphans(G, {a: P.get(a) for a in GD_XACCT}, now)
    except Exception as e:
        rg_err(G, "orphan", e)

async def rg_sync_entry(G, slot, o, P, now):
    """一張進場單：成交 → 一單持倉（L00＝原點）；沒成交就沒了 → 清掉（post-only 被取消 → 冷卻 1 秒）。"""
    if True:
        res = await rg_entry_state(G, o, P)
        if res is None:
            return
        mine = bool(o.get("cxl"))
        if res == "gone":
            if slot == "o00":
                if G.get("o00") is o:
                    if o.get("drop"):
                        G["o00"] = None
                    else:
                        o.update(clid=None, oid=None, amb=False, cxl=False, acc=Decimal(0))   # L00 沒成交就沒了 → 價格回到 L00 上面再重掛
                        if not mine:
                            G["cool"]["o00"] = now + RG_PO_COOL
                            G["n_po"] = G.get("n_po", 0) + 1
            elif G["ord"].get(slot) is o:
                G["ord"][slot] = None
                if not mine:
                    G["cool"][slot] = now + RG_PO_COOL     # post-only 被 OKX 取消（或在 OKX 被手動取消）→ 1 秒後照現價重算
                    G["n_po"] = G.get("n_po", 0) + 1
            rg_fdq(o["acct"])
            gd_save_soon()
            G["ev"].set()
            return
        p = rg_pos_new(G, o, res[1])
        G["pos"].append(p)
        if slot == "o00":
            if G.get("o00") is o:
                G["o00"] = None
            if G["org"] is None:
                G["org"] = {"px": o["px"], "t": p["t_in"]}  # 💰L00 成交＝🩵原點（跟 /runt 一樣用掛單價）
        elif G["ord"].get(slot) is o:
            G["ord"][slot] = None
        rg_fdq(o["acct"])
        gd_save_soon()
        G["ev"].set()

async def rg_sync_algo(G, p, A, now):
    """一單的條件單：還在 OKX 清單上＝沒事；不在了（而且不是剛送出）→ 查 OKX 歷史：觸發出場／被取消。"""
    if p.get("acl_wait"):
        x = A["acl"].get(p["acl_wait"])
        if x:
            p["aid"], p["t_aid"], p["acl_wait"] = x["algoId"], now, None
            rg_algo_ok(p, *p["sent"])
        elif now - (p.get("t_aw") or now) > RG_AMB_SEC:
            p["acl_wait"] = None                           # 沒送到 → 下一拍重送
        return
    aid = p.get("aid")
    if not aid or aid in A["alg"] or now - (p.get("t_aid") or 0) < RG_AID_WAIT:
        return
    await rg_algo_gone(G, p)


RG_ORPH_GAP = 60       # 每 60 秒掃一次 OKX 上 /run 的孤兒單（沒有任何一單認領的掛單、條件單）
RG_ORPH_AGE = 30       # 送出去超過 30 秒還沒有人認領才算（剛送出、回應還沒回來的不算）

async def rg_orphans(G, P, now):
    """保險：OKX 上這個幣有 /run（這個 bot）的掛單、條件單，但 BOT 沒有任何一單認領（例：bot 在送單途中當掉）→ 撤掉、通知。
    不撤的話，一張沒人管的條件單觸發時會平掉別的單的倉位。"""
    if now - (G.get("orph_t") or 0) < RG_ORPH_GAP:
        return
    G["orph_t"] = now
    old = lambda x: now - (int(x.get("cTime") or 0) / 1000) > RG_ORPH_AGE
    oids = {o.get("oid") for o in list(G["ord"].values()) + [G.get("o00")] if o} - {None}
    clids = {o.get("clid") for o in list(G["ord"].values()) + [G.get("o00")] if o} - {None}
    aids = {p.get("aid") for p in G["pos"] if not p["out"]} - {None}
    acls = {p.get("acl_wait") for p in G["pos"] if not p["out"]} - {None}
    found = []
    for a, A in P.items():
        if A is None:
            continue
        iids = {G[kd]["iid"] for kd in gd_kinds(G)}       # v20.0：現貨、合約都在 5 個帳戶
        for od in A["pend"].values():
            if (od.get("instId") in iids and str(od.get("clOrdId") or "").startswith(RG_PFX) and old(od)
                    and od.get("ordId") not in oids and od.get("clOrdId") not in clids):
                found.append(f"{a} 掛單 {od.get('instId')} {od.get('side')} {od.get('px')}×{od.get('sz')}")
                await api("POST", "/api/v5/trade/cancel-order", {"instId": od["instId"], "ordId": od["ordId"]}, acct=a)
        for x in A["alg"].values():
            if (x.get("instId") in iids and str(x.get("algoClOrdId") or "").startswith(RG_APFX) and old(x)
                    and x.get("algoId") not in aids and x.get("algoClOrdId") not in acls):
                found.append(f"{a} 條件單 {x.get('instId')} {x.get('sz')} SL {x.get('slTriggerPx')}|TP {x.get('tpTriggerPx')}")
                rg_algo_cancel(G, a, x["instId"], x["algoId"])
    if found:
        rg_ev("orphan", G, items=found)
        rg_warn(G, "orphan", "OKX 上有 /run 的單沒有任何一單認領(多半是 bot 送單途中重開),已撤掉:\n" + "\n".join(found), every=1)

async def rg_algo_pending(a):
    """這個帳戶全部還沒觸發的 OCO（一頁 100 張，最多翻 5 頁）。"""
    rows, after = [], ""
    for _ in range(5):
        r = await api("GET", "/api/v5/trade/orders-algo-pending?ordType=oco&limit=100" + (f"&after={after}" if after else ""), acct=a)
        if str(r.get("code")) != "0":
            return r
        d = r.get("data") or []
        rows += d
        if len(d) < 100:
            break
        after = d[-1].get("algoId") or ""
    return {"code": "0", "data": rows}

async def rg_sync():
    """向 OKX 對帳一次（全部 /run 的組一起）：每個帳戶 掛單、條件單、持倉（每 1 秒）各查一次，再逐組比對。
    一個帳戶有任何一個查不到 → 那個帳戶這一次不下任何結論。"""
    gs = [G for G in list(RD.values()) if not G.get("done")]
    if not gs:
        return
    now = time.time()
    accts = set(GD_XACCT)                                  # v20.0：現貨、合約都在 5 個帳戶
    sw_accts = set(GD_XACCT) if any(G.get("sw") for G in gs) else set()

    async def one(a):
        dopos = a in sw_accts and now - RG_SYNC["pos_t"].get(a, 0) >= RG_POS_GAP - 0.05
        calls = [api("GET", "/api/v5/trade/orders-pending?limit=100", acct=a), rg_algo_pending(a)]
        if dopos:
            calls.append(api("GET", "/api/v5/account/positions?instType=SWAP", acct=a))
        res = await asyncio.gather(*calls)
        if any(str(r.get("code")) != "0" for r in res):
            if any(str(r.get("code")) == "50011" for r in res):
                await asyncio.sleep(0.5)
            return a, None, [str(r.get("code")) for r in res]
        pr = res[2] if dopos else None
        if dopos:
            RG_SYNC["pos_t"][a] = now
        pend = res[0].get("data") or []
        alg = res[1].get("data") or []
        A = {"t": now, "pend": {x.get("ordId"): x for x in pend}, "pcl": {x.get("clOrdId"): x for x in pend if x.get("clOrdId")},
             "alg": {x.get("algoId"): x for x in alg}, "acl": {x.get("algoClOrdId"): x for x in alg if x.get("algoClOrdId")},
             "pos": None}
        if pr is not None:
            A["pos"] = {(x.get("instId"), x.get("posSide")): x for x in pr.get("data") or [] if rg_D(x.get("pos")) != 0}
        return a, A, None
    out = await asyncio.gather(*(one(a) for a in sorted(accts)))
    P = {a: A for a, A, _ in out}
    bad = [(a, c) for a, A, c in out if A is None]
    if bad:
        RG_SYNC["ok"] = False
        RG_SYNC["err"] += 1
        if time.time() - RG_SYNC["warn_t"] > 60:
            RG_SYNC["warn_t"] = time.time()
            print(f"[run] 對帳查詢失敗 {bad} → 那個帳戶這一次不判斷")
    else:
        RG_SYNC.update(t=now, ok=True)
    for G in gs:
        try:
            await rg_sync_group(G, P, now)
        except Exception as e:
            print("[run] sync fail", G.get("sym"), type(e).__name__, e)

async def rg_sync_task():
    """背景：有 /run 的組就每 0.5 秒對帳一次；私有 WS 有動靜（或 BOT 剛送單、撤單）就馬上對（最少隔 0.25 秒）。"""
    last = 0.0
    while not SHUTTING_DOWN:
        try:
            if not RD:
                try:
                    await asyncio.wait_for(rg_wake().wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                rg_wake().clear()
                continue
            try:
                await asyncio.wait_for(rg_wake().wait(), timeout=RG_SYNC_GAP)
            except asyncio.TimeoutError:
                pass
            rg_wake().clear()
            gap = time.time() - last
            if gap < RG_SYNC_MIN:
                await asyncio.sleep(RG_SYNC_MIN - gap)
            last = time.time()
            await rg_sync()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[run] sync task fail", type(e).__name__, e)
            await asyncio.sleep(1)

async def rg_bal_task():
    """背景：/run 用到的帳戶每 10 秒讀一次 OKX 可用（＋帳戶模式）；下單、成交、撤單之後 1 秒內再讀一次。"""
    last = {}
    while not SHUTTING_DOWN:
        await asyncio.sleep(1)
        if not RD:
            continue
        now = time.time()
        for a in rg_accts():
            q = RG_FDQ.get(a)
            if not ((q is not None and now - q >= 0.5) or now - last.get(a, 0) >= RG_BAL_GAP):
                continue
            RG_FDQ.pop(a, None)
            last[a] = now
            try:
                e = await fd_refresh(a, RG_APP.get("app"))
                if e:
                    print("[run] 帳戶讀不到", a, e)
            except Exception as ex:
                print("[run] 帳戶讀取失敗", a, type(ex).__name__, ex)

async def rg_ws_one(a):
    """私有頻道（一個帳戶）：訂單有動靜就叫醒對帳；真相一律回查 OKX（WS 漏訊也不會誤判）。這個帳戶沒有 /run 就斷開。"""
    wsl = _ws_lib()
    if not wsl:
        return
    while not SHUTTING_DOWN and a in rg_accts():
        try:
            async with wsl.connect(WS_PRI_URL, ping_interval=20, ping_timeout=10, family=OKX_FAM) as ws:
                ts = str(int(time.time()))
                sgn = base64.b64encode(hmac.new(ACC[f"OKX_{a}_SECRET"].encode(), (ts + "GET" + "/users/self/verify").encode(),
                                                hashlib.sha256).digest()).decode()
                await ws.send(json.dumps({"op": "login", "args": [{"apiKey": ACC[f"OKX_{a}_API_KEY"], "passphrase": ACC[f"OKX_{a}_PASSPHRASE"],
                                                                  "timestamp": ts, "sign": sgn}]}))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                if json.loads(raw).get("event") != "login":
                    raise RuntimeError(f"login fail {raw[:200]}")
                await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "orders", "instType": "ANY"}]}))
                RG_WS[a] = True
                print(f"[WS] 私有頻道已連線 {a}")
                while not SHUTTING_DOWN and a in rg_accts():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    m = json.loads(raw)
                    if m.get("arg", {}).get("channel") == "orders" and m.get("data"):
                        rg_wake().set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[WS] 私有頻道斷線 {a}，3秒後重連", type(e).__name__, e)
        RG_WS[a] = False
        await asyncio.sleep(3)
    RG_WS.pop(a, None)

async def rg_ws_task():
    """背景：/run 用到的帳戶各開一條私有 WS（只當鬧鐘）。"""
    while not SHUTTING_DOWN:
        for a in rg_accts():
            t = RG_WST.get(a)
            if (t is None or t.done()) and all(ACC.get(f"OKX_{a}_{k}") for k in ("API_KEY", "SECRET", "PASSPHRASE")):
                RG_WST[a] = rt_bg(rg_ws_one(a))
        await asyncio.sleep(2)


# ---------- /run 開始：設 OKX 槓桿；/stop；收尾 ----------
def rg_lev_rows(G, o):
    """每個帳戶每一邊要設的 OKX 槓桿＝那個帳戶那一邊最高那一層的槓桿（gd_lev，照原點）：[(帳戶, S／D, 槓桿)]。"""
    out = []
    for x in "SD":
        if G["has"][x]:
            for b, a in enumerate(GD_XACCT):
                out.append((a, x, max(gd_lev(G, k, o) for k in gd_blk_ks(x, b))))
    return out

async def rg_setlev(G, o):
    """/run 開始（檢查都過了、還沒掛單）：設 OKX 逐倉槓桿（每個帳戶每一邊一個）。失敗回原因。"""
    G["lev_set"] = {}
    for a, x, lv in rg_lev_rows(G, o):
        side = "S" if x == "S" else "L"
        r = await api("POST", "/api/v5/account/set-leverage",
                      {"instId": G["sw"]["iid"], "lever": rg_s(lv), "mgnMode": "isolated", "posSide": rg_ps(side)}, acct=a)
        sc = rg_sc(r)
        rg_ev("set_lev", G, acct=a, side=rg_ps(side), lever=lv, code=r.get("code"), sCode=sc, sMsg=rg_msg(r))
        if str(r.get("code")) != "0" or sc not in ("0", "None"):
            return f"{a} {'空單' if x == 'S' else '多單'} 設槓桿 {pct(lv)}X 被 OKX 拒絕 sCode={sc} {rg_msg(r)}".strip()
        G["lev_set"][rg_pkey(a, side)] = str(lv)
    return None

def rg_lev_lines(G, o):
    """/run 開始通知：OKX 逐倉槓桿那幾行（每個帳戶一行：空 3.43X|多 1.93X）。"""
    rows = rg_lev_rows(G, o)
    if not rows:
        return []
    L = ["", "OKX逐倉槓桿(設成每個帳戶最高那一層,進場後保證金補到每層U)"]
    for a in GD_XACCT:
        xs = [f"{'空' if x == 'S' else '多'}{pct(lv)}X" for b, x, lv in rows if b == a]
        if xs:
            L.append(f"{a} " + "|".join(xs))
    return L

async def rg_stop_one(G):
    """/stop 一組（實盤）：撤掉所有掛單（等 OKX 確認，最多 6 秒）、不再掛新單；持倉照規則跑到出場（BOT 不平倉）。
    沒有持倉、掛單都撤掉了＝馬上結束；撤單時剛好成交＝那一單也跑到出場。"""
    now = time.time()
    cx = gd_cx_list(G)
    G["stop"], G["stop_wait"] = now, True
    for o in list(G["ord"].values()) + [G.get("o00")]:
        if o:
            o["drop"] = True
            if rg_onokx(o):
                rg_cancel(G, o)
    if G.get("o00") and not rg_onokx(G["o00"]):
        G["o00"] = None
    for s, o in list(G["ord"].items()):
        if o and not rg_onokx(o):
            G["ord"][s] = None
    gd_save_soon()
    rg_wake().set()
    t_end = now + 6
    while time.time() < t_end and (any(G["ord"].values()) or G.get("o00")) and not G["done"]:
        await asyncio.sleep(0.3)
        rg_wake().set()
    G["stop_wait"] = False
    if G["done"]:
        return None
    if gd_held(G) or any(G["ord"].values()) or G.get("o00"):
        print(f"[run] 停止中 {gd_head(G)}")
        return gd_stop_lines(G, time.time(), stopping=True, cx=cx)
    L = gd_stop_lines(G, time.time(), cx=cx)
    gd_stop_drop(G)
    rt_bg(rg_leftover(G))
    print(f"[run] 停止 {gd_head(G)}")
    return L

async def rg_leftover(G):
    """策略結束後：OKX 上還有沒有 /run 留下的掛單、條件單（這個幣）→ 撤掉、通知（持倉 BOT 不碰）。"""
    await asyncio.sleep(2)
    if G["sym"] in RD:                                     # 同一個幣又重新 /run 了 → 不能撤新的那一組的單
        return
    found = []
    for a in GD_XACCT:                                     # v20.0：現貨、合約都在 5 個帳戶
        iids = {G[kd]["iid"] for kd in gd_kinds(G)}
        r = await api("GET", "/api/v5/trade/orders-pending?limit=100", acct=a)
        for od in r.get("data") or []:
            if od.get("instId") in iids and str(od.get("clOrdId") or "").startswith(RG_PFX):
                found.append(f"{a} 掛單 {od.get('instId')} {od.get('px')}")
                await api("POST", "/api/v5/trade/cancel-order", {"instId": od["instId"], "ordId": od["ordId"]}, acct=a)
        r = await rg_algo_pending(a)
        for x in r.get("data") or []:
            if x.get("instId") in iids and str(x.get("algoClOrdId") or "").startswith(RG_APFX):
                found.append(f"{a} 條件單 {x.get('instId')} SL {x.get('slTriggerPx')}|TP {x.get('tpTriggerPx')}")
                await api("POST", "/api/v5/trade/cancel-algos", [{"instId": x["instId"], "algoId": x["algoId"]}], acct=a)
    if found:
        rg_ev("leftover", G, items=found)
        app = RG_APP.get("app")
        if app is not None and G.get("chat"):
            await rt_send(app, G["chat"], "\n".join([f"{E.WARN} /stop {GREAL.tag}{G['sym']} OKX 上還有 /run 留下的單,已撤掉:"] + found
                                                     + [f"時間:{hhmmss()}"]))

async def rg_recover(app):
    """重開後恢復 /run：存檔裡的組接著跑（持倉、掛單、條件單、原點），之後 rg_sync 以 OKX 為準對帳（關機期間的成交、出場都補上）。"""
    rn_log_prune(GREAL)
    rn_log_prune(path=RG_EV_FILE)
    data = []
    try:
        if os.path.exists(RG_FILE):
            with open(RG_FILE) as f:
                data = json.load(f) or []
    except Exception as e:
        print("[run] recover read fail", e)
    if not data:
        return
    RG_APP["app"] = app
    old = [d for d in data if int(d.get("v") or 0) < 200]  # v20.0：v19 的 /run（3% 格子、3 個帳戶）不恢復；OKX 上的單 BOT 不碰
    if old:
        try:
            os.replace(RG_FILE, RG_FILE + ".v19")
        except Exception as e:
            print("[run] v19 舊存檔改名失敗", e)
        chat = next((d.get("chat") for d in old if d.get("chat")), None) or CHAT_ID
        if chat:
            await rt_send(app, chat, "\n".join([f"{E.WARN} v20.0 /run 換新策略(每2%、5個帳戶):舊的 /run 沒有恢復(存檔改名 .v19 收起來)",
                                                "BOT 沒有動 OKX:舊的掛單、條件單、持倉還在 OKX 上,請到 OKX 處理"]
                                               + [f"{d.get('sym')} {d.get('dir') or ''}|持倉 {len(d.get('pos') or [])}單" for d in old]
                                               + [f"時間:{hhmmss()}"]))
        data = [d for d in data if int(d.get("v") or 0) >= 200]
        if not data:
            return
    await rec_okx_ready({d["sym"] for d in data if set(d.get("dir") or "") & {"S", "D"}})
    if any(set(d.get("dir") or "") & {"L", "U"} for d in data):
        for i in range(10):
            if await okx_spot_specs_all():
                break
            await asyncio.sleep(3)
    names, bad = [], []
    for d in data:
        try:
            await gd_rec_one(app, d, names, GREAL)
        except Exception as e:
            print("[run] recover fail", d.get("sym"), type(e).__name__, e)
            GREAL.hold.append(d)
            bad.append(f"{d.get('sym')} {d.get('dir') or ''}".strip())
    gd_save()
    chat = next((d.get("chat") for d in data if d.get("chat")), None) or CHAT_ID
    L = []
    if names:
        L += [f"{E.BOT} run實盤 已自動恢復(bot 重開)"] + names + ["以 OKX 為準對帳:關機期間的成交、出場、手動平倉會補上、補通知"]
    if bad:
        L += [f"{E.WARN} 以下這次沒查到 OKX,沒有清掉(存檔保留),BOT 每 10 秒再試,恢復了會再通知:"] + bad
    if L and chat:
        await rt_send(app, chat, "\n".join(L + [f"時間:{hhmmss()}"]))
    if GREAL.hold:
        rt_bg(gd_hold_task(app, GREAL))
    for a in rg_accts():
        try:
            await fd_refresh(a)
        except Exception as e:
            print("[run] 重開讀帳戶失敗", a, type(e).__name__, e)
    rg_wake().set()


# ---------- /runt、/stoprunt（v19.0：/run、/stop 共用同一套，gd_cmd、gd_stop_cmd 帶 M） ----------
GD_ASK = ("📝 請輸入 /runt 參數(直接打參數,不用打 /runt)\n"         # v20.0：每一個 bot 都可以下 LSUD（單都放在 o1111o～o5555o）
          "幣種 方向 每層U 埋伏點%\n"
          "例:SUIUSDT LSUD 5U 0.1%\n方向:L、S、U、D 任意組合(LSUD 全部、LSU、S、D…)\n"
          f" L＝💰現貨 L00～L{GD_N:02d}(原點和原點下面)\n"
          f" U＝💰現貨 U01～U{GD_N:02d}(原點上面)\n"
          f" S＝🈴做空 S01～S{GD_N:02d}(原點上面)\n"
          f" D＝🈴做多 D01～D{GD_N:02d}(原點下面)\n"
          f"L、U、D 離原點 {pct(GD_SP_OFF)}% 起每 {pct(GD_STEP)}%(D 跟 L 同價),S {pct(GD_STEP)}% 起每 {pct(GD_STEP)}%\n"
          f"每 {GD_BLK} 層一個帳戶({GD_XACCT[0]}～{GD_XACCT[-1]},現貨、合約一樣),合約每個帳戶第一層 {pct(GD_ALEV)}X、後面同一個強平價(槓桿不用打)\n"
          "同一個幣種一組(幣種數量不限;同一個幣只能在一個 bot 下)\n"
          "進行中的幣改每層U:幣種 每層U(例 SUIUSDT 3U)\n"
          "每層U 要加 U,埋伏點要加 %")
GD_HINT = "SUIUSDT LSUD 5U 0.1%"
GD_RETRY = f"請重新輸入 /runt 參數:\n幣種 方向 每層U 埋伏點%\n例:{GD_HINT}"

GD_STOP_ASK = ["📝 請輸入要停止的幣種,或 all",
               "例:SUIUSDT、all(全部幣種)",
               "停止＝撤掉掛單、不再掛新單;持倉照規則跑到出場為止,全部出場後策略結束(BOT 不平倉)"]
GD_STOP_RETRY = "請重新輸入要停止的幣種,或 all(例 SUIUSDT)"
GD_STOP_HINT = "SUIUSDT 或 all"

def gd_parse(a):
    """/runt 4 個參數：幣種 方向 每層U 埋伏點%（v15.6 拿掉槓桿）。成功回 (dict, None)，失敗回 (None, 錯誤說明)。"""
    a = [x.strip() for x in a]
    if len(a) == 5 and re.fullmatch(r"\d+(?:\.\d+)?[xX]", a[2]):
        return None, (f"槓桿不用打(合約每個帳戶第一層 {pct(GD_ALEV)}X,後面拉到同一個強平價,最高{pct(GD_LCAP)}X),"
                      f"請拿掉 {a[2]}")
    if len(a) != 4:
        return None, f"參數數量錯誤(需4個,收到{len(a)}個)"
    p = {"sym": a[0].upper()}
    if not p["sym"].endswith("USDT") or len(p["sym"]) <= 4:
        return None, f"幣種格式錯誤:{a[0]}(例 SUIUSDT)"
    d = a[1].upper()
    if not d or any(x not in GD_DIRS for x in d) or len(set(d)) != len(d):
        return None, f"方向只能 L、S、U、D 的組合,每個字母最多一次(例 LSUD、LSU、S、D;收到 {a[1]})"
    p["dir"] = d
    m = re.fullmatch(r"(\d+(?:\.\d+)?)[uU]", a[2])
    if not m:
        return None, "每層U 要加 U(例 5U)"
    p["amt"] = Decimal(m.group(1))
    if p["amt"] <= 0:
        return None, "每層U 須大於 0"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)%", a[3])
    if not m:
        return None, "埋伏點要加 %(例 0.1%)"
    p["off"] = Decimal(m.group(1))
    if p["off"] <= 0:
        return None, "埋伏點須大於 0"
    if p["off"] > 20:
        return None, "埋伏點不可超過 20%"
    return p, None

async def cmd_runt(u, c):
    """v15.0 /runt 網格（模擬）：沒帶參數 → 先告訴你參數怎麼打，再等你輸入；打錯 → 回錯誤原因，繼續等你重打。"""
    await gd_cmd(u, c, GSIM)

async def cmd_run(u, c):
    """v19.0 /run 實盤網格：跟 /runt 同一套，送 OKX 真的下單。"""
    await gd_cmd(u, c, GREAL)

def gd_ask_text(M):
    """/runt、/run 的參數說明（v19.0：/run 第一行多「實盤」）。"""
    t = GD_ASK.replace("/runt", f"/{M.cmd}")
    return (f"{E.WARN} 實盤:OKX 真的下單\n" + t) if M.real else t

async def gd_cmd(u, c, M):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    GS = M.D
    fn = cmd_run if M.real else cmd_runt
    a = c.args or []
    if not a:
        await ask(u, M.cmd, fn, "\n".join([gd_ask_text(M), RT_SEP] + gd_running(M) + [ASK_CANCEL]), GD_HINT); return
    retry = lambda msg: ask(u, M.cmd, fn, f"{msg}\n{GD_RETRY.replace('/runt', '/' + M.cmd)}", GD_HINT)
    if len(a) == 2:                                        # v18.4（1111 2026-10-21）：/runt 幣種 每層U＝進行中的幣改每層U
        sym = a[0].strip().upper()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)[uU]", a[1].strip())
        if not m:
            await retry(f"{E.WARN} 改每層U 要打:幣種 每層U(例 {sym} 3U)"); return
        if sym not in GS:
            await retry(f"{E.WARN} {sym} 沒有在跑 {M.cmd},不能改每層U\n要開始請打 4 個參數:幣種 方向 每層U 埋伏點%"); return
        if GS[sym].get("stop"):
            await reply(u, "\n".join(gd_stop_busy_lines(GS[sym]))); return
        await gd_amt_change(u, GS[sym], Decimal(m.group(1)), retry); return
    p, err = gd_parse(a)
    if err:
        await retry(f"{E.WARN} {err}"); return
    sym = p["sym"]
    if sym in GS and GS[sym].get("stop"):                  # v18.5：停止中
        await reply(u, "\n".join(gd_stop_busy_lines(GS[sym]))); return
    if sym in GS:                                          # v18.4（1111 2026-10-21 畫面）
        H = GS[sym]
        await retry("\n".join([f"{E.WARN} {sym} 進行中 {M.cmd}({H['dir']} {pct(H['amt'])}U)", f"要改每層U:/{M.cmd} {sym} {pct(p['amt'])}U",
                                "要換方向/埋伏點", f"請先/{M.stop} {sym}", f"再重新 /{M.cmd}"])); return
    other = gd_other_bot(sym, M)                           # v20.0：6 個 bot 共用 5 個帳戶 → 同一個幣只能在一個 bot 下
    if other:
        await retry("\n".join([f"{E.WARN} /{M.cmd} {M.tag}{sym} {p['dir']} {pct(p['amt'])}U {rt_p2(p['off'])}% 沒有開始",
                                f"{sym} 已經在 {other} 進行中 {M.cmd}", f"同一個幣只能在一個 bot 下({len(GD_XACCT)}個帳戶一起用)"])); return
    has = {x: x in p["dir"] for x in GD_DIRS}
    await gd_spec_fresh(has)                               # v17.0：開始時重查 OKX 規格
    try:
        sp, sw = await gd_specs(sym, has)
    except Exception as e:
        await retry(f"{E.LOSS} {e}"); return
    px = {}
    for kd, x in (("sp", sp), ("sw", sw)):
        if x:
            try:
                px[kd] = await get_last(x["iid"])
            except Exception:
                await retry(f"{E.LOSS} 查不到 {x['iid']} 現價,請稍後再試"); return
    mmr = await rn_mmr({"iid": sw["iid"]}) if sw else None
    if sym in GS:                                          # 查價途中又打了一次
        return
    G = gd_new(p, sp, sw, u.effective_chat.id, time.time(), mmr)
    if M.real:
        rg_init(G)                                         # v19.0：實盤欄位
    okd = gd_okd(G)
    mpx = rt_q(px[okd], G[okd]["tick"])
    o = rt_amb(px["sp"], "L", p["off"], sp["tick"]) if has["L"] else mpx   # 原點：有 L＝L00 的價格；沒有 L＝現價
    bad, info = await gd_check(G, o)                       # v17.0：照實盤檢查，不過就擋
    if not bad and M.real and sw:                          # v19.0：檢查都過了 → 設 OKX 逐倉槓桿（每個帳戶每一邊）
        e = await rg_setlev(G, o)
        if e:
            bad = [(e, ["👉稍後再試,或到 OKX 看那個帳戶這個幣的槓桿設定"])]
        else:
            info = list(info) + rg_lev_lines(G, o)
    if bad:
        await retry("\n".join(gd_reject_lines(G, o, bad))); return
    if sym in GS:                                          # 檢查途中又打了一次
        return
    now = time.time()
    G["t_run"] = now
    for kd in px:
        G["px"][kd] = px[kd]
    if has["L"]:                                           # 有 L：現價下方埋伏點% 掛 💰L00，成交＝原點
        G["o00"] = {"px": o, "t": now, "live": now + RT_LAT} if not M.real else rg_onew(G, "sp", 0, o, now)
    else:                                                  # 沒有 L（1111 2026-10-17）：當下查到的現價直接當原點
        G["org"] = {"px": mpx, "t": now}
    gd_start(c.application, G)
    if M.real and G.get("o00"):
        rg_go_place(G, "o00", G["o00"])                    # v19.0：L00 送 OKX
    print(f"[grid] 開始 {M.tag}{gd_head(G)} 埋伏 {p['off']}%")
    await reply(u, "\n".join(gd_start_lines(G, mpx, now, info)))

def gd_stop_one(G, now):
    """/stoprunt 一組：停止那一刻剛好出場、還沒記帳的照樣記帳。BOT 不平倉。
    v18.5（1111 2026-10-22）：有持倉＝🚦停止中：撤掉所有掛單、不再掛新單，持倉照規則（SL/TP 照常移動）跑到每一單都出場，
      最後一單出場 → 策略結束、通知（gd_tick → gd_stop_end）；沒有持倉＝馬上結束（跟以前一樣）。"""
    for p in sorted([p for p in G["pos"] if p["out"]], key=lambda p: p["out"][0]):
        G["pos"].remove(p)
        gd_done(G, p, gd_money(G, p, p["out"][1]))
    if gd_held(G):
        L = gd_stop_lines(G, now, stopping=True)
        G["stop"] = now
        for cur in G["ord"].values():
            if cur:
                if cur.get("cxl") is None:
                    cur["cxl"] = now + RT_LAT              # 撤單（確認之前碰到照樣成交，成交的那一單也跑到出場為止）
                cur["nxt"] = None
        G["o00"] = None
        gd_save_soon()
        print(f"[grid] 停止中 {gd_head(G)}")
        return L
    L = gd_stop_lines(G, now)
    gd_stop_drop(G)
    print(f"[grid] 停止 {gd_head(G)}")
    return L


def gd_stop_drop(G):
    """這一組拿掉（策略結束）。"""
    G["done"] = True
    tk = G.get("task")
    if tk and tk is not asyncio.current_task():
        tk.cancel()
    D = gd_M(G).D
    if D.get(G["sym"]) is G:
        D.pop(G["sym"], None)
    gd_ws_drop(G)
    gd_save_soon()


def gd_stop_end(G, send, now):
    """v18.5：停止中的組，持倉全部出場、掛單都撤掉了 → 策略結束、通知。"""
    send("\n".join(gd_stop_done_lines(G, now)))
    gd_stop_drop(G)
    if G.get("real"):
        rt_bg(rg_leftover(G))                              # v19.0：OKX 上還有 /run 留下的單 → 撤掉、通知
    print(f"[grid] 停止中 → 全部出場,結束 {gd_M(G).tag}{gd_head(G)}")

async def gd_amt_change(u, G, amt, retry):
    """v18.4（1111 2026-10-21 畫面）：/runt 幣種 每層U（進行中的幣）＝改每層U：原點、格子、槓桿、強平價都不動；
    抱著的單照原本的金額（p["lu"]，SL/TP、損益照舊）；掛單撤掉換成新的金額（照實盤：先撤再掛，gd_hang 下一拍掛新的）；之後新掛的單都用新的。
    新的金額連第一層都下不了＝擋（跟開始時一樣）。累計的 % 之後改成每一單的 % 相加（G["mix"]）。"""
    sym, old = G["sym"], G["amt"]
    if amt <= 0:
        await retry(f"{E.WARN} 每層U 須大於 0"); return
    if amt == old:
        await retry(f"{E.WARN} {sym} 每層U 已經是 {pct(old)}U"); return
    o = (G.get("org") or G.get("o00") or {}).get("px")
    if o is None:
        await retry(f"{E.WARN} {sym} 還沒有原點,稍後再改"); return
    first = [x for x in G["dir"] if not gd_size(G, *gd_first(x), gd_lpx(o, *gd_first(x), G[gd_side_kd(x)]["tick"]), o, amt)["ok"]]
    if first:
        L = [f"{E.WARN} /{gd_M(G).cmd} {sym} 每層U {pct(old)}U→{pct(amt)}U 沒有改", "", "每層U 不夠,第一層下不了"]
        for x in first:
            kd, k = gd_first(x)
            L.append(f" {GD_MK[kd]}{gd_nm(kd, k)} {'買' if kd == 'sp' else '開'}不了(最少{pct(G[kd].get('minsz') or 0)}{'顆' if kd == 'sp' else '張'})")
        mins = [gd_layer_min(G, *gd_first(x), o) for x in G["dir"]]
        if all(v is not None for v in mins):
            L.append(f" 👉每層最少 {max(mins)}U")
        await retry("\n".join(L)); return
    accts = list(GD_XACCT)                                 # v20.0：5 個帳戶
    for a in accts:
        try:
            await fd_refresh(a)
        except Exception as e:
            print("[grid] 改每層U 讀帳戶失敗", a, type(e).__name__, e)
    fd_sim_read()
    now = time.time()
    held = [p for p in G["pos"] if not p["out"]]
    for p in held:
        if p.get("lu") is None:
            p["lu"] = old                                  # 抱著的單照原本的金額
    if not G.get("mix"):
        gd_agg_pc(G, old)
    n_ord = 0
    if G.get("real"):                                      # v19.0：實盤＝OKX 撤單，確認撤掉了下一拍照新的金額再掛（L00 照原價重掛）
        for cur in G["ord"].values():
            if cur and not cur.get("cxl"):
                rg_cancel(G, cur)
                n_ord += 1
        o0 = G.get("o00")
        if o0:
            n_ord += 1
            if rg_onokx(o0) and not o0.get("cxl"):
                rg_cancel(G, o0)
    else:
        for cur in G["ord"].values():
            if cur and cur.get("cxl") is None:
                cur["cxl"], cur["nxt"] = now + RT_LAT, None    # 撤掉，下一拍照新的金額再掛
                n_ord += 1
        o0 = G.get("o00")
        if o0:
            n_ord += 1
            if o0.get("live") is not None:
                o0.update(live=now + 2 * RT_LAT, chk=False); o0.pop("x", None)   # L00 撤掉換成新的金額（再一次 post-only）
    G["amt"], G["mix"] = amt, True
    gd_save_soon()
    lus = sorted({gd_lu(G, p) for p in held}) or [old]
    L = [f"✅ /{gd_M(G).cmd} {gd_M(G).tag}{sym} 每層U {pct(old)}U→{pct(amt)}U", "",
         f"持倉 {len(held)}單 照原本 {'、'.join(pct(v) + 'U' for v in lus)}(SL/TP損益不變)",
         f"掛單 {n_ord}張 撤掉換成 {pct(amt)}U",
         f"之後新掛的單都用 {pct(amt)}U"]
    if G["has"]["S"] or G["has"]["D"]:
        L.append("槓桿/強平價不變(照原點)")
    real = bool(G.get("real"))
    rows = [(a, rg_avail(a) if real else fd_avail(a)) for a in accts]
    vs = rn_pad([f"{v:.2f}U" if v is not None else "查不到" for _, v in rows])
    L += ["", "資金(OKX 可用)" if real else "資金(模擬從真的可用扣)"] + [f"{a} 可用 {x}" + (f"|約夠{int(v // amt)}層" if v is not None else "")
                                                                for (a, v), x in zip(rows, vs)]
    print(f"[grid] 改每層U {gd_M(G).tag}{sym} {old}→{amt}")
    await reply(u, "\n".join(L + ["", f"時間:{hhmmss()}"]))

async def cmd_stoprunt(u, c):
    """v15.0 /stoprunt 幣種｜all。v18.5（1111 2026-10-22）：撤掉掛單、不再掛新單；持倉照規則跑到出場為止（SL/TP 照常移動），
    全部出場後策略結束、通知；沒有持倉＝馬上結束。停止中的幣再打＝已經停止策略中,請勿重複。BOT 不平倉。"""
    await gd_stop_cmd(u, c, GSIM)

async def cmd_stop(u, c):
    """v19.0 /stop 幣種｜all（實盤）：跟 /stoprunt 一樣，撤單送 OKX、等 OKX 確認撤掉。BOT 永遠不平倉。"""
    await gd_stop_cmd(u, c, GREAL)

async def gd_stop_cmd(u, c, M):
    GS = M.D
    fn = cmd_stop if M.real else cmd_stoprunt
    a = [x for x in (c.args or []) if x.strip()]
    if not GS:
        await reply(u, f"{E.BOT} 目前沒有進行中的 {M.cmd}"); return
    run_lines = gd_running(M)
    retry = lambda msg: ask(u, M.stop, fn, "\n".join([msg, GD_STOP_RETRY] + run_lines), GD_STOP_HINT)
    if not a:
        await ask(u, M.stop, fn, "\n".join(GD_STOP_ASK + run_lines + [ASK_CANCEL]), GD_STOP_HINT); return
    if len(a) != 1:
        await retry(f"{E.WARN} 只要打幣種或 all(方向不用打;收到{len(a)}個參數)"); return
    w = a[0].strip().upper()
    if w == "ALL":
        gs = sorted(GS.values(), key=lambda G: G["sym"])
    elif w in GS:
        gs = [GS[w]]
    else:
        await retry(f"{E.WARN} {w} 沒有在跑 {M.cmd}"); return
    if M.real:                                             # v19.0：實盤一組一組撤單、等 OKX 確認
        for G in gs:
            L = gd_stop_dup_lines(G) if G.get("stop") else await rg_stop_one(G)
            if L:
                await reply(u, "\n".join(L))
        gd_save()
        return
    now = time.time()
    outs = [gd_stop_dup_lines(G) if G.get("stop") else gd_stop_one(G, now) for G in gs]   # v18.5：停止中的不重複
    gd_save()
    for L in outs:
        await reply(u, "\n".join(L))

FNG_URL = "https://api.alternative.me/fng/?limit=7"      # v9.7：Crypto Fear & Greed Index（alternative.me，免費、不用 key，每天 08:00 台灣時間更新）
FNG_ZH = {"Extreme Fear": ("😱", "極度恐懼"), "Fear": ("😨", "恐懼"), "Neutral": ("😐", "中性"),
          "Greed": ("😊", "貪婪"), "Extreme Greed": ("🤑", "極度貪婪")}
WEEK_ZH = "一二三四五六日"

async def fng_fetch():
    """向 alternative.me 查最近 7 天的恐懼貪婪指數（最新的在前）。回傳 [(台灣日期, 數值, 英文分級), ...]。"""
    r = await HTTP.get(FNG_URL, timeout=10)
    rows = []
    for d in r.json()["data"][:7]:
        rows.append((datetime.fromtimestamp(int(d["timestamp"]), TZ8), int(d["value"]), d.get("value_classification", "")))
    if not rows:
        raise ValueError("no data")
    return rows

async def fng_lines():
    """v9.7（1111）：/coins 最後的恐懼貪婪指數，近 7 天清單（最新在上）；查不到就一行查詢失敗。"""
    try:
        rows = await fng_fetch()
    except Exception as e:
        print("[coins] fng fail", type(e).__name__, e)
        return ["恐懼貪婪指數 查詢失敗,稍後再試"]
    L = ["恐懼貪婪指數(近7天)"]
    for t, v, cls in rows:
        emo, zh = FNG_ZH.get(cls, ("", cls))
        L.append(f"{t:%m/%d}({WEEK_ZH[t.weekday()]}) {emo} {v} {zh}".replace("  ", " "))
    return L + ["(每天 08:00 更新)"]

COINS_HIDE = ("HYPEUSDT", "XAUUSDT")   # v10.1（1111）：/coins 不列（只是不列，/runt 照樣可以用）；v11.8（1111）：ZECUSDT、PEPEUSDT 加回 /coins
# v10.5（1111）：/coins 多列這 10 個（2026-10-07 查過 OKX 都有 USDT 永續、都在交易中）。跟 symbols.json 合起來、拿掉 COINS_HIDE，照字母排序。
# v11.3（1111 2026-10-10）：加 STXUSDT（查過 OKX：STX-USDT-SWAP 交易中，每張 10 STX、最小 0.1 張、最高 20X）。
# v11.8（1111 2026-10-11）：加 PEPEUSDT、ZECUSDT（查過 OKX 都在交易中、最高 50X；原本在 COINS_HIDE 裡，拿出來）。
# v12.3（1111 2026-10-12）：加 SKYUSDT（查過 OKX：SKY-USDT-SWAP 交易中，每張 100 SKY、最小 1 張、最高 20X）。
# v12.6（1111 2026-10-13）：加 NEARUSDT、STRKUSDT、WUSDT、ZROUSDT、TIAUSDT、FILUSDT（2026-10-13 查過 OKX 都是 USDT 永續、交易中）：
#   NEAR 每張 10 NEAR、最小 0.1 張、最高 50X；STRK 每張 1 STRK、最小 1 張、50X；W 每張 1 W、最小 1 張、20X；
#   ZRO 每張 1 ZRO、最小 1 張、20X；TIA 每張 1 TIA、最小 1 張、50X；FIL 每張 0.1 FIL、最小 1 張、50X。
COINS_ADD = ("PUMPUSDT", "NIGHTUSDT", "ETHFIUSDT", "ENAUSDT", "APTUSDT", "JUPUSDT", "ARBUSDT", "ONDOUSDT", "AEROUSDT", "ASTERUSDT",
             "STXUSDT", "PEPEUSDT", "ZECUSDT", "SKYUSDT",
             "NEARUSDT", "STRKUSDT", "WUSDT", "ZROUSDT", "TIAUSDT", "FILUSDT")

async def coins_acct_lines(acct, tag=""):
    """v10.1：交易帳戶 USDT 四行（當下向 OKX 查），名稱照 OKX：餘額＝cashBal、權益＝eq、可用＝availBal、佔用＝frozenBal。
    v17.0（1111 2026-10-19 畫面）：一個帳戶一段：帳戶 帳戶模式|持倉模式 → 四行 → 費率 現貨 掛單/吃單|合約 掛單/吃單。"""
    e = await fd_refresh(acct)
    if e:
        print("[coins] balance fail", acct, e)
        return [f"{acct}{tag} 查詢失敗({e}),稍後再試"]
    if not FD[acct].get("fee"):
        await fd_fee(acct)
    f = FD[acct]
    fee = f.get("fee") or {}
    fs = lambda kd: "/".join(f"{rt_p2(v)}%" for v in fee[kd]) if fee.get(kd) else "讀不到"
    return (fd_money_lines(acct)                           # v20.0.4／v20.0.5：資金跟 /status 同一個畫面、同一套數字（fd_money）
            + [f"費率 現貨{fs('sp')}", f"{GD_CXL_IND}合約{fs('sw')}"])   # v17.0.1（1111 2026-10-20 排版）：合約另一行，對齊在 現貨 下面

def coins_list():
    """/coins、/amp 共用的幣種清單：symbols.json 有啟用的＋COINS_ADD，拿掉 COINS_HIDE，照字母排序（v11.1 抽出來共用）。"""
    return sorted(({s["symbol"] for s in SYMS if s["enabled"]} | set(COINS_ADD)) - set(COINS_HIDE))

async def coins_spot_last():
    """v17.0：一次查回全部現貨的現價 {instId: 現價}；查不到回 {}。"""
    r = await pub("/api/v5/market/tickers?instType=SPOT")
    out = {}
    for x in r.get("data") or []:
        try:
            if x.get("last"):
                out[x["instId"]] = Decimal(x["last"])
        except Exception:
            pass
    return out

async def coins_mmr(syms):
    """v17.0：每層最少（S 的槓桿）要用的維持保證金率：查過的記著（RN_MMR），沒查過的 8 個一批去查（OKX 公開每 2 秒 20 次，6 個帳戶共用）。"""
    todo = [SPEC_CACHE[s]["iid"] for s in syms if s in SPEC_CACHE and SPEC_CACHE[s]["iid"] not in RN_MMR]
    for i in range(0, len(todo), 8):
        await asyncio.gather(*(rn_mmr({"iid": x}) for x in todo[i:i + 8]))
        if i + 8 < len(todo):
            await asyncio.sleep(1.0)

async def cmd_coins(u, c):
    """/coins。v17.0（1111 2026-10-19 畫面）：每個帳戶一段（帳戶模式、持倉模式、餘額、權益、可用、佔用、費率；v18.0：o1111o 列三個帳戶）；
    每個幣種兩行：現價|最大槓桿|合約最小（OKX 最小下單量的價值）、每層最少 L|U|S|D（現價當原點、33 層全部下得了，0.5U 一級）。
    v17.0.1（1111 2026-10-20 排版）：分 3 則：① 兩個帳戶 ② 每個幣種（幣種之間空一行） ③ 恐懼貪婪指數＋時間。
    v17.0.5（1111 2026-10-20）：第一行 現價|現貨最少幾顆|最大槓桿|合約最小。
    v17.0.2（1111 2026-10-20 排版）：② 拿掉上面兩行說明；每個幣種三行：現價|最大槓桿|合約最小、L33層 | U33層、D33層 | S33層
      ＝那一邊每一層都下得了的最少每層U（0.1U 一級）× 層數＝那一邊全部持倉要準備的錢（L 含 L00＝34 單；v18.0 S、D 照新的槓桿算，錢分在三個帳戶）。"""
    fng = asyncio.ensure_future(fng_lines())                 # v9.7：恐懼貪婪指數跟幣種一起查
    accts = [(a, "") for a in list(GD_XACCT) + ([ACCT] if ACCT not in GD_XACCT else [])]   # v20.0：每個 bot 都列 5 個帳戶（o6666o 多列自己）
    P1 = [f"{E.BOT} OKX原K｜{ACCT}", "事件：幣種清單（即時）", "━━━━━━━━━━"]
    fd_sim_read()                                          # v20.0.4：別的 bot 的模擬佔用、已實現、未實現（跟 /status 一樣）
    for i, (a, tag) in enumerate(accts):
        P1 += (["━━━━━━━━━━"] if i else []) + await coins_acct_lines(a, tag)
    await reply(u, "\n".join(P1))                         # ① 兩個帳戶（先送，幣種算好再送 ②）
    on = coins_list()                                       # v10.5：加 COINS_ADD，不重複；v11.1：跟 /amp 共用
    if not await okx_specs_all():
        await asyncio.sleep(1)
        await okx_specs_all()
    await okx_spot_specs_all()
    lasts = await okx_last_all()
    if lasts is None:
        await asyncio.sleep(1)
        lasts = await okx_last_all()
    lasts = lasts or {}
    spx = await coins_spot_last()
    await coins_mmr(on)
    fee = {f"sp:{a}": ((FD.get(a) or {}).get("fee") or {}).get("sp") or GD_FEE_DEF["sp"] for a in GD_XACCT}   # v20.0：每層最少照那一層的帳戶的費率
    fee.update({a: ((FD.get(a) or {}).get("fee") or {}).get("sw") or GD_FEE_DEF["sw"] for a in GD_XACCT})
    rows = []
    for sym in on:
        sw, sp = SPEC_CACHE.get(sym), SPOT_SPEC.get(sym)
        try:
            last = lasts.get(sw["iid"]) if sw else None
            o = spx.get(sp["iid"]) if sp else None
            o = o if o is not None else last
            if o is None:
                raise ValueError("沒有現價")
            H = {"amt": Decimal(1), "fee": fee, "mmr": RN_MMR.get(sw["iid"]) if sw else None,
                 "sp": dict(sp) if sp else None, "sw": dict(sw) if sw else None}
            mins = {}
            for x in "LUSD":
                if H[gd_side_kd(x)] is None:
                    mins[x] = "沒有現貨" if x in "LU" else "沒有合約"
                else:
                    v = gd_side_min(H, x, o)
                    mins[x] = f"{v * len(gd_side_layers(x)):.2f}U" if v is not None else "不行"   # 33 層的預算（L 含 L00）
            tk = (sp or sw)["tick"]
            spn = f"{Decimal(sp['minsz']).normalize():f}顆" if sp else "沒有現貨"   # v17.0.5（1111 2026-10-20）：現貨最少幾顆（OKX 最小數量）
            head = f"{sym} {rt_q(o, tk)}|{spn}|" + (f"{pct(sw['maxlev'])}X|{sw['minsz'] * sw['ctval'] * (last or o):.2f}U" if sw else "沒有合約")
            rows.append((head, mins))
        except Exception as e:
            print("[coins] fail", sym, type(e).__name__, e)
            rows.append((f"{sym}|查詢失敗", None))
    num = lambda v: v[0].isdigit()                         # 數字才對齊（沒有現貨、不行 照原樣）
    pad = {}
    for c in ("LD", "US"):                                 # 左欄 L、D 一起對齊；右欄 U、S 一起對齊
        vs = [m[x] for _, m in rows if m for x in c if num(m[x])]
        pad.update(zip(vs, rn_pad(vs)))
    cell = lambda m, x: f"{x}{GD_N}層{pad.get(m[x], m[x]) if num(m[x]) else m[x]}"
    pages, cur = [], []                                    # ② 每個幣種：三行一組，幣種之間空一行（太長才分頁，一組不切開）
    for head, m in rows:
        b = [head] + ([f"{cell(m, 'L')} | {cell(m, 'U')}", f"{cell(m, 'D')} | {cell(m, 'S')}"] if m else [])
        if cur and sum(len(x) + 1 for x in cur + b) + 1 > TG_LIM:
            pages.append(cur); cur = []
        cur += ([""] if cur else []) + b
    if cur:
        pages.append(cur)
    for pg in pages:
        await reply(u, "\n".join(pg))
    await reply(u, "\n".join(await fng + ["━━━━━━━━━━", f"時間：{hhmmss()}"]))   # ③ 恐懼貪婪指數

# v15.1（1111 2026-10-18）：/timeframe（/tf）拿掉；/amp 自己帶週期。ACCOUNT_TF 留著固定 5m（舊 /runt、/run 埋伏通知的對齊用）。

# ---------- /amp 振幅分析（v6.3～v9.6；v9.7 刪除；v11.1 1111 2026-10-07 加回） ----------
# v15.1（1111 2026-10-18）：/amp 週期（1m/5m/15m/1h/2h/4h，都是 OKX 原生週期），一次 2000 根（約 1.4/6.9/20.8/83/167/333 天）；
#   沒帶週期＝先問。原本跟著 /tf、10m 用 5m 合成、看幾天（AMP_DAYS）都拿掉。
# 指令（v11.1～v15.0）：/amp（不帶參數）—— 用目前的 /tf 週期，照 /coins 的幣種清單和順序（v11.1：跟 /coins 同一份，coins_list），一個幣種一頁，
#   算好一頁就先送；在背景跑，不卡住其他指令；進行中再按 /amp 回「進行中 第幾個/共幾個」。只查 OKX 公開 K 線，不下單。
# 每一頁：最高振幅（哪一根）、平均振幅、中位振幅（後面是「最高振幅÷它」的倍數）、最高價（哪一根）、⬆️ 現價到最高價%、
#   現價（查詢當下）、⬇️ 現價到最低價%、最低價（哪一根）、區間＝⬆️%＋⬇️%（後面是「區間÷最高振幅」的倍數）。
#   振幅＝每根 (最高−最低) ÷ 前一根收盤（最舊那一根沒有前一根 → 用自己的開盤）。中位＝由小排到大取正中間（雙數取中間兩個平均）。
#   倍數、區間都用畫面上顯示的數字算（1111 自己按計算機會一樣），四捨五入到小數兩位。只用完整、已收線的 K 線。
#   10m 不是 OKX 原生週期 → 用 5m 合成（整點對齊，湊不滿一整根的丟掉）。
# 看幾天（v11.1，1111）：1m 看 2 天（原本 7 天：10080 根、23 個幣種要 10 分鐘，還會擠到查價額度）、3m 7 天、5m／10m 14 天、
#   15m／30m 30 天。根數＝天數÷週期：1m 2880、3m 3360、5m 4032、10m 2016、15m 2880、30m 1440。
# v11.1：分隔符號改半形 |（跟其他畫面一致）。
AMP_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "2h": "2H", "4h": "4H"}   # v15.1：/amp 的週期 → OKX 的 bar
AMP_N = 2000                     # v15.1（1111）：一次 2000 根
AMP_HINT = "1h"
AMP_RUN = {"on": False, "i": 0, "n": 0, "task": None}
AMP_ATH_FILE = "/srv/1111bot/data/amp_ath.json"   # v14.0：歷史高低點（一天查一次，6 個帳戶共用）

def amp_ath_load():
    try:
        with open(AMP_ATH_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def amp_ath_save(sym, v):
    try:
        d = amp_ath_load()
        d[sym] = v
        tmp = f"{AMP_ATH_FILE}.{ACCT}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, AMP_ATH_FILE)
    except Exception as e:
        print("[amp] 歷史高低點存檔失敗", type(e).__name__, e)

async def amp_hist(iid, bar, after="", lim=100, ep="history-candles"):
    """v14.0：K 線一頁（新→舊，含還沒收線的那一根）：[(ts, 高, 低)]。ep＝candles（最近的）／history-candles（更早的）。查不到等一下重試，最多 3 次。"""
    q = f"/api/v5/market/{ep}?instId={iid}&bar={bar}&limit={lim}" + (f"&after={after}" if after else "")
    r = {}
    for i in range(3):
        r = await pub(q)
        if r.get("code") == "0":
            break
        await asyncio.sleep(1.0 + i)
    if r.get("code") != "0":
        raise RuntimeError(f"OKX K線查詢失敗 {r.get('code')} {r.get('msg')}")
    out = []
    for c in r.get("data") or []:
        try:
            out.append((int(c[0]), Decimal(c[2]), Decimal(c[3])))
        except Exception:
            continue
    return out

async def amp_ath_query(iid):
    """v14.0：OKX 上市以來的最高、最低價和那一天：月 K 找哪個月 → 日 K 找哪一天。回傳 {hi, hi_t, lo, lo_t}（t＝毫秒）。"""
    got, after = {}, ""
    for i in range(5):                                     # 一頁 100 個月（第一頁用 candles，含這個月）；最多翻 5 頁
        page = await amp_hist(iid, "1M", after, ep="candles" if i == 0 else "history-candles")
        if not page:
            break
        for x in page:
            got[x[0]] = x
        if len(page) < 100:
            break
        after = str(min(t for t, _, _ in page))
        await asyncio.sleep(0.12)
    months = list(got.values())
    if not months:
        raise RuntimeError("OKX 查不到月 K")
    mh = max(months, key=lambda x: (x[1], -x[0]))
    ml = min(months, key=lambda x: (x[2], x[0]))
    def nxt(ms):                                           # 那個月的下一個月 1 號（台灣時間）
        d = datetime.fromtimestamp(ms / 1000, TZ8)
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return int(d.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    async def day(m, pick):
        await asyncio.sleep(0.12)
        days = [x for x in await amp_hist(iid, "1D", str(nxt(m[0])), 40) if m[0] <= x[0] < nxt(m[0])]
        return pick(days) if days else (m[0], m[1], m[2])
    dh = await day(mh, lambda xs: max(xs, key=lambda x: (x[1], -x[0])))
    dl = await day(ml, lambda xs: min(xs, key=lambda x: (x[2], x[0])))
    return {"hi": str(dh[1]), "hi_t": dh[0], "lo": str(dl[2]), "lo_t": dl[0]}

async def amp_ath(sym, iid, S, last, t_now):
    """v14.0：歷史高低點：今天查過就用存下來的；這次的 K 線（最高價、最低價）或現價創新高／新低 → 馬上換成新的。查不到回 None。"""
    today = rn_today()
    v = amp_ath_load().get(sym)
    if not v or v.get("d") != today:
        try:
            v = {**await amp_ath_query(iid), "d": today}
        except Exception as e:
            print("[amp] 歷史高低點查詢失敗", sym, type(e).__name__, e)
            return None
    hi, lo = Decimal(v["hi"]), Decimal(v["lo"])
    for p, t in ((S["hi"], S["tH"]), (last, int(t_now * 1000))):
        if p > hi:
            hi, v["hi"], v["hi_t"] = p, str(p), t
    for p, t in ((S["lo"], S["tL"]), (last, int(t_now * 1000))):
        if p < lo:
            lo, v["lo"], v["lo_t"] = p, str(p), t
    amp_ath_save(sym, v)
    return v

def amp_n(tf):
    return AMP_N

async def amp_fetch_page(iid, after, ep, bar):
    """抓一頁 K 線（新→舊，只含已收線）。candles 只有最近約 1440 根，抓完改 history-candles 續抓。
    回傳 (list, 下一個 ep, 是否到盡頭, 這一頁最舊那一根的時間)。OKX 回錯誤（多半是太快被限流）→ 等一下重試，最多 3 次。"""
    lim = 300 if ep == "candles" else 100
    q = f"/api/v5/market/{ep}?instId={iid}&bar={bar}&limit={lim}" + (f"&after={after}" if after else "")
    r = {}
    for i in range(3):
        r = await pub(q)
        if r.get("code") == "0":
            break
        await asyncio.sleep(1.0 + i)
    if r.get("code") != "0":
        raise RuntimeError(f"OKX K線查詢失敗 {r.get('code')} {r.get('msg')}")
    batch = r.get("data") or []
    if not batch:
        if ep == "candles" and after:
            return [], "history-candles", False, after
        return [], ep, True, after
    oldest = str(min(int(c[0]) for c in batch))
    out = []
    for c in batch:
        try:
            if len(c) >= 9 and str(c[8]) != "1":
                continue                                   # 還沒收線的那一根不要
            out.append({"ts": int(c[0]), "o": Decimal(c[1]), "h": Decimal(c[2]), "l": Decimal(c[3]), "c": Decimal(c[4])})
        except Exception:
            continue
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out, ep, False, oldest

async def amp_klines(iid, tf, n):
    """最近 n+1 根（多一根當第一根的前收）已收線的 tf K 線（舊→新）；OKX 不夠就回有的。v15.1：週期都是 OKX 原生（AMP_TF）。"""
    bar = AMP_TF[tf]
    need = n + 2
    base = {}; after, ep = "", "candles"
    for _ in range(need // 100 + 30):
        page, ep, done, after = await amp_fetch_page(iid, after, ep, bar)
        if done:
            break
        for x in page:
            base[x["ts"]] = x
        if len(base) >= need:
            break
        await asyncio.sleep(0.12 if ep == "history-candles" else 0.06)   # 不要打太快（OKX 公開額度 6 個帳戶共用）
    return sorted(base.values(), key=lambda x: x["ts"])[-(n + 1):]

def amp_stats(kl, n):
    """kl＝舊→新。回傳分析用的根數與各項數字（同數值取第一次出現的那一根）。"""
    if len(kl) > n:
        prev, rows = kl[0]["c"], kl[1:]
    else:
        prev, rows = kl[0]["o"], kl
    amps = []
    for x in rows:
        amps.append((x["h"] - x["l"]) / prev * 100 if prev else Decimal(0))
        prev = x["c"]
    iA = max(range(len(rows)), key=lambda i: (amps[i], -i))
    iH = max(range(len(rows)), key=lambda i: (rows[i]["h"], -i))
    iL = min(range(len(rows)), key=lambda i: (rows[i]["l"], i))
    sa = sorted(amps); m = len(sa) // 2
    amed = sa[m] if len(sa) % 2 else (sa[m - 1] + sa[m]) / 2
    return {"rows": rows, "n": len(rows), "amax": amps[iA], "tA": rows[iA]["ts"], "aavg": sum(amps) / len(amps), "amed": amed,
            "hi": rows[iH]["h"], "tH": rows[iH]["ts"], "lo": rows[iL]["l"], "tL": rows[iL]["ts"]}

def _amp_t(ms):
    return datetime.fromtimestamp(ms / 1000, TZ8).strftime("%m/%d %H:%M")

def _amp_right(ss):
    """同一欄右對齊：位數不夠的前面補數字寬的空白。"""
    w = max(len(x) for x in ss)
    return [" " * (w - len(x)) + x for x in ss]

async def amp_page(sym, tf, head):
    """一個幣種一頁：成功回完整畫面；查不到回「查詢失敗,跳下一個」。"""
    try:
        spec = await get_spec(sym)
        iid, tick = spec["iid"], spec["tick"]
        n = amp_n(tf)
        kl = await amp_klines(iid, tf, n)
        last = await get_last(iid); t_now = time.time()
    except Exception as e:
        print("[amp] fail", sym, type(e).__name__, e)
        return f"{head}\n💥 {sym} 查詢失敗,跳下一個"
    if not kl:
        return f"{head}\n💥 {sym} 查無K線資料,跳下一個"
    S = amp_stats(kl, n)
    q = lambda v: str(Decimal(str(v)).quantize(tick))
    sA = f"{S['amax']:.3f}"                                 # v14.0（1111）：平均振幅、中位振幅拿掉
    p1, p2, p3 = _amp_right([q(S["hi"]), q(last), q(S["lo"])])
    def rx(a, b):                                          # 倍數＝畫面上的 a ÷ 畫面上的 b，四捨五入到小數兩位
        a, b = Decimal(a), Decimal(b)
        return str((a / b).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if b else "-"
    ath = await amp_ath(sym, iid, S, last, t_now)          # v14.0：歷史高點、歷史低點
    if ath:
        hu = Decimal(f"{(Decimal(ath['hi']) - last) / last * 100:.3f}") or Decimal("0.000")
        ld = Decimal(f"{(last - Decimal(ath['lo'])) / last * 100:.3f}") or Decimal("0.000")
        h1, h2 = _amp_right([f"{hu:.3f}", f"{ld:.3f}"])
        ymd = lambda ms: datetime.fromtimestamp(ms / 1000, TZ8).strftime("%Y/%m/%d")
        hist = [f"歷史高點 {q(Decimal(ath['hi']))}|{ymd(ath['hi_t'])}", f"⬆️ {h1}%",
                f"現　價 {q(last)}|{_amp_t(t_now * 1000)}", f"⬇️ {h2}%", f"歷史低點 {q(Decimal(ath['lo']))}|{ymd(ath['lo_t'])}"]
    else:
        hist = ["歷史高點 查詢失敗", f"現　價 {q(last)}|{_amp_t(t_now * 1000)}", "歷史低點 查詢失敗"]
    up = Decimal(f"{(S['hi'] - last) / last * 100:.3f}")  # 現價要漲多少才到最高價（負＝已經衝過最高價）
    dn = Decimal(f"{(last - S['lo']) / last * 100:.3f}")  # 現價要跌多少才到最低價（負＝已經跌破最低價）
    up = up if up else Decimal("0.000"); dn = dn if dn else Decimal("0.000")
    rg = up + dn
    d1, d2 = _amp_right([f"{up:.3f}", f"{dn:.3f}"])
    cnt = f"{S['n']}根" + ("" if S["n"] >= n else "(OKX只有這些)")
    L = [head,                                             # v14.0（1111 2026-10-15）：橫線改成空白列
         f"{sym}|{tf}|{cnt}",
         f"{_amp_t(S['rows'][0]['ts'])} ~ {_amp_t(S['rows'][-1]['ts'])}",
         "",
         f"最高振幅 {sA}%|{_amp_t(S['tA'])}",
         *hist,
         "",
         f"最高價 {p1}|{_amp_t(S['tH'])}",
         f"⬆️ {d1}%",
         f"現　價 {p2}|{_amp_t(t_now * 1000)}",
         f"⬇️ {d2}%",
         f"最低價 {p3}|{_amp_t(S['tL'])}",
         f"區　間 {rg:.3f}%|{rx(f'{rg:.3f}', sA)}倍",
         "",
         f"時間:{hhmmss()}"]
    return "\n".join(L)

async def _amp_all(u, tf, syms):
    """背景跑：照 /coins 的順序一個幣種一頁，算好一個就先送（不卡住其他指令）。"""
    n = len(syms)
    try:
        for i, sym in enumerate(syms, 1):
            AMP_RUN["i"] = i
            head = f"⚡️ 振幅分析|{ACCT}|{i}/{n}"
            try:
                page = await amp_page(sym, tf, head)
            except Exception as e:
                print("[amp] page fail", sym, type(e).__name__, e)
                page = f"{head}\n💥 {sym} 查詢失敗,跳下一個"
            await reply(u, page)
    finally:
        AMP_RUN["on"] = False

def amp_tf(x):
    """v15.1：/amp 的週期（1m/5m/15m/1h/2h/4h，大小寫都認得：1H、15M）；不對回 None。"""
    x = (x or "").strip().lower()
    return x if x in AMP_TF else None

async def cmd_amp(u, c):
    """v15.1（1111 2026-10-18）/amp 週期：照 /coins 的幣種清單和順序，一個幣種一頁，每一頁 2000 根。沒帶週期 → 先問；打錯 → 繼續等你重打。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args or []
    opts = "/".join(AMP_TF)
    if AMP_RUN["on"]:
        await reply(u, f"{E.BOT} 振幅分析進行中({AMP_RUN['i']}/{AMP_RUN['n']}),請等跑完"); return
    if not a:
        await ask(u, "amp", cmd_amp, f"📝 請輸入 /amp 週期(直接打週期,不用打 /amp)\n可選:{opts}\n一次 {AMP_N} 根K線\n例:{AMP_HINT}\n{ASK_CANCEL}", AMP_HINT); return
    tf = amp_tf(a[0]) if len(a) == 1 else None
    if tf is None:
        await ask(u, "amp", cmd_amp, f"{E.WARN} 週期只能 {opts}(收到 {' '.join(a)})\n請重新輸入週期(例 {AMP_HINT})", AMP_HINT); return
    syms = coins_list()
    if not syms:
        await reply(u, f"{E.BOT} /coins 沒有幣種"); return
    AMP_RUN.update(on=True, i=0, n=len(syms))
    AMP_RUN["task"] = asyncio.create_task(_amp_all(u, tf, syms))

async def cmd_menu(u, c):
    """/menu 說明。v17.0（1111 2026-10-19）：舊 /run、/stop、接續帳戶拿掉；/runt 照實盤規則。v18.0：新策略。"""
    arm = f"最高毛利率 >做多(L、U、D){GD_LONG_ARM:.2f}%／做空(S){RN_ARM:.2f}% 第一次設 SL/TP："
    await reply(u, f"{E.BOT} OKX原K｜{ACCT} {VERSION}\n使用說明\n━━━━━━━━━━\n"
        "/status 所有 /runt（模擬網格）現況；最上面 資金(模擬)＝每個帳戶 可用|佔用\n"
        "/summary 本日戰報（總表＋分幣種）；每天 00:00 自動發前一天的\n"
        "/runt 幣種 方向 每層U 埋伏點%（模擬，不下單）：固定原點隱形網格＝💰現貨做多＋🈴合約做空、做多\n"
        f"　方向：L、S、U、D 任意組合。L＝💰現貨 L00～L{GD_N:02d}（原點和原點下面）、U＝💰現貨 U01～U{GD_N:02d}（原點上面）、"
        f"S＝🈴做空 S01～S{GD_N:02d}（原點上面）、D＝🈴做多 D01～D{GD_N:02d}（原點下面）；"
        f"v20.0 L、U、D 離原點 {pct(GD_SP_OFF)}% 起每 {pct(GD_STEP)}%（D 跟 L 同價）、S {pct(GD_STEP)}% 起每 {pct(GD_STEP)}%\n"
        f"　每 {GD_BLK} 層一個帳戶（現貨、合約一樣）：" + "｜".join(f"{a} {b * GD_BLK + 1:02d}～{(b + 1) * GD_BLK:02d}" for b, a in enumerate(GD_XACCT))
        + "（L00 在第一個帳戶；雙向持倉＋逐倉：每個帳戶的 S、D 各自一個倉位，各自 10 單一起強平）；"
        f"合約每個帳戶第一層 {pct(GD_ALEV)}X，後面拉到跟第一層同一個強平價的最大倍數（最高 {pct(GD_LCAP)}X；算不出來＝跟前一層一樣，例 D48～D50＝D47）\n"
        f"　每一個 bot（o1111o～o6666o）都可以下 LSUD，單都放在 {GD_XACCT[0]}～{GD_XACCT[-1]}；同一個幣只能在一個 bot 下\n"
        "　原點：有 L＝現價下方埋伏點% 掛 💰L00 現貨買，成交價＝🩵原點；沒有 L＝/runt 那一刻的現價直接當原點。原點到 /stoprunt 都不動\n"
        "　掛單：一組最多 3 張：現價下面最近一格（沒有持倉的）💰現貨買、🈴做多；現價上面最近一格 🈴做空；跟著價格換格；一律限價（post-only）\n"
        f"　出場：每一層各自：{arm}SL＝距離現價−{RN_SL_DIST:.2f}%、TP＝距離現價+{RN_TP_FIRST:.2f}%（做空相反）；"
        f"之後 SL＝現價−{RN_SL_DIST:.2f}%（每秒 {round(1 / RN_SL_STEP)} 次）、TP＝現價+{RN_TP_DIST:.2f}%（每秒 {round(1 / RN_TP_STEP)} 次），都只往獲利方向移動；"
        f"SL 最低 +{RN_SL_MIN:.2f}%；碰到＝市價出場；現貨只賣那一層\n"
        "　每層U：現貨每層買 U、合約每層保證金 U\n"
        "　照實盤規則：數量照 OKX 單位往下取（合約保證金照樣每層U）；下的時候檢查 帳戶模式、雙向持倉、那個幣五個帳戶都沒有現貨、合約持倉、掛單、"
        "每一邊的第一層下不下得了、可用夠不夠，不過就擋並告訴你怎麼改（後面每層U 不夠的層＝自動用那一層的最低金額，不擋）；"
        "資金＝真的可用 − 6 個 bot 的模擬佔用 ＋ 模擬已實現（每分鐘讀 OKX、每 5 秒共用），不夠那一張不掛；"
        "手續費照帳戶費率、現貨買進從幣扣（零頭留在帳戶）；post-only 一掛就會成交＝被取消、下一拍重算；換格先撤單、確認撤掉再掛；"
        f"SL/TP 改單照 OKX 額度（每 {pct(GD_AMEND_WIN)} 秒 {GD_AMEND_N} 次，每個帳戶的現貨一份、永續一份），超過時離現價最近的先改\n"
        "　例：/runt SUIUSDT LSUD 5U 0.1%、/runt SUIUSDT LSU 5U 0.1%、/runt SUIUSDT LU 5U 0.1%\n"
        "　v18.4 改每層U（進行中的幣）：/runt 幣種 每層U（例 /runt WLDUSDT 3U）：原點、槓桿、強平價不動；抱著的單照原本的金額；掛單撤掉換成新的金額；之後新掛的都用新的\n"
        "/stoprunt 幣種｜all　停止 /runt：撤掉掛單、不再掛新單；持倉照規則（SL/TP 照常移動）跑到出場為止，全部出場後策略結束、通知；沒有持倉＝馬上結束（BOT 不平倉）\n"
        "/run 幣種 方向 每層U 埋伏點%（v19.0 實盤，OKX 真的下單）：規則跟 /runt 一模一樣；掛單＝OKX post-only、SL/TP＝每一單一張 OKX OCO（移動＝改單）；"
        "成交、出場、手續費、資金費、強平價都查 OKX；合約 OKX 槓桿設成每個帳戶最高那一層、成交後保證金補到每層U（強平價＝設計）；/stop 同 /stoprunt（BOT 不平倉）\n"
        f"/coins 分 3 則：①帳戶（{'、'.join(GD_XACCT)}；o6666o 多列自己）（帳戶模式、持倉模式、餘額、權益、可用、佔用、費率） ②每個幣種（現價、現貨最少幾顆、最大槓桿、合約最小、{GD_N}層預算 L|U|D|S＝每一層都下得了的最少每層U×層數，L 含 L00） ③恐懼貪婪指數\n"
        f"/amp 週期　振幅分析：週期 {'/'.join(AMP_TF)}，一次 {AMP_N} 根K線，照 /coins 的幣種一個一頁\n"
        "/vps VPS 狀況：CPU、限速、記憶體、硬碟；每個帳戶的 CPU、記憶體、WS 斷線、價格延遲\n"
        "/check 健檢｜/check api｜/check log\n"
        f"━━━━━━━━━━\n行情 WS {ws_status()}")

async def cmd_unknown(u, c):
    await reply(u, f"{E.BOT} 指令無法辨識：{u.message.text}\n請用 /menu")

# ---------- 指令輸入引導（v7.5，1111 核可；v8.2 改） ----------
# 手機點選單裡的指令會直接送出、沒辦法帶參數。現在：需要參數的指令（/run /stop /price /runt /stoprunt /tf；v9.7 起 /test1 /stoptest1 刪除）沒帶參數時，
# 先回「參數怎麼打」，鍵盤自動跳出（ForceReply，輸入框有灰色範例），1111 直接打參數送出即可（不用再打指令）。
# 打錯 → 回錯誤原因，繼續等他重打。點任何其他指令就不等了。一次打完整（/runt WLDUSDT LS 1X 1U 0.5%）照樣能用。
# 等待狀態只在記憶體裡，bot 重開就清掉。
# v8.2（1111）：60 秒沒輸入就自動取消，不用打「取消」；逾時會發一行「已自動取消」順便收掉回覆列
#   （以前 ForceReply 沒人回就一直掛著，每次點進聊天室都跳出「回覆…」）。
#   /tf 不帶參數只是查看週期，不再用 ForceReply（不會跳出回覆列）；60 秒內直接打週期照樣能改，逾時不發訊息。
WAIT = {}                   # chat_id -> {"cmd": 指令名, "fn": 收到參數後要呼叫的函式, "t": 開始等的時間, "fr": 有沒有用 ForceReply}
WAIT_SEC = 60               # v8.2：60 秒沒輸入 → 自動取消（原本 5 分鐘）
FR_OPEN = set()             # 還沒收掉回覆列的 chat
ASK_CANCEL = "(60秒沒輸入自動取消)"

async def ask(u, name, fn, text, hint, force=True):
    """回一則說明，鍵盤自動跳出（輸入框灰色範例＝hint），然後等 1111 直接打參數；60 秒沒輸入自動取消。
    force=False：不跳回覆列（/tf 查看週期用），一樣等 60 秒。"""
    chat = u.effective_chat.id
    w = {"cmd": name, "fn": fn, "t": time.time(), "fr": force}
    WAIT[chat] = w
    for i in range(2):
        try:
            if force:
                await u.message.reply_text(text, reply_markup=ForceReply(input_field_placeholder=hint[:64]))
                FR_OPEN.add(chat)
            else:
                await u.message.reply_text(text)
            break
        except Exception as e:
            print("ask fail", i, type(e).__name__, e); await asyncio.sleep(2)
    tk = asyncio.create_task(ask_expire(u.get_bot(), chat, w))
    _BG.add(tk); tk.add_done_callback(_BG.discard)

async def ask_expire(bot, chat, w):
    """v8.2：60 秒後還在等同一個輸入 → 自動取消；有跳回覆列的就發一行通知，順便把回覆列收掉。"""
    await asyncio.sleep(WAIT_SEC)
    if WAIT.get(chat) is not w:
        return                                  # 已經輸入了，或改點了別的指令
    WAIT.pop(chat, None)
    if not w["fr"]:
        return
    for i in range(2):
        try:
            await bot.send_message(chat, f"⌛ /{w['cmd']} {WAIT_SEC}秒沒有輸入,已自動取消", reply_markup=ReplyKeyboardRemove())
            FR_OPEN.discard(chat); return
        except Exception as e:
            print("ask expire fail", i, type(e).__name__, e); await asyncio.sleep(2)

async def wait_clear(u, c):
    """任何指令一進來就先取消正在等的參數（那個指令自己需要的話會再開始等）。"""
    if u.effective_chat:
        WAIT.pop(u.effective_chat.id, None)

async def on_text(u, c):
    """不是指令的文字：正在等參數 → 當成那個指令的參數；沒在等 → 提示先點指令。"""
    chat = u.effective_chat.id
    w = WAIT.pop(chat, None)
    txt = (u.message.text or "").strip()
    if not w or time.time() - w["t"] > WAIT_SEC:
        await reply(u, f"{E.BOT} 沒有在等你輸入參數,請先點指令(/menu)"); return
    if txt in ("取消", "cancel", "CANCEL"):
        await reply(u, f"{E.BOT} 已取消"); return
    c.args = txt.split()
    await w["fn"](u, c)


async def job_summary(ctx):
    """v8.7（1111）：每天台灣時間 00:00 自動發前一天完整 24 小時的 /summary（/runt）；前一天什麼都沒有就不發。"""
    chat = CHAT_ID or next((G["chat"] for G in list(RD.values()) + list(GD.values())), None)
    if not chat: return
    d = (now8() - timedelta(minutes=5)).strftime("%Y-%m-%d")
    try:
        for m in gd_sum_msgs(d, full=True, M=GREAL) + gd_sum_msgs(d, full=True):   # v19.0：/run 實盤在前、/runt 在後
            for part in tg_split("\n".join(m)):           # v10.0：太長分頁
                await ctx.bot.send_message(chat, part)
    except Exception as e: print("auto summary fail", e)

# ---------- 啟動 ----------
async def _post_init(app):
    global HTTP
    HTTP = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0),
                             transport=httpx.AsyncHTTPTransport(local_address=OKX_V4, limits=httpx.Limits(max_connections=40)))   # v15.2：連 OKX 一律走 IPv4
    load_state()              # v10.8：聊天室、/tf 週期（原本在舊 /run 的 startup_recover）
    # v15.0：/runt 換成網格 → v13.1 的模擬平倉旗標（runt_flat_帳戶）不再用（/stoprunt 就是馬上停止）
    # v10.3（1111 2026-10-07）：左下 Menu 順序 status、summary、coins、price、run、stop、runt、stoprunt、timeframe、check、menu
    #   v11.1：timeframe 下面加 amp
    #   （週期改列 /timeframe；/tf 照樣可以打）
    CMDS = [BotCommand("status", "現況"),
            BotCommand("summary", "本日戰報（runt）"),
            BotCommand("coins", "帳戶＋幣種＋恐懼貪婪指數"),
            BotCommand("run", "實盤網格（OKX 真的下單）"),       # v19.0
            BotCommand("stop", "停止run｜幣種或all"),
            BotCommand("runt", "網格（模擬，不下單）"),           # v15.0
            BotCommand("stoprunt", "停止runt｜幣種或all"),
            BotCommand("amp", "振幅分析｜週期"),                 # v15.1：/price、/gap、/timeframe 拿掉
            BotCommand("check", "健檢｜api｜log"),
            BotCommand("menu", "說明"),
            BotCommand("vps", "VPS 狀況")]                      # v13.9
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
            t0010 = datetime.strptime("00:00:10", "%H:%M:%S").time().replace(tzinfo=TZ8)   # v8.7：00:00 發前一天完整的
            jq.run_daily(job_summary, time=t0010, name="daily_summary")
            print("已排程：每日 00:00 自動 /summary（前一天）")
    except Exception as e:
        print("schedule fail", e)
    # WebSocket：逐筆成交價（/runt 判斷成交、SL/TP 出場）。斷線自動降級回 REST 查價。
    asyncio.create_task(ws_public_task())
    rt_bg(health_task())            # v13.9：/vps 用的狀態（每 10 秒寫一次）
    print(f"/runt 移動任務已啟動（{VERSION}）")   # deploy5.sh 用這一行確認版本（v10.8 以前是舊 /run 的 frame_mover 印的）
    await gd_recover(app)     # v15.0：/runt 網格（v14 的 runtlayer 不恢復，改名 .v14）
    if GD_HOLD:
        rt_bg(gd_hold_task(app))    # v15.0：網格這次沒恢復成功的，背景每 10 秒再試
    RG_APP["app"] = app
    rt_bg(rg_sync_task())           # v19.0：/run 實盤：每 0.5 秒向 OKX 對帳
    rt_bg(rg_ws_task())             # v19.0：/run 用到的帳戶各一條私有 WS（只當鬧鐘）
    rt_bg(rg_bal_task())            # v19.0：/run 用到的帳戶每 10 秒讀 OKX 可用
    try:
        await rg_recover(app)       # v19.0：/run 實盤的組接著跑，以 OKX 為準對帳
    except Exception as e:
        print("[run] recover fail", type(e).__name__, e)
    rt_bg(gd_save_task())           # v15.0：網格存檔（最快 1 秒一次、平常 10 秒一次）
    rt_bg(fd_task(app))             # v17.0：每分鐘讀帳戶可用、帳戶模式；每小時查 OKX 規格

async def _post_stop(app):
    global SHUTTING_DOWN
    save_state()          # 關閉前最後一次存檔（聊天室、/tf 週期）
    if GD or RD or GD_SAVE["dirty"]:
        gd_save()         # v15.0：網格最後一次存檔（v19.0：實盤 rungrid 一起）
    SHUTTING_DOWN = True
    print("關閉中：已保存狀態，停止後續寫檔")

def main():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    print(f"啟動 {ACCT} {VERSION}（token ...{TOKEN[-6:]}）")
    app = (Application.builder().token(TOKEN).post_init(_post_init).post_stop(_post_stop)
           .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0)
           .pool_timeout(30.0).get_updates_read_timeout(40.0)
           .get_updates_connect_timeout(30.0).build())
    app.add_handler(MessageHandler(filters.COMMAND, wait_clear), group=-1)    # v7.5：點任何指令 → 取消正在等的參數
    for cmd, fn in [(["menu", "start"], cmd_menu),
                    ("status", cmd_status), ("summary", cmd_summary),
                    ("check", cmd_check),                                     # v15.1：/price 拿掉
                    ("runt", cmd_runt), ("stoprunt", cmd_stoprunt),          # v7.6／v7.7：/runt＝佈局（模擬）
                    ("run", cmd_run), ("stop", cmd_stop),                    # v19.0：/run 實盤網格
                    ("amp", cmd_amp),                                         # v11.1：/amp 加回；v15.1：/gap、/timeframe 拿掉
                    ("coins", cmd_coins), ("vps", cmd_vps)]:            # v13.9：/vps
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, on_text))   # v7.5：直接打參數
    app.run_polling()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# 設計此腳本的目的在於用bot取代我在交易所app上的一切手動行為，切記
"""1111bot｜OKX 多帳戶 Telegram bot（v10.8 起只剩 /runt 佈局模擬）

【現在在用的】
  /runt、/stoprunt：佈局策略（模擬，只查 OKX 價格、不下單）。規則寫在下面「/runt 佈局策略」那一區的註解，
  那就是規格；每一版改了什麼也記在那裡。
  /status、/summary、/coins、/price、/timeframe、/check、/menu。
  /run、/stop：實盤版準備中（v10.8 拿掉舊的 A/B 跨式 /run，Menu 位置保留）。

【鐵則】
  1. OKX 為唯一真相來源：損益、手續費、持倉，實盤一律查 OKX；OKX 沒有的才由 BOT 算。
  2. BOT 永遠不平倉；有持倉時由 1111 自己到 OKX 平倉。
  3. Telegram 與 WebSocket 皆為旁路：失效不影響策略本身。

【部署】GitHub 網頁上傳 run_bot.py → VPS：bash /srv/1111bot/deploy5.sh <行數> <版本>（還原：rollback）。
  五個帳戶（o2222o～o6666o）共用這一份檔案，ACCT 由 systemd 的 EnvironmentFile 注入。
"""
import sys, hmac, base64, hashlib, json, time, asyncio, os, re, builtins
from collections import deque
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP
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


VERSION = "v10.8"     # 腳本版本號：回報問題時請附上（/status 最後一行顯示）
BASE = "https://www.okx.com"
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

async def api(method, path, body=None):
    b = json.dumps(body) if body else ""
    ts = ts_now()
    h = {"OK-ACCESS-KEY": ACC[f"OKX_{ACCT}_API_KEY"],
         "OK-ACCESS-SIGN": sign(ACC[f"OKX_{ACCT}_SECRET"], ts, method, path, b),
         "OK-ACCESS-TIMESTAMP": ts,
         "OK-ACCESS-PASSPHRASE": ACC[f"OKX_{ACCT}_PASSPHRASE"],
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
# 【為什麼需要】/runt 的成交、SL/TP 出場、最高毛利率都看逐筆成交價；REST 輪詢在「幾秒衝 2~3%」的針上
# 只有幾個樣本，WS trades 逐筆推送才不會漏。
# 【旁路原則】WS 掛掉自動降級回 REST 查價（1 秒一次，rt_px）。
WS_PUB_URL = "wss://ws.okx.com:8443/ws/v5/public"

WS_PX   = {}        # iid -> (Decimal 價格, epoch 時間戳)
WS_LIVE = {"pub": False}
WS_WANT = set()     # 目前需要訂閱的 iid
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
    if RN:
        rn_on_tick(iid, d)      # 【v7.6】/runt 模擬：逐筆成交價判斷成交、SL/TP 出場


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

def tg_pack(title, blocks, tail=(), lim=TG_LIM):
    """v10.0：把一段一段（blocks＝每段幾行）整段裝進每一頁，不把同一段切開（一段本身就超過才逐行切）；
    分成兩頁以上時，每頁標題後面加 (1/N)，tail（例：時間）放最後一頁。"""
    budget = lim - len(title) - 10
    size = lambda ls: sum(len(x) + 1 for x in ls)
    pages, cur = [], []
    for b in blocks:
        b = list(b)
        while size(b) > budget:                            # 一段就超過：逐行切
            if cur:
                pages.append(cur); cur = []
            k, n = 0, 0
            while k < len(b) and n + len(b[k]) + 1 <= budget:
                n += len(b[k]) + 1; k += 1
            k = max(k, 1)
            pages.append(b[:k]); b = b[k:]
        if cur and size(cur) + size(b) > budget:
            pages.append(cur); cur = []
        cur += b
    tail = list(tail)
    if cur and size(cur) + size(tail) > budget:
        pages.append(cur); cur = []
    cur += tail
    pages.append(cur)
    if len(pages) == 1:
        return ["\n".join([title] + pages[0])]
    return ["\n".join([f"{title}({i}/{len(pages)})"] + p) for i, p in enumerate(pages, 1)]

def tg_split(t, lim=TG_LIM):
    """v10.0：一則太長 → 第一行當標題，在分隔線（━━━）處切成好幾頁；不長就原樣一則。"""
    if len(t) <= lim:
        return [t]
    lines = t.split("\n")
    segs, cur = [], []
    for ln in lines[1:]:
        if ln == TG_SEP and cur:
            segs.append(cur); cur = [ln]
        else:
            cur.append(ln)
    if cur:
        segs.append(cur)
    return tg_pack(lines[0], segs, (), lim)


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
    """/status：每個 /runt 組一段（持倉中＝持倉通知、埋伏中＝埋伏通知，資金費率當下向 OKX 查）。
    v10.0：太長分頁，一組不切開。v10.8：舊 /run 的部分拿掉。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    rn_blocks = rn_status_blocks(await rn_status_fund())
    blocks = [[RT_SEP] + b for b in rn_blocks] or [[RT_SEP, "目前沒有進行中的幣種"]]
    for part in tg_pack(f"📊 status｜{ACCT}", blocks, [RT_SEP, f"時間:{hhmmss()}|{VERSION}"]):
        await reply(u, part)


async def cmd_summary(u, c):
    """v8.7（1111）：/summary＝/runt 的本日損益（總表＋分幣種）。v10.8：舊 /run 的戰報拿掉。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    for m in await rn_sum_msgs(rn_today()):
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
             "order-algo": (20, 2), "cancel-algos": (20, 2),
             "orders-algo-pending": (20, 2), "orders-pending": (60, 2)}


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
    live = sum(1 for T in RN.values() for p in T["pos"] if not p["out"])
    out.append(f"🟢 runt　　{len(RN)} 組｜持倉 {live} 單｜週期 {ACCOUNT_TF}")
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
RT_REST_GAP = 1.0           # WS 沒有新鮮報價時，REST 查價最快 1 秒一次（5 個帳戶共用 REST 額度 20 次/2 秒）
RT_SAVE_SEC = 10            # 改價次數每 10 秒存檔一次
RT_BG = set()               # 背景工作（保留參照，避免中途被回收）
RT_SEP = "━━━━━━━━━━"


def rt_bg(coro):
    """背景執行（不卡住查價迴圈）。"""
    t = asyncio.create_task(coro)
    RT_BG.add(t); t.add_done_callback(RT_BG.discard)
    return t

def rt_t(t_epoch):
    return datetime.fromtimestamp(t_epoch, TZ8).strftime("%H:%M:%S")

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

def rt_hms(sec):
    """時間長度 hh:mm:ss。"""
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

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

def rt_note_base(t0):
    """埋伏通知對齊這一輪的「初次」：00:05:00、00:10:00 …（bot 重開後也一樣對齊）。"""
    iv = TF_SEC.get(ACCOUNT_TF, 300)
    return t0 + max(0, (time.time() - t0) // iv) * iv


async def rt_px(T):
    """現價：WS 逐筆成交價優先（不吃 REST 額度）；WS 3 秒沒有新報價才走 REST，而且最快 1 秒一次。"""
    v = WS_PX.get(T["iid"])
    now = time.time()
    if v and now - v[1] < 3.0:
        return v[0]
    if now - T.get("rest_t", 0) < RT_REST_GAP:
        return None
    T["rest_t"] = now
    try:
        return await get_last(T["iid"])
    except Exception:
        return None


# ---------- /price 價格階梯（v8.3 /test2；v9.7 改名 /price） ----------
# 指令：/price 幣種　例：/price WLDUSDT（只查現價、列出價格，不掛單、不下單、不跑迴圈）。
# 畫面（1111 定案：跟 /runt 同方向，價格由高到低）：S9～S0／現價／L0～L9。
#   S0、L0 離現價 0.5%，之後每一層離上一層 0.5%（S 往上取 tick、L 往下取 tick，往離現價遠的方向）。
T2_STEP = Decimal("0.5")
T2_N    = 10                       # S0～S9、L0～L9
T2_HINT = "WLDUSDT"
T2_ASK  = "📝 請輸入 /price 幣種(直接打幣種,不用打 /price)\n例:WLDUSDT"

def t2_ladder(px, tick):
    """S0～S9、L0～L9 的價格：從現價開始，每一層＝上一層 ±0.5%。"""
    S, L = [], []
    s = l = px
    for _ in range(T2_N):
        s = rt_amb(s, "S", T2_STEP, tick); l = rt_amb(l, "L", T2_STEP, tick)
        S.append(s); L.append(l)
    return S, L

def t2_lines(sym, px, tick):
    S, L = t2_ladder(px, tick)
    out = [f"📣 price｜{ACCT}", sym, RT_SEP]
    out += [f"S{i} {rt_q(S[i], tick)}" for i in range(T2_N - 1, -1, -1)]
    out += [f"現價 {rt_q(px, tick)}"]
    out += [f"L{i} {rt_q(L[i], tick)}" for i in range(T2_N)]
    return out + [RT_SEP, f"時間:{hhmmss()}"]

async def cmd_price(u, c):
    """/price（v9.7 由 /test2 改名）：沒帶參數 → 先問幣種；打錯 → 回錯誤原因，繼續等你重打。"""
    a = c.args or []
    if not a:
        await ask(u, "price", cmd_price, f"{T2_ASK}\n{ASK_CANCEL}", T2_HINT); return
    retry = lambda msg: ask(u, "price", cmd_price, f"{msg}\n請重新輸入 /price 幣種(例 WLDUSDT)", T2_HINT)
    if len(a) != 1:
        await retry(f"{E.WARN} 參數數量錯誤(需1個,收到{len(a)}個)"); return
    sym = a[0].strip().upper()
    if not sym.endswith("USDT") or len(sym) <= 4:
        await retry(f"{E.WARN} 幣種格式錯誤:{a[0]}(例 WLDUSDT)"); return
    try:
        spec = await get_spec(sym)
    except Exception:
        await retry(f"{E.LOSS} 找不到幣種 {sym}"); return
    tick = spec["tick"]
    try:
        px = rt_q(await get_last(spec["iid"]), tick)
    except Exception:
        await retry(f"{E.LOSS} 查不到 {sym} 現價,請稍後再試"); return
    await reply(u, "\n".join(t2_lines(sym, px, tick)))


# ---------- /runt 佈局策略（模擬，v8.2） ----------
# v7.6 → v7.7 → v7.8 → v7.9 → v8.0（1111 2026-10-04 核可）：/runt＝新「佈局模式」。
#   v10.8（1111 2026-10-07 清理）：拿掉舊 /run、/stop、/confirm（A/B 跨式）整套（下單、緊貼引擎 frame_mover、私有 WS、對帳、
#     出場報告、交易紀錄、重開接管），/check 的 sl、rule、data，/log /selftest /test2 別名，/test1、v7.2 一次性搬家程式，
#     v10.7 後沒用的合倉函式，app.strategy.normal 的載入。/run、/stop 留在 Menu，回「實盤準備中」。
#     存檔 strategies_{ACCT}.json 只剩聊天室和 /tf 週期（load_state／save_state）。/runt 行為、畫面、存檔都沒變。6332 → 約 2,550 行。
#   v10.7（1111 2026-10-07「先分開計算」）：強平改回每一層各自算（v9.3～v9.7 的算法：每一層自己的保證金、槓桿、預估強平價，
#     碰到 → 那一層出場原因「強平」、損失那一層保證金，發一般出場通知；其他層照常）。v9.8 的整個方向一起強平拿掉
#     （OKX 分倉說明是合併算爆倉，實盤真的強平時再用數據分析調整）。/status 表沒設 SL 的那一層「未設SL」→「📍+5.42%」
#     （現價到這一層強平價；1X 做多＝📍無）；/status、/stoprunt、出場通知的合倉三行拿掉，持倉中和本日已實現之間空一行。
#   v10.6（1111 2026-10-07）：/status、/stoprunt、輸入提示的進行中清單、/summary 套牢明細與分幣種：幣種一律照字母排序
#     （同幣種 L 在前、0 層往上）。只改畫面順序，策略、損益都不變。
#   v10.5（1111 2026-10-07）：/coins 加 PUMP、NIGHT、ETHFI、ENA、APT、JUP、ARB、ONDO、AERO、ASTER（COINS_ADD），照字母排序。其他不變。
#   v10.4（1111 2026-10-07）：/coins 再拿掉 PEPEUSDT（只是不列，/runt 照樣可以用）。其他不變。
#   v10.3（1111 2026-10-07）：TG 左下 Menu 順序改成 status、summary、coins、price、run、stop、runt、stoprunt、timeframe、check、menu
#     （週期列 /timeframe，/tf 照樣可以打）。其他不變。
#   v10.2（1111 2026-10-07）：級距槓桿改名 XX1～XX5：XX→XX1（1X…10X）、YX→XX2（2X…20X）、ZX→XX5（5X…50X），
#     新增 XX3（3X…30X）、XX4（4X…40X）；第 n 層＝(n+1)×倍數，幣種最高槓桿要夠 L9（30X、40X）。舊名稱打了提示新名稱。
#     做法 A（維持保證金率 1% 估算）：XX3 十層都碰得到（L9 進場後離強平約 0.8%）；XX4 到 L8（36X）後約 0.4% 就整個方向強平，L9 碰不到。
#     存檔記倍數（1～5），在跑的組重開後自動顯示新名稱。其他規則不變。
#   v10.1（1111 2026-10-07）：① 套牢明細（出場通知、/summary）：XX／YX／ZX 每一行加這一層的槓桿（S3 4X），
#     % 是 U÷每單保證金，越上層槓桿越大、倉位越大，所以 XX 的套牢 % 是山形、不是階梯（不是算錯；÷槓桿就是 0.5% 的階梯）。
#     ② 本輪結束／🚦 策略結束：拿掉「各單淨損益」一單一行。本輪結束＝這一輪：期間、歷時、出場原因、各層出場、這一輪已實現；
#     策略結束＝整個策略（/runt 開始～結束、所有輪，從逐單紀錄 runtlog 取最後 st.n 筆）：開始、結束、歷時｜輪數｜出場單數、
#     整個策略已實現、出場統計（出場原因、平均持倉、最長持倉、最高毛利率、最好、最差）、各層出場；後面照舊接本日已實現。
#     ③ /coins：最前面加交易帳戶(USDT) 餘額／權益／可用／佔用（當下查 OKX）；HYPEUSDT、XAUUSDT、ZECUSDT 不列（/runt 照樣可用）。
#   v10.0（1111 2026-10-06）：Telegram 單則上限 4096 字，o2222o 組數多時 /status 超過上限整則被拒收（沒有回應）→
#     所有回覆、通知、00:00 自動 /summary：超過 3800 字自動分頁，每頁標題加 (1/N)；/status、/stoprunt 以組為單位，不把同一組切開。
#   v9.9（1111 2026-10-06）：浮動槓桿加 YX（L0 2X、L1 4X…L9 20X）、ZX（L0 5X、L1 10X…L9 50X），幣種最高槓桿要 ≥20X／≥50X 才能用；
#     T["xx"]＝倍數（0 固定、1 XX、2 YX、5 ZX；舊存檔 true＝XX）。ZX 照做法 A，L7（40X）成交後再跌約 0.36% 就整個方向強平，L8、L9 碰不到（1111 知道）。
#     /coins 改成 幣種｜最小保證金(1X)｜最大槓桿。
#   v9.8（1111 2026-10-05）：照 OKX 逐倉的「合倉」算（同幣種同方向＝一組，風險、保證金、強平都合起來算）：
#     做法 A（OKX 還沒實測，先這樣算）：XX 每加一層，整個方向改成那一層的槓桿（只往上），合倉保證金＝倉位合計÷目前槓桿；
#     預估強平價用合倉算，碰到 → 整個方向一起強平，損失全部合倉保證金（按倉位大小分攤到每一單），合成一則「強平通知」。
#     持倉中下面加三行：合倉 倉位|均價|槓桿、保證金|收益率（OKX：未實現收益÷保證金）、預估強平價(距現價%)。
#     % 照舊＝U÷每單保證金（可以相加）。出場通知「持倉 3:16|SL移動0次|TP移動0次」。v9.7：刪 /tune /amp /test1 /stoptest1、/test2→/price、/coins 恐懼貪婪。
#   v9.6（1111 2026-10-05）：L、S 完全獨立：一組策略＝幣種＋方向（RN key＝'DOGEUSDT L'），各有自己的槓桿、保證金、埋伏點、
#     輪次、300 秒、🚦。/runt 幣種 L／S 開一組，LS＝一次開兩組（同參數），開完各打各的；不再「一邊成交撤另一邊」，
#     同幣種可以同時有 L、S 持倉（實盤用 OKX 雙向持倉）。同幣種同方向只能一組（🚦 打完前也不能重開）；
#     LS 其中一邊已經有 → 整個擋下來（防呆，避免併倉）。/stoprunt 每一組各自停；資金費結算每一組各發一則。
#     v9.5 以前的存檔（一個幣種一筆）重開時拆成 L、S 兩組。v9.4 的 🚦L3（單向 🚦 標在每一張單）拿掉，🚦 標在每一組的幣種前面。
#   v9.5（1111 2026-10-05）：框架 SL＝最高 −0.20%（v9.3～v9.4 是 −0.25%，太遠）、TP＝最高 +0.15% 不變，框寬 0.35%。
#     出場通知「持倉 41分59秒|SL移動0次|TP移動0次」：加 TP 移動次數（框架形成後 TP 每變一次算一次，形成那一次不算），數字前後不空格。
#   v9.4（1111 2026-10-05 定案）：/stoprunt 幣種 方向（LS 兩邊、L 只停多、S 只停空，方向一定要打）、/stoprunt all（全部幣種、兩邊）。
#     每一個要停的方向：沒有進場 → 馬上停止（還沒成交的 L0/S0 撤掉）；有進場 → 🚦 最後一輪（這一輪照常作戰、層單照掛，
#     那一邊全部出場後「🚦 L 策略結束」，等 300 秒後只掛另一邊）。另一個方向照常一輪接一輪。
#     T["dir"]＝還在跑的方向，T["last"]＝🚦 最後一輪的方向（v9.3 是 True/False）；兩個方向都停＝整個幣種（🚦WLDUSDT，跟 v9.3 一樣）。
#     畫面：單向 🚦 標在那一邊每一張單前面（🚦L0、🚦L3），標題方向照列（WLDUSDT LS），/status 多一行「🚦L 最後一輪,全部出場後只剩 S」。
#   v9.3（1111 2026-10-05 定案）：
#     • /runt 幣種 方向 槓桿 保證金 初始埋伏點%：方向 LS＝兩邊、L＝只做多、S＝只做空；
#       槓桿 1X～100X＝每一層都一樣；XX＝L0 1X、L1 2X…L9 10X（只有槓桿跟著層數加，每一層保證金一樣；幣種最高槓桿 <10X 擋下）。
#     • 損益 % 照 OKX「收益率」＝ ÷ 保證金（1X 時＝價格漲跌%）；毛利率（最高／最低毛利率、SL/TP 規則）仍是價格漲跌%。
#     • 模擬強平：照 OKX 逐倉預估強平價（維持保證金率向 OKX position-tiers 查第 1 檔），碰到 → 出場原因「強平」，損失整筆保證金。
#     • 框架：SL＝最高 −0.25%、TP＝最高 +0.15%（保本點 >0.20%、框架點 >0.30% 不變）。（v9.5 SL 改回 −0.20%）
#     • /stoprunt：有持倉 → 🚦 最後一輪（層單照掛、照常作戰），L0/S0 與全部持倉出場後「🚦 策略結束」，不再開新的一輪；
#       沒有持倉（埋伏中、等 300 秒）→ 馬上停止。BOT 永遠不平倉（1111 自己去 OKX 平倉）；🚦 結束前同一幣種不能再 /runt。
#       將來 /run 實盤：要每 0.25 秒巡邏 OKX 持倉／掛單，發現 1111 手動平倉了要跟著更新。
#     • 畫面上「商品」一律改成「幣種」（只剩這一行註解有「商品」）。
#     • 層單是 maker 限價單：掛單價已經被穿過（一掛就成交，例如那一層剛被強平、價格還在下面）→ 先不掛，等價格回到外面再掛。
#   v8.2：9 層改成每一層離上一層 0.5%；損益畫面分「持倉中(未實現)」「本日已實現」，同幣種同方向（L、S 分開）；
#         本日＝台灣時間 00:00～24:00（以出場時間算），00:00 發前一天的本日結算；
#         資金費（模擬）：向 OKX 查資金費率，結算那一刻還持倉的單各自記一次（持倉價值×費率；正＝多付空、負＝空付多），算進淨損益。
#   v8.1：最高毛利率 >0.20% 才設 SL（SL＝最高 −0.10%、最低 0.10%；TP＝最高 +0.20%），最高用逐筆成交價，SL 至少離現價 1 跳；
#         某一層出場 → 撤掉比它後掛（更深）的掛單；本輪結束後等 60 秒才重新取價埋伏；/status＝持倉通知；畫面拿掉「保本」。
#   v8.0：層數由 5 層改成 9 層（L1～L9 離 L0 進場價 1%、2%、3.5%、5%、7%、10%、14%、19%、25%）。
#   v7.9：畫面名稱一律半形：初始單＝L0/S0，層單 L1～L9/S1～S9；出場通知標題只寫「出場通知 幣種 L1 1X 1U」，出場原因移到出場那一行。模擬＝BOT 自己模擬、只查價格，完全不下單。
#   將來移轉到 /run 才是真實下單；到時候損益、手續費一律向 OKX 查詢，不得自行計算（1111 最嚴格的要求）。
#   （/test 是另一個指令，不受影響。）
# 指令：/runt 幣種 方向 槓桿 保證金 初始埋伏點%　　例：/runt WLDUSDT LS 1X 1U 0.5%、/runt WLDUSDT L XX1 1U 0.5%（v10.2 起 XX1～XX5）
#   方向 LS／L／S；槓桿一定要有 X（或 XX）、保證金一定要有 U、初始埋伏點一定要有 %。（v9.2 以前沒有方向參數）
#   同一帳戶同幣種同方向只能一組（避免併倉；v9.6 起 L、S 各自一組）；可同時跑多個幣種。
# 停止：/stoprunt 幣種 方向、/stoprunt all（有持倉＝🚦 最後一輪，v9.3；方向 v9.4）。
# 規則（1111 定案）：
#   ① 初始單：取價 → 上下各距離「初始埋伏點%」限價掛單（L 在下、S 在上），不帶 TP/SL，掛了不改價。
#      成交＝逐筆成交價碰到掛單價，進場價＝掛單價。（v9.5 以前：一邊成交 → 先撤掉另一邊；v9.6 起 L、S 各自一組，不再撤另一邊）
#   ② 9 層（v8.2）：從初始單進場價開始，每一層＝上一層價格再 0.5%（L 往下、S 往上，往離現價遠的方向取 tick），
#      L9 約離 L0 4.41%、S9 約 4.59%。（v8.0～v8.1：1%、2%、3.5%、5%、7%、10%、14%、19%、25%；更早 5 層 1%～5%）
#      初始單一進入虧損（毛利率 < 0）就立刻限價掛 L1/S1；L1/S1 成交後也進入虧損就掛 L2/S2，依此類推（每 0.25 秒查價判斷）。
#      一般化：某一層持倉中且毛利率 < 0，而下一層沒有持倉、也沒有掛單 → 掛下一層。所以某一層出場後，
#      如果它的上一層還在虧損，會再掛回原位。掛出去就不撤（初始單又回到獲利也照掛），直到初始單出場。
#      v8.1（1111）：某一層出場 → 比它後掛的（更深的）掛單一律撤掉；L0/S0 出場 → 這一邊的掛單全部撤掉。
#      每一層的保證金都跟初始單一樣；槓桿也一樣，XX 例外（v9.3：L0 1X、L1 2X…L9 10X）。
#   ③ v9.0（1111 2026-10-05 定案，兩個重點邏輯）：
#        保本點：最高毛利率 > 0.20% → SL 先卡住 +0.10%（沒有 TP），在 0.20%～0.30% 之間 SL 不動；
#        框架點：最高毛利率 > 0.30% → 框架形成：SL＝最高 −0.20%、TP＝最高 +0.15%（v9.5，寬 0.35%；v9.3～v9.4 −0.25%／+0.15%；v9.0～v9.2 −0.20%／+0.20%），之後一起往獲利方向移動（只進不退）。
#        SL 至少離現價 1 跳、送出後過 RT_LAT 生效、逐筆碰到就出場、往虧損走就抱著——都跟以前一樣。吃不到大行情再調遠框架點（RN_BOX）。
#      以下是 v8.1～v8.9 的舊規則（留著對照）：
#   ③ 每一單（初始單、L1～L9）各自出場，規則都一樣（毛利率＝價格漲跌%，不乘槓桿）：
#        最高毛利率 > 0.20% 才設 SL/TP：SL＝最高毛利率 −0.10%（最低 0.10%）、TP＝最高毛利率 +0.20%，
#        之後每 0.25 秒照同一個公式一起往獲利方向移動（只進不退）。（v8.1：原本 >0.10% 就設 SL 0.10%，SL 一設就貼在現價上，
#        一回頭就出場、移動不到。1111 核可的對照表：+0.15%→無、+0.22%→SL 0.12%/TP 0.42%、+0.40%→0.30%/0.60%）
#        最高毛利率＝逐筆成交價記錄的最高（0.25 秒內的高點也不漏）。SL 至少離現價 1 跳（OKX 實盤也不接受設在現價上）；
#        價格已經回落、離 0.10% 不到 1 跳時，這次不設（不動），等下一次。
#        SL、TP 送出後過 RT_LAT 才生效；生效後逐筆成交價碰到就出場，出場價＝那一筆成交價（含滑價）。往虧損走就一直抱著。
#   ④ 初始單出場 → 撤掉這一邊還沒成交的層單；已持倉的層單照自己的 SL/TP 走完；全部出場 → 本輪結束
#      → 等 300 秒（v9.2；v8.1～v9.1 是 60 秒）→ 用現價重新來過。
#   ⑤ 通知（v8.6，1111）：有事件發生才通知——進場成交、出場通知、本輪結束、資金費結算。
#      v8.7：00:00 每個幣種的本日結算拿掉，改成 00:00 自動發前一天完整的 /summary（總表＋分幣種）；
#      每一張出場記一行到 runtlog_{ACCT}.jsonl（/summary 出場統計用，保留 30 天）。
#      v8.8：出場統計的「最差」改成「最深套住」；拿掉「另有 N 單…」備註。
#      v8.9：持倉中下面加「套牢明細」；「最差」＝套牢明細裡淨損益最低的那一單（跟最好同格式）；段落之間空一行。
#      v9.1：出場通知也加「套牢明細」（同幣種同方向），持倉中／本日已實現／套牢明細之間空一行，拿掉最後一行「(L0還在,繼續)」。
#      v9.2：所有損益段落都列手續費（毛損益／手續費／資金費／淨損益）；淨損益＝畫面上的毛損益−手續費＋資金費；本輪結束後等 300 秒。
#      不再每個 /tf 發持倉通知／埋伏通知；/status 隨時看（持倉中＝持倉通知、埋伏中＝埋伏通知的內容，
#      都有資金費率兩行，/status 當下向 OKX 查，v8.5）。/test1 的埋伏通知不變（照 /tf）。
#      L0/S0 掛上去之後不改價、不重掛，等到一邊成交（1111 2026-10-05：維持現狀）。
#   ⑥ 手續費（模擬）：0.07%＝限價進場 0.02%（maker）＋市價出場 0.05%（taker）。
#      每一單：損益 %＝損益 U ÷ 保證金（v9.3，OKX 收益率；v9.2 以前 ÷ 名目價值）。名目價值＝保證金×這一層的槓桿。
#      合計（持倉中、本日已實現、本日結算）：v8.4 起 % 和 U 都是各單直接相加（1111：0.16%＋0.12%＝0.28%，不是平均）。
#      淨損益＝毛損益 − 手續費 ＋ 資金費（v8.2；資金費＋收、−付）。持倉中＝假設現在用市價平倉。
#   ⑧ 資金費（模擬，v8.2）：每 60 秒查 OKX /public/funding-rate（結算前 15 秒內再查一次）；到結算時間，
#      結算前就進場、還沒出場的單：資金費＝數量×現價×費率（費率正：多單付、空單收；負：空單付、多單收）。
#      用結算前最後查到的費率、當下成交價（實盤 OKX 用標記價格，差很小）。關機期間的結算不補算。
#   ⑦ bot 重開：持倉、層單掛單接著跑（關機期間沒有看盤）；埋伏中的用重開當下的現價重新來過。
RN_STEP    = 0.25              # 每秒查價 4 次（固定）
RN_ARM     = Decimal("0.20")   # v9.0 保本點：最高毛利率 > 0.20% → SL 先卡在 +0.10%（還沒有 TP）
RN_SL_MIN  = Decimal("0.10")   # 保本點的 SL 位置 +0.10%（也是 SL 最低）
RN_BOX     = Decimal("0.30")   # v9.0 框架點：最高毛利率 > 0.30% → 框架形成（1111：吃不到大行情再調遠這個）
RN_SL_GAP  = Decimal("0.20")   # 框架：SL＝最高毛利率 −0.20%（v9.5，1111：−0.25% 太遠；v9.3～v9.4 −0.25%，v9.0～v9.2 −0.20%，v8.1～v8.9 −0.10%）
RN_TP_GAP  = Decimal("0.15")   # 框架：TP＝最高毛利率 +0.15%（v9.3 起；v9.5 框寬 0.35%）
RN_MMR_DEF = Decimal("0.01")   # v9.3 強平：查不到 OKX 維持保證金率時先用 1%（OKX 多數幣種第 1 檔）
RN_WAIT    = 300               # 本輪結束後等 300 秒才重新取價埋伏（v9.2，1111；v8.1～v9.1 是 60 秒）
RN_NLV     = 9                 # 9 層：L1～L9／S1～S9
RN_LV_STEP = Decimal("0.5")    # v8.2：每一層離上一層的價格 0.5%（L 往下、S 往上；往離現價遠的方向取 tick）
RN_FEE_IN  = Decimal("0.02")   # 手續費 0.07%＝限價進場 0.02%
RN_FEE_OUT = Decimal("0.05")   #              ＋市價出場 0.05%
RN_FUND_Q   = 60              # v8.2 資金費：每 60 秒向 OKX 查一次資金費率（公開資料，不用 API key、不下單）
RN_FUND_PRE = 15              #            結算前 15 秒內再查一次，拿最接近結算的費率
RN_FILE    = f"/srv/1111bot/data/runtlayer_{ACCT}.json"   # 不可用 runt_{ACCT}.json（那是 v7.2 撤單保險會讀的檔）
RN_DAY_FILE = f"/srv/1111bot/data/runtday_{ACCT}.json"    # 本日已實現（同幣種同方向，v8.2）
RN_DAY_KEEP = 30               # 保留最近 30 天
RN_LOG_FILE = f"/srv/1111bot/data/runtlog_{ACCT}.jsonl"   # v8.7：每一張出場一行（/summary 出場統計、日後數據分析），保留 30 天
RN_DAY = {}                    # 日期 -> 幣種 -> L/S -> 損益累計
RN = {}                        # 幣種 -> 參數＋狀態（見 rn_new）
RN_SL_WAIT = "SL 未設(毛利率 >0.20% 才設)"


def rn_ls(x):
    """L、S 照固定順序排好：'SL'→'LS'。"""
    return "".join(c for c in "LS" if c in (x or ""))

def rn_key(sym, side):
    """v9.6（1111：完全獨立）：一組策略＝幣種＋方向，RN 的 key＝'DOGEUSDT L'。"""
    return f"{sym} {side}"

def rn_whole(T):
    """v9.6：這一組是 🚦 最後一輪（/stoprunt 時有持倉；全部出場後策略結束、這一組拿掉）。"""
    return bool(T.get("last"))

def rn_sym(T):
    """v9.3：最後一輪（/stoprunt 時有持倉）幣種前面加 🚦。v9.6：每一組各自標。"""
    return f"🚦{T['sym']}" if rn_whole(T) else T["sym"]

def rn_groups():
    """v9.6：畫面上的順序：同幣種排在一起，L 在前。
    v10.6（1111：同時跑很多幣種，比較好辨識）：幣種照字母排序（原本是照開始的先後）。只影響畫面順序。"""
    return sorted(RN.values(), key=lambda T: (T["sym"], T["dir"]))

def rn_lev(T, lvl):
    """v9.3：這一層的槓桿。XX＝第 n 層 n+1 倍（L0 1X、L1 2X…L9 10X）；否則每一層一樣。每一層保證金都一樣。
    v9.9：YX＝2 倍（L0 2X…L9 20X）、ZX＝5 倍（L0 5X…L9 50X）。T["xx"]＝倍數（0＝固定槓桿）。
    v10.2：改名 XX1～XX5（倍數 1～5，XX3、XX4 新增），算法不變。"""
    return Decimal(lvl + 1) * int(T["xx"]) if T.get("xx") else T["lev"]

def rn_levs(T):
    return RN_XNAME.get(int(T["xx"]), f"XX{int(T['xx'])}") if T.get("xx") else f"{pct(T['lev'])}X"

def rn_plev(T, p):
    return p.get("lev") or rn_lev(T, p["lvl"])

def rn_liq(T, side, ent, lev):
    """v9.3 預估強平價（OKX 逐倉的算法）：做多＝進場價×(1−1/槓桿)÷(1−維持保證金率)；做空＝進場價×(1+1/槓桿)÷(1+維持保證金率)。
    往離進場價近的方向取 tick（保守）。1X 做多不會強平（None）。"""
    mmr = T.get("mmr") or RN_MMR_DEF
    if side == "L":
        if lev <= 1:
            return None
        return align(ent * (1 - 1 / lev) / (1 - mmr), T["tick"], "S")
    return align(ent * (1 + 1 / lev) / (1 + mmr), T["tick"], "L")

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


def rn_dur(sec):
    """v9.8（1111）：持倉時間用 3:16、1:03:16（不寫分秒，畫面窄一點）。"""
    sec = max(0, int(sec))
    h, m, x = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"

def rn_head(T):
    return f"{rn_sym(T)} {T['dir']} {rn_levs(T)} {pct(T['amt'])}U"

def rn_par(T):
    return f"{rn_head(T)} 埋伏{rt_p2(T['off'])}%"

def rn_nm(T, side, lvl):
    """畫面上的名稱（v7.9：一律半形）：初始單 L0/S0，層單 L1～L9/S1～S9。（v9.4 的 🚦L3 在 v9.6 拿掉：🚦 標在每一組的幣種前面）"""
    return f"{side}{lvl}"

def rn_lvx(T, lvl, lev=None):
    """v10.1：XX／YX／ZX 每一層槓桿不同 → 名稱後面加槓桿（S3 4X）；固定槓桿只寫 S3（槓桿在標題）。lev＝那一單實際的槓桿。"""
    if not T.get("xx"):
        return ""
    return f" {pct(lev if lev is not None else rn_lev(T, lvl))}X"

def rn_nmx(T, p):
    """v10.1：一張單的名稱＋（XX／YX／ZX 才有的）槓桿。"""
    return rn_nm(T, p["side"], p["lvl"]) + rn_lvx(T, p["lvl"], rn_plev(T, p))

def rn_title(side, lvl):
    """標題上的名稱（同 rn_nm）。"""
    return f"{side}{lvl}"

def rn_init_names(T, live_only=False):
    """這一輪的初始單名稱：L0、S0（兩張都成交＝L0、S0）。"""
    if live_only:
        sides = sorted({p["side"] for p in T["pos"] if p["lvl"] == 0 and not p["out"]})
    else:
        sides = sorted(T["init"])
    return "、".join(f"{s}0" for s in sides)

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

def rn_lay_px(side, e0, lvl, tick):
    """第 lvl 層掛單價（v8.2）：從 L0/S0 進場價開始，每一層＝上一層價格 L 往下 0.5%（往下取 tick）、S 往上 0.5%（往上取 tick）。"""
    px = e0
    for _ in range(lvl):
        px = rt_amb(px, side, RN_LV_STEP, tick)
    return px

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

def rn_notional(T, n=1):
    return T["amt"] * T["lev"] * n

def rn_q2(v):
    """% 照畫面取到 0.01%（合計＝各單畫面上的數字直接相加）。"""
    return Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def rn_q4(v):
    """U 照畫面取到 0.0001U。"""
    return Decimal(v).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

def rn_money(T, p, px):
    """以 px 出場（市價）時的 毛損益U、手續費U、淨損益U 與對應的 %。
    v9.3（1111：照 OKX）：% ＝ ÷ 保證金（OKX 的「收益率」）；1X 時跟價格漲跌%一樣。名目價值＝保證金×這一層的槓桿。
    強平：損失全部保證金（毛損益＝−保證金），手續費只有開倉那筆。"""
    margin = T["amt"]
    notional = margin * rn_plev(T, p)
    if p.get("out") and p["out"][2] == "強平":
        gross_u = -(p.get("liqm") or margin)               # v9.8：合倉強平，每一單損失分攤到的保證金
        fee_u = notional * RN_FEE_IN / 100
    else:
        gross_u = p["qty"] * ((px - p["ent"]) if p["side"] == "L" else (p["ent"] - px))
        fee_u = notional * RN_FEE_IN / 100 + p["qty"] * px * RN_FEE_OUT / 100
    fu = p.get("fu", Decimal(0))                           # v8.2 資金費（＋收、−付），已在每次結算時記到這一單
    # v9.2（1111）：毛損益、手續費、資金費先照畫面取整（% 到 0.01、U 到 0.0001），淨損益＝毛損益−手續費＋資金費，
    # 畫面上每一段自己加都對得上（原本各自四捨五入，會出現 0.72%−0.07%＝0.64% 的情況）。
    g, fee, fund = rn_q2(gross_u / margin * 100), rn_q2(fee_u / margin * 100), rn_q2(fu / margin * 100)
    gu, feeu, fuq = rn_q4(gross_u), rn_q4(fee_u), rn_q4(fu)
    return {"g": g, "fee": fee, "net": g - fee + fund, "gu": gu, "feeu": feeu, "netu": gu - feeu + fuq,
            "fu": fuq, "fund": fund, "fn": p.get("fn", 0)}

def rn_acc():
    """一組損益合計（v8.4，1111：合計＝各單相加，例 0.16%＋0.12%＝0.28%）：
    單數、毛損益%／淨損益%／資金費% 各單相加（各單先照畫面取到 0.01%）、毛損益U／淨損益U／資金費U 各單相加（先取到 0.0001U）、資金費次數。"""
    z = Decimal(0)
    return {"n": 0, "gp": z, "np": z, "fp": z, "gu": z, "u": z, "fu": z, "fn": 0}

def rn_acc_add(a, m):
    """m＝rn_money 的結果（一單）。"""
    a["n"] += 1
    a["gp"] += rn_q2(m["g"]); a["np"] += rn_q2(m["net"]); a["fp"] += rn_q2(m["fund"])
    a["gu"] += rn_q4(m["gu"]); a["u"] += rn_q4(m["netu"]); a["fu"] += rn_q4(m["fu"]); a["fn"] += m["fn"]

def rn_acc_load(x):
    x = x or {}
    D0 = lambda k: Decimal(x.get(k, "0"))
    a = {"n": int(x.get("n", 0)), "gu": D0("gu"), "u": D0("u"), "fu": D0("fu"), "fn": int(x.get("fn", 0))}
    if "gp" in x:
        a.update({"gp": D0("gp"), "np": D0("np"), "fp": D0("fp")})
    else:                                            # v8.2～v8.3 的記錄只有 U 和名目價值合計：每單名目價值一樣 → 各單 % 相加＝U ÷ 一單名目價值
        k = Decimal(a["n"]) / D0("nt") * 100 if D0("nt") else Decimal(0)
        a.update({"gp": a["gu"] * k, "np": a["u"] * k, "fp": a["fu"] * k})
    return a

def rn_acc_dump(a):
    return {k: (v if isinstance(v, int) else str(v)) for k, v in a.items()}

def rn_today(t=None):
    """本日＝台灣時間（UTC+8）00:00～24:00。"""
    return datetime.fromtimestamp(time.time() if t is None else t, TZ8).strftime("%Y-%m-%d")

def rn_day(sym, side, d=None):
    """本日已實現（同幣種同方向；/stoprunt 再 /runt 也接著算）。"""
    return RN_DAY.setdefault(d or rn_today(), {}).setdefault(sym, {}).setdefault(side, rn_acc())

def rn_done(T, p, m):
    """一單出場 → 算進本輪、本日（依出場時間，台灣時間；v8.2：同幣種同方向分開記）。"""
    T["st"]["n"] += 1
    T["st"]["u"] += m["netu"]
    T["st"]["gu"] += m["gu"]
    rn_acc_add(rn_day(T["sym"], p["side"], rn_today(p["out"][0])), m)
    rn_log_add(T, p, m)
    T["rnd"].append({"side": p["side"], "lvl": p["lvl"], "ent": p["ent"], "t_in": p["t_in"],
                     "t_out": p["out"][0], "net": m["net"], "netu": m["netu"], "gu": m["gu"], "fu": m["fu"], "why": p["out"][2],
                     "g": m["g"], "fund": m["fund"], "fn": m["fn"], "mfe": rn_q2(p["mfe"]), "lev": rn_plev(T, p)})   # v10.1：統計用

def rn_pl(title, a, fee=True):
    """損益一段：標題＋毛損益、淨損益（% 與 U 各自對齊）。a＝rn_acc 格式的合計。
    v8.4（1111）：% 與 U 都是各單直接相加（原本 % 是 ÷ 名目價值合計＝平均，兩單 0.16%、0.12% 會顯示 0.14%）。
    有結算過資金費、而且取整後不是 0 才多一行資金費（＋收、−付；淨損益已含；v8.7：+0.00%|+0.0000U 不列）。
    fee＝手續費一行（＝淨損益−毛損益−資金費）；v9.2（1111）：所有段落都列手續費，順序 毛損益／手續費／資金費／淨損益。"""
    fd = a["fn"] and (rn_q2(a["fp"]) != 0 or rn_q4(a["fu"]) != 0)
    rows = ([("毛損益", a["gp"], a["gu"])]
            + ([("手續費", a["np"] - a["gp"] - a["fp"], a["u"] - a["gu"] - a["fu"])] if fee else [])
            + ([("資金費", a["fp"], a["fu"])] if fd else []) + [("淨損益", a["np"], a["u"])])
    x = rn_pad([rn_sp(r[1]) for r in rows])
    y = rn_pad([rn_su(r[2]) for r in rows])
    return [title] + [f"{r[0]} {p}|{q}" for r, p, q in zip(rows, x, y)]

def rn_pl_side(T, side, px):
    """同一方向：持倉中(未實現)、本日已實現（沒有的那段不列）。v8.2：1111 定案只看本日，不列累計。
    持倉中＝假設現在用 px 市價平倉，含進場 0.02%＋出場 0.05% 手續費。"""
    L = []
    live = [p for p in T["pos"] if p["side"] == side and not p["out"]]
    if live and px is not None:
        a = rn_acc()
        for p in live:
            rn_acc_add(a, rn_money(T, p, px))
        L = rn_pl(f"{side} 持倉中 {len(live)}單(未實現)", a)   # v10.7（1111）：合倉三行拿掉（每一層各自強平）
    d = ((RN_DAY.get(rn_today()) or {}).get(T["sym"]) or {}).get(side)
    if d and d["n"]:
        L = rn_join([L, rn_pl(f"{side} 本日已實現 {d['n']}單", d)])   # v10.7：跟持倉中之間空一行
    return L

def rn_pl_secs(T, px, sides=None):
    """各方向一段，用分隔線隔開（這個方向什麼都沒有就不列）。v9.6：預設只列這一組的方向。"""
    L = []
    for s in sides or (T["dir"],):
        x = rn_pl_side(T, s, px)
        if x:
            L += [RT_SEP] + x
    return L

def rn_log_add(T, p, m):
    """v8.7：一張出場記一行（JSON），/summary 的出場統計用，也留給 1111 做數據分析。"""
    try:
        t_in, (t_out, px, why) = p["t_in"], p["out"]
        r = {"d": rn_today(t_out), "sym": T["sym"], "side": p["side"], "lvl": p["lvl"], "lev": str(rn_plev(T, p)), "amt": str(T["amt"]),
             "ent": str(p["ent"]), "px": str(px), "t_in": round(t_in, 3), "t_out": round(t_out, 3), "hold": round(t_out - t_in, 1),
             "why": why, "n_mv": p["n_mv"], "n_tp": p.get("n_tp", 0), "mfe": str(rn_q2(p["mfe"])), "mae": str(rn_q2(p["mae"])),
             "gp": str(rn_q2(m["g"])), "np": str(rn_q2(m["net"])), "fp": str(rn_q2(m["fund"])),
             "gu": str(rn_q4(m["gu"])), "u": str(rn_q4(m["netu"])), "fu": str(rn_q4(m["fu"])), "fn": m["fn"]}
        with open(RN_LOG_FILE, "a") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[runt] log fail", e)

def rn_log_read(d):
    """v8.7：讀某一天（台灣時間）的逐單出場紀錄。"""
    out = []
    try:
        if os.path.exists(RN_LOG_FILE):
            with open(RN_LOG_FILE) as f:
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

def rn_log_prune():
    """v8.7：bot 重開時只留最近 30 天的逐單紀錄。"""
    try:
        if not os.path.exists(RN_LOG_FILE):
            return
        keep = (now8() - timedelta(days=RN_DAY_KEEP)).strftime("%Y-%m-%d")
        with open(RN_LOG_FILE) as f:
            lines = [x for x in f if x.strip() and (json.loads(x).get("d") or "") >= keep]
        tmp = RN_LOG_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(lines)
        os.replace(tmp, RN_LOG_FILE)
    except Exception as e:
        print("[runt] log prune fail", e)

def rn_join(blocks):
    """v8.9（1111）：幾段之間空一行，比較好閱讀（空的那段不列）。"""
    out = []
    for b in blocks:
        if b:
            out += ([""] if out else []) + b
    return out

async def rn_sum_msgs(d, full=False):
    """v8.7 /summary（1111 定案）：第一則總表、第二則分幣種。d＝台灣時間的日期；full＝00:00 自動發前一天完整 24 小時。
    已實現＝本日記錄（RN_DAY）；持倉中＝現在還抱著的單用現價算（假設現在市價平倉）；出場統計＝逐單紀錄（runtlog）。
    v8.9（1111）：持倉中下面加「套牢明細」（每一張還抱著的單，淨損益）；「最差」＝套牢明細裡淨損益最低的那一單
    （一定是某個幣種的 L0/S0，跟「最好」同格式）；段落之間空一行（分幣種也是）。
    什麼都沒有：full＝不發（回傳 []）；手動 /summary＝回一則「本日沒有出場的單,也沒有持倉」。"""
    day = RN_DAY.get(d) or {}
    hold = {}                                                # (幣種, L/S) -> rn_acc
    rows = []                                                # 套牢明細：(幣種, L/S, 層, 淨損益%, 淨損益U, 進場時間)
    for T in list(RN.values()):
        live = [p for p in T["pos"] if not p["out"]]
        if not live:
            continue
        px = T.get("px_last") or (WS_PX.get(T["iid"]) or (None,))[0]
        if px is None:
            try:
                px = await get_last(T["iid"])
            except Exception:
                continue
        for p in live:
            m = rn_money(T, p, px)
            rn_acc_add(hold.setdefault((T["sym"], p["side"]), rn_acc()), m)
            rows.append((rn_sym(T), p["side"], p["lvl"], rn_q2(m["net"]), rn_q4(m["netu"]), p["t_in"],
                         rn_lvx(T, p["lvl"], rn_plev(T, p))))     # v9.3／v9.6：🚦 那一組；v10.1：XX／YX／ZX 加槓桿
    def tot(accs):
        a = rn_acc()
        for x in accs:
            for k in a:
                a[k] += x[k]
        return a
    R = tot([a for v in day.values() for a in v.values()])
    H = tot(hold.values())
    recs = rn_log_read(d)
    span = f"{d[5:7]}/{d[8:10]} 00:00~{'24:00' if full else hhmmss()[:5]}(台灣時間)"
    head = [f"📊 summary｜{ACCT}", span]
    foot = [RT_SEP, f"時間:{hhmmss()}|{VERSION}"]
    if not R["n"] and not H["n"]:
        return [] if full else [head + [RT_SEP, "本日沒有出場的單,也沒有持倉"] + foot]
    one = lambda sym, side, lvl, np_, u, lx="": f"{sym} {side}{lvl}{lx} {rn_sp(np_)}|{rn_su(u)}"
    # 套牢明細：v10.6（1111）幣種照字母排序（原本虧最多的在前），同一個幣種 L 在前、由 0 層往上；🚦 不影響排序
    rows.sort(key=lambda r: (r[0].lstrip("🚦"), r[1], r[2], r[5]))
    L = head + [RT_SEP, "全部合計"]
    L += rn_join([rn_pl(f"已實現 {R['n']}單", R, fee=True) if R["n"] else [],
                  rn_pl(f"持倉中 {H['n']}單(未實現)", H) if H["n"] else [],
                  (["套牢明細"] + [one(*r[:5], r[6]) for r in rows]) if rows else [],
                  [f"{'24:00' if full else '現在'}全部平倉試算", f"淨損益 {rn_sp(R['np'] + H['np'])}|{rn_su(R['u'] + H['u'])}"]
                  if H["n"] else []])
    if recs:
        why = {w: sum(1 for r in recs if r.get("why") == w) for w in ("SL", "SL(移動)", "TP", "強平")}
        if not why["強平"]:                                  # v9.3：有強平才列
            why.pop("強平")
        avg = sum(float(r.get("hold") or 0) for r in recs) / len(recs)
        b = max(recs, key=lambda r: (Decimal(r.get("np") or "0"), Decimal(r.get("u") or "0")))
        bw = [f"最好 {one(b['sym'], b['side'], b['lvl'], Decimal(b['np']), Decimal(b['u']))}"]
        if rows:
            w = min(rows, key=lambda r: (r[3], r[4]))
            bw.append(f"最差 {one(*w[:5], w[6])}")
        L += [RT_SEP] + rn_join([[f"出場統計({len(recs)}單)",
                                  "出場原因 " + "|".join(f"{k} {v}" for k, v in why.items()),
                                  f"平均持倉 {rt_hold_str(avg)}"], bw])
    L += foot
    M = [f"📊 summary｜{ACCT} 分幣種", span]
    syms = set(day) | {k[0] for k in hold}
    for sym in sorted(syms):                                 # v10.6（1111）：分幣種照字母排序（原本賺最多的在前）
        blocks = []
        for side in ("L", "S"):
            h = hold.get((sym, side))
            if h:
                blocks.append(rn_pl(f"{side} 持倉中 {h['n']}單(未實現)", h))
            a = (day.get(sym) or {}).get(side)
            if a and a["n"]:
                blocks.append(rn_pl(f"{side} 本日已實現 {a['n']}單", a))
        M += [RT_SEP, sym] + rn_join(blocks)
    M += foot
    return [L, M]

def rn_day_save():
    try:
        keep = sorted(RN_DAY)[-RN_DAY_KEEP:]
        for k in list(RN_DAY):
            if k not in keep:
                RN_DAY.pop(k)
        data = {d: {sym: {s: rn_acc_dump(a) for s, a in v.items()} for sym, v in x.items()} for d, x in RN_DAY.items()}
        tmp = RN_DAY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, RN_DAY_FILE)
    except Exception as e:
        print("[runt] day save fail", e)

def rn_day_load():
    try:
        if os.path.exists(RN_DAY_FILE):
            for d, x in json.load(open(RN_DAY_FILE)).items():
                RN_DAY[d] = {sym: {s: rn_acc_load(a) for s, a in v.items()} for sym, v in x.items()}
    except Exception as e:
        print("[runt] day load fail", e)

def rn_sp4(v):
    """資金費率：帶正負號、4 位小數的 %：+0.0100%。"""
    v = Decimal(v).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return f"{'-' if v < 0 else '+'}{abs(v):.4f}%"

def rn_su6(v):
    """資金費金額很小：帶正負號、6 位小數的 U。"""
    v = Decimal(v).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return f"{'-' if v < 0 else '+'}{abs(v):.6f}U"

def rn_fund_who(rate):
    return "多單付給空單" if rate > 0 else ("空單付給多單" if rate < 0 else "不收不付")

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

def rn_fund_settle(T, px):
    """v8.2：到了結算時間 → 這一刻還持倉的單（結算前就進場、還沒出場）各自記一次資金費。
    資金費＝持倉價值（數量×現價）×資金費率；費率正：多單付、空單收；費率負：空單付、多單收。回傳通知內容（沒有持倉＝None）。"""
    f = T["fund"]
    t, rate = f["t"], f["rate"]
    f["last"], f["t"], f["q"] = t, None, 0.0              # 馬上再查下一期
    rows = []
    for p in T["pos"]:
        if p["t_in"] >= t or (p["out"] and p["out"][0] <= t):
            continue
        val = p["qty"] * px
        amt = -val * rate if p["side"] == "L" else val * rate
        p["fu"] = p.get("fu", Decimal(0)) + amt
        p["fn"] = p.get("fn", 0) + 1
        rows.append((p, val, amt))
    if not rows:
        return None
    rows.sort(key=lambda r: (r[0]["side"], r[0]["lvl"], r[0]["t_in"]))
    vs = rn_pad([f"{v.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)}U" for _, v, _ in rows])
    am = rn_pad([rn_su6(a) for _, _, a in rows])
    tot = sum((a for _, _, a in rows), Decimal(0))
    L = [f"💰資金費結算 {rn_head(T)}",
         RT_SEP,
         f"結算 {rt_t(t)}|費率 {rn_sp4(rate * 100)}",
         f"({rn_fund_who(rate)})",
         RT_SEP]
    L += [f"{rn_nm(T, p['side'], p['lvl'])} 價值 {v}|{'收' if a > 0 else '付'} {x}" for (p, _, a), v, x in zip(rows, vs, am)]
    L += [RT_SEP,
          f"合計 {'收' if tot > 0 else '付'} {rn_su6(tot)}",
          "(已算進各單淨損益)",
          f"時間:{hhmmss()}"]
    return L

def rn_fund_fmt(rate, t, note=None):
    """資金費率兩行（v8.3，1111 指定格式）；note＝查詢失敗時的說明。"""
    L = [f"💰資金費率 {rn_sp4(rate * 100)}({'多付空' if rate > 0 else ('空付多' if rate < 0 else '不收付')})",
         f"下次付費時間 {rt_t(t)[:5]}"]
    return L + ([note] if note else [])

def rn_fund_line(T):
    """持倉通知、埋伏通知用：bot 每 60 秒查到的資金費率、下次結算時間（還沒查到就不列）。"""
    f = T.get("fund") or {}
    if f.get("t") is None or f.get("rate") is None:
        return []
    return rn_fund_fmt(f["rate"], f["t"])

async def rn_fund_now(T):
    """v8.5 /status：當下向 OKX 查資金費率（公開資料）。只拿來顯示，不改 bot 結算資金費用的資料（避免剛好在結算那一刻錯過結算）。
    OKX 沒回應 → 用 bot 最近一次查到的，註明幾點查的；連那個都沒有 → 查詢失敗。"""
    try:
        r = await asyncio.wait_for(pub(f"/api/v5/public/funding-rate?instId={T['iid']}"), 5)
        d = r["data"][0]
        rate, t = Decimal(d["fundingRate"]), int(d["fundingTime"]) / 1000
        if t < time.time() and d.get("nextFundingTime"):    # OKX 剛結算完、還沒換到下一期 → 下次＝再下一期
            t = int(d["nextFundingTime"]) / 1000
        return rn_fund_fmt(rate, t)
    except Exception as e:
        print("[runt] status funding fail", T.get("sym"), type(e).__name__, e)
    f = T.get("fund") or {}
    if f.get("t") is not None and f.get("rate") is not None:
        return rn_fund_fmt(f["rate"], f["t"], f"(OKX 沒回應,這是 {rt_t(f['qt'])} 查到的)")
    return ["💰資金費率 查詢失敗,稍後再試"]

def rn_pos(T, side, lvl, ent, t, px_hit):
    """一張單成交 → 一個持倉（各自獨立：自己的進場價、SL/TP、最高/最低毛利率）。"""
    g0 = rn_g(side, ent, px_hit)
    lev = rn_lev(T, lvl)
    if T.get("xx") and lev > (T.get("lev_now") or 0):       # v9.8 做法 A：整個方向改成這一層的槓桿
        T["lev_now"] = lev
    return {"side": side, "lvl": lvl, "ent": ent, "t_in": t, "qty": T["amt"] * lev / ent,
            "lev": lev, "liq": rn_liq(T, side, ent, lev),
            "sl": None, "tp": None, "sl_g": None, "tp_g": None, "pend": None,
            "t_arm": None, "arm_px": None, "n_mv": 0, "n_tp": 0, "hi": None, "fu": Decimal(0), "fn": 0,
            "mfe": max(g0, Decimal(0)), "mae": min(g0, Decimal(0)), "told": False, "out": None}

def rn_round(T, t0, px0):
    """開始新的一輪：取價 → 初始單上下埋伏（不帶 TP/SL，不改價）。"""
    T["lev_now"] = None                                    # v9.8：新的一輪從 L0 的槓桿重來
    T.update({"t0": t0, "px0": px0, "cxl_due": None, "nxt": 0.0, "note_last": rt_note_base(t0),
              "init": {}, "ords": [], "rnd": [], "h0": None, "wait_until": None,
              "legs": {s: {"side": s, "px": rt_amb(px0, s, T["off"], T["tick"]), "fill": None, "dead": False}
                       for s in ("S", "L") if s in T.get("dir", "LS")}})       # v9.3：方向 L／S 只掛那一邊

def rn_new(p, spec, chat):
    return {**p, "key": rn_key(p["sym"], p["dir"]), "chat": chat, "iid": spec["iid"], "tick": spec["tick"],
            "ev": asyncio.Event(), "done": False, "rest_t": 0.0, "save_t": time.time() + RT_SAVE_SEC,
            "pos": [], "legs": {}, "ords": [], "rnd": [], "init": {}, "h0": None, "cxl_due": None, "px_last": None,
            "wait_until": None, "last": False, "dir": p["dir"], "xx": int(p.get("xx") or 0), "mmr": p.get("mmr"),
            "st": {"r": 0, "n": 0, "u": Decimal(0), "gu": Decimal(0)}, "day": rn_today(), "t_run": time.time(),
            "fund": {"t": None, "rate": None, "q": 0.0, "qt": 0.0, "last": None, "busy": False}}

def rn_touch(T, px, t):
    """一筆價格（WS 逐筆成交價，或 WS 斷線時的查價）在時刻 t 檢查：掛單成交、SL/TP 出場。"""
    if T["done"]:
        return
    for g in (T.get("legs") or {}).values():               # 初始單
        if g["fill"] or g["dead"]:
            continue
        if T["cxl_due"] is not None and t >= T["cxl_due"]:
            g["dead"] = True; continue
        if (g["side"] == "L" and px <= g["px"]) or (g["side"] == "S" and px >= g["px"]):
            g["fill"] = (t, g["px"], px)
            T["pos"].append(rn_pos(T, g["side"], 0, g["px"], t, px))
            T["init"][g["side"]] = g["px"]
            if T["cxl_due"] is None:
                T["cxl_due"] = t + RT_LAT                 # 反向單過了送單時間才撤掉
            T["ev"].set()
    for o in list(T["ords"]):                               # 層單（掛出去過了送單時間才生效）
        if t < o["live"]:
            continue
        if (o["side"] == "L" and px <= o["px"]) or (o["side"] == "S" and px >= o["px"]):
            T["ords"].remove(o)
            T["pos"].append(rn_pos(T, o["side"], o["lvl"], o["px"], t, px))
            T["ev"].set()
    for p in T["pos"]:
        if p["out"]:
            continue
        gg = rn_g(p["side"], p["ent"], px)
        if gg > p["mfe"]:
            p["mfe"] = gg
        if gg < p["mae"]:
            p["mae"] = gg
        if p["pend"] and t >= p["pend"][4]:                # SL/TP 過了送單時間才生效
            p["sl"], p["tp"], p["sl_g"], p["tp_g"] = p["pend"][:4]; p["pend"] = None
        isL = p["side"] == "L"
        liq = p.get("liq")                                 # v10.7（1111：先分開計算）：每一層各自的預估強平價，碰到 → 這一層強平
        if liq is not None and ((isL and px <= liq) or (not isL and px >= liq)):
            p["out"] = (t, liq, "強平")
        elif p["sl"] is None:
            continue
        elif p["tp"] is not None and ((isL and px >= p["tp"]) or (not isL and px <= p["tp"])):   # v9.0：框架形成前沒有 TP
            p["out"] = (t, px, "TP")
        elif (isL and px <= p["sl"]) or (not isL and px >= p["sl"]):
            p["out"] = (t, px, "SL(移動)" if p["n_mv"] else "SL")
        if p["out"]:                                       # 出場 → 撤掉這一邊比它後掛（更深）的掛單；L0/S0 出場＝這一邊全部撤掉
            T["ords"] = [o for o in T["ords"] if o["side"] != p["side"] or o["lvl"] <= p["lvl"]]
            T["ev"].set()

def rn_on_tick(iid, px):
    """WS 逐筆成交價（由 _ws_push_px 呼叫）。"""
    t = time.time()
    for T in list(RN.values()):
        if T.get("iid") == iid:
            rn_touch(T, px, t)

def rn_rule(T, p, px, now):
    """每次查價（0.25 秒）判斷一次（v9.0，1111 定案）：
    保本點：最高毛利率 > 0.20% → SL 卡在 +0.10%（沒有 TP；0.20%～0.30% 之間不動）；
    框架點：最高毛利率 > 0.30% → SL＝最高 −0.20%（最低 0.10%）、TP＝最高 +0.15%（v9.5），之後一起往獲利方向移動。
    v9.5：TP 移動次數 n_tp（框架形成後 TP 每變一次 +1；形成那一次不算），跟 SL 移動次數 n_mv 一樣記。
    最高＝逐筆成交價記錄的最高；SL 至少離現價 1 跳；只往獲利方向。"""
    side, ent, tick = p["side"], p["ent"], T["tick"]
    isL = side == "L"
    hi = p["mfe"]                                          # 最高毛利率（逐筆）
    p["hi"] = hi
    cur_sl, cur_tp, sl_g, tp_g = rn_cur_sltp(p)
    if cur_sl is None and hi <= RN_ARM:
        return
    better = lambda a, b: (a > b) if isL else (a < b)      # a 比 b 更往獲利方向
    if hi > RN_BOX:                                        # 框架點：框架形成／移動
        sl_px = rn_lvl(side, ent, max(RN_SL_MIN, hi - RN_SL_GAP), tick)
        tp_px = rn_lvl(side, ent, hi + RN_TP_GAP, tick)
    else:                                                  # 保本點：SL 卡 +0.10%，還沒有 TP
        sl_px = rn_lvl(side, ent, RN_SL_MIN, tick)
        tp_px = None
    lim = px - tick if isL else px + tick                  # SL 至少離現價 1 跳
    if better(sl_px, lim):
        sl_px = lim
        if better(rn_lvl(side, ent, RN_SL_MIN, tick), sl_px):   # 離 0.10% 不到 1 跳 → 這次不設／SL 不動
            sl_px = cur_sl
    if cur_sl is not None and (sl_px is None or not better(sl_px, cur_sl)):
        sl_px = cur_sl                                     # 只往獲利方向
    if tp_px is None or (cur_tp is not None and not better(tp_px, cur_tp)):
        tp_px = cur_tp
    if sl_px is None:
        return
    if sl_px == cur_sl and tp_px == cur_tp:
        return
    if cur_sl is None:
        p["t_arm"] = now
        p["arm_px"] = sl_px
    elif sl_px != cur_sl:
        p["n_mv"] += 1
    if cur_tp is not None and tp_px != cur_tp:              # v9.5：TP 移動次數
        p["n_tp"] = p.get("n_tp", 0) + 1
    p["pend"] = (sl_px, tp_px, rn_g(side, ent, sl_px), None if tp_px is None else rn_g(side, ent, tp_px), now + RT_LAT)

def rn_layers(T, px, now):
    """層單：某一層持倉中且毛利率 < 0，而下一層沒有持倉也沒有掛單 → 立刻限價掛下一層（初始單還在才掛）。
    v9.3：層單是 maker 限價單（手續費 0.02%）；掛單價已經被穿過（例如那一層剛被強平、價格還在下面）就先不掛，
    等價格回到掛單價外面再掛——不然會一掛就成交、又馬上強平，一直重複。"""
    for s in ("L", "S"):
        held = {p["lvl"]: p for p in T["pos"] if p["side"] == s and not p["out"]}
        if 0 not in held:
            continue
        e0 = held[0]["ent"]
        pend = {o["lvl"] for o in T["ords"] if o["side"] == s}
        for k in range(RN_NLV):
            p = held.get(k)
            if p is None or (k + 1) in held or (k + 1) in pend:
                continue
            if rn_g(s, p["ent"], px) < 0:
                lp = rn_lay_px(s, e0, k + 1, T["tick"])
                if (s == "L" and px <= lp) or (s == "S" and px >= lp):
                    continue                               # v9.3：掛單價已經被穿過（一掛就成交＝不是 maker）→ 這次不掛，等價格回到外面
                T["ords"].append({"side": s, "lvl": k + 1, "px": lp, "live": now + RT_LAT, "t": now})
                pend.add(k + 1)

def rn_cur_sltp(p):
    """畫面用：最新決定的 SL/TP（剛送出還沒生效的也算）。"""
    if p["pend"]:
        return p["pend"][0], p["pend"][1], p["pend"][2], p["pend"][3]
    return p["sl"], p["tp"], p["sl_g"], p["tp_g"]

def rn_fill_lines(T, ps):
    """初始單進場成交通知。"""
    tick = T["tick"]
    ps = sorted(ps, key=lambda p: p["side"])               # 兩張都成交時 L 在前
    sides = {p["side"] for p in ps}
    if T.get("dir", "LS") != "LS" and len(sides) == 1:     # v9.3：單向只有一張初始單
        tag = f"{ps[0]['side']}0"
    else:
        tag = "L0S0" if len(sides) == 2 else ("L0(S0)" if "L" in sides else "(L0)S0")
    two = len(ps) == 2
    L = [f"{E.ENTRY} 進場成交 {rn_sym(T)} {tag} {pct(rn_lev(T, 0))}X {pct(T['amt'])}U",
         RT_SEP,
         f"初次 {rt_t(T['t0'])}|{rt_q(T['px0'], tick)}"]
    for p in ps:
        L.append(f"進場 {rt_t(p['t_in'])}|{rt_q(p['ent'], tick)}" + (f"({p['side']}0)" if two else ""))
    L += [f"埋伏 {rt_hold_str(min(p['t_in'] for p in ps) - T['t0'])}",
          RT_SEP,
          RN_SL_WAIT,
          f"時間:{hhmmss()}"]
    return L

def rn_lay_fill_lines(T, p):
    """層單進場成交通知：列出這一邊目前持倉中的單（初始單＋各層）。"""
    tick = T["tick"]
    held = sorted([q for q in T["pos"] if q["side"] == p["side"] and not q["out"]], key=lambda q: (q["lvl"], q["t_in"]))
    L = [f"{E.ENTRY} 進場成交 {rn_sym(T)} {rn_nm(T, p['side'], p['lvl'])} {pct(rn_plev(T, p))}X {pct(T['amt'])}U",
         RT_SEP]
    L += [f"{rn_nm(T, q['side'], q['lvl'])} {rt_t(q['t_in'])}|{rt_q(q['ent'], tick)}" for q in held]
    L += [RT_SEP, RN_SL_WAIT, f"時間:{hhmmss()}"]
    return L

def rn_exit_lines(T, p, m, tail):
    """每一單各自出場的通知。"""
    tick = T["tick"]
    t, px, why = p["out"]
    names = ["毛損益", "手續費"] + (["資金費"] if m["fn"] else []) + ["淨損益"]
    pc = rn_pad([rn_sp(m["g"]), rn_sp(-m["fee"])] + ([rn_sp(m["fund"])] if m["fn"] else []) + [rn_sp(m["net"])])
    us = rn_pad([rn_su(m["gu"]), rn_su(-m["feeu"])] + ([rn_su(m["fu"])] if m["fn"] else []) + [rn_su(m["netu"])])
    hi, lo = rn_pad([rn_sp(p["mfe"]), rn_sp(p["mae"])])
    L = [f"{E.pnl_emoji(m['netu'])} 出場通知 {rn_sym(T)} {rn_nm(T, p['side'], p['lvl'])} {pct(rn_plev(T, p))}X {pct(T['amt'])}U",
         RT_SEP,
         f"進場 {rt_t(p['t_in'])}|{rt_q(p['ent'], tick)}"]
    if p["t_arm"]:
        L.append(f"設SL {rt_t(p['t_arm'])}|{rt_q(p['arm_px'], tick)}")
    L += [f"出場 {rt_t(t)}|{rt_q(px, tick)}|{why}",
          f"持倉 {rn_dur(t - p['t_in'])}|SL移動{p['n_mv']}次|TP移動{p.get('n_tp', 0)}次",   # v9.5／v9.8（1111）：不空格、時間 3:16
          f"最高毛利率 {hi}",
          f"最低毛利率 {lo}",
          RT_SEP,
          "這一單已實現"] + [f"{n} {x}|{y}" + (f"|結算{m['fn']}次" if n == "資金費" else "") for n, x, y in zip(names, pc, us)]
    # v9.1（1111 版面）：這個方向的 持倉中(未實現)／本日已實現／套牢明細，段落之間空一行，時間前也空一行；
    # 最後一行「(L0還在,繼續)」拿掉（tail 不再用）。套牢明細＝這個幣種同方向還抱著的每一單（0 層往上），用出場價算淨損益。
    side = p["side"]
    live = sorted([q for q in T["pos"] if q["side"] == side and not q["out"]], key=lambda q: (q["lvl"], q["t_in"]))
    hold, rows = [], []
    if live:
        a = rn_acc()
        ms = []
        for q in live:
            mq = rn_money(T, q, px)
            rn_acc_add(a, mq)
            ms.append(mq)
        # v10.1（1111）：XX／YX／ZX 每一行加這一層的槓桿（越上層槓桿越大、倉位越大，% 才會是山形）；% 和 U 各自對齊
        xs = rn_pad([rn_sp(rn_q2(mq["net"])) for mq in ms])
        ys = rn_pad([rn_su(rn_q4(mq["netu"])) for mq in ms])
        rows = [f"{rn_sym(T)} {rn_nmx(T, q)} {x}|{y}" for q, x, y in zip(live, xs, ys)]
        hold = rn_pl(f"{side} 持倉中 {len(live)}單(未實現)", a)   # v10.7：合倉三行拿掉（每一層各自強平）
    d = ((RN_DAY.get(rn_today()) or {}).get(T["sym"]) or {}).get(side)
    day = rn_pl(f"{side} 本日已實現 {d['n']}單", d) if d and d["n"] else []
    blocks = rn_join([hold, day, (["套牢明細"] + rows) if rows else []])
    if blocks:
        L += [RT_SEP] + blocks + [""]
    L.append(f"時間:{hhmmss()}")
    return L


RN_WHY = ("SL", "SL(移動)", "TP", "強平")     # v10.1：出場原因的排列順序

def rn_span(sec):
    """v10.1：時間長度。一天以內跟持倉一樣（50:38、1:50:11），超過一天加「天」（1天1:50:11），跑一星期也好讀。"""
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    return f"{d}天{r // 3600}:{r % 3600 // 60:02d}:{r % 60:02d}" if d else rn_dur(r)

def rn_mdt(t):
    """v10.1：日期＋時間 10/06 09:12:03（台灣時間）。"""
    return datetime.fromtimestamp(t, TZ8).strftime("%m/%d %H:%M:%S")

def rn_rec_round(T, r):
    """v10.1：本輪紀錄（T["rnd"] 的一筆）→ 統計用的一單。v10.0 以前的紀錄沒有毛損益%／資金費% → 用 U ÷ 保證金補。"""
    amt = T["amt"]
    fu = Decimal(r.get("fu", 0))
    return {"side": r["side"], "lvl": int(r["lvl"]), "lev": r.get("lev"), "why": r.get("why", ""),
            "g": r["g"] if r.get("g") is not None else rn_q2(Decimal(r["gu"]) / amt * 100),
            "net": Decimal(r["net"]), "fund": r["fund"] if r.get("fund") is not None else rn_q2(fu / amt * 100),
            "gu": Decimal(r["gu"]), "netu": Decimal(r["netu"]), "fu": fu, "fn": int(r.get("fn", 1 if fu else 0)),
            "t_in": float(r["t_in"]), "t_out": float(r["t_out"]), "mfe": r.get("mfe")}

def rn_rec_log(r):
    """v10.1：逐單出場紀錄（runtlog 一行）→ 統計用的一單。"""
    D = lambda k: Decimal(str(r.get(k) or "0"))
    return {"side": r["side"], "lvl": int(r["lvl"]), "lev": Decimal(r["lev"]) if r.get("lev") else None, "why": r.get("why", ""),
            "g": D("gp"), "net": D("np"), "fund": D("fp"), "gu": D("gu"), "netu": D("u"), "fu": D("fu"), "fn": int(r.get("fn") or 0),
            "t_in": float(r["t_in"]), "t_out": float(r["t_out"]), "mfe": Decimal(r["mfe"]) if r.get("mfe") is not None else None}

def rn_run_recs(T):
    """v10.1 策略結束：整個策略（從 /runt 開始、所有輪）出場的每一單，從逐單紀錄（runtlog）取。
    這一組出場過 T["st"]["n"] 單（bot 重開也接著算）→ 取這個幣種、這個方向最後的 n 筆；有 /runt 開始時間的話，只取那之後出場的。"""
    n = int(T["st"]["n"])
    if n <= 0 or not os.path.exists(RN_LOG_FILE):
        return []
    t_run = T.get("t_run")
    out = []
    try:
        with open(RN_LOG_FILE) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("sym") != T["sym"] or r.get("side") != T["dir"]:
                    continue
                if t_run is not None and float(r.get("t_out") or 0) < t_run - 1:
                    continue
                out.append(r)
        return [rn_rec_log(r) for r in out[-n:]]
    except Exception as e:
        print("[runt] run recs read fail", e)
        return []

def rn_stat_lines(T, recs, full):
    """v10.1（1111：一大串各單淨損益沒有意義，跑一星期也要看得懂）：統計取代一單一行。
    full＝策略結束才有的「出場統計」（平均／最長持倉、最高毛利率、最好、最差）。回傳 (出場原因一行, 出場統計段, 各層出場段)。"""
    why = {}
    for r in recs:
        why[r["why"]] = why.get(r["why"], 0) + 1
    ws = [w for w in RN_WHY if why.get(w)] + [w for w in why if w not in RN_WHY]
    why_line = "出場原因 " + "|".join(f"{w} {why[w]}" for w in ws)
    nm = lambda r: f"{r['side']}{r['lvl']}" + rn_lvx(T, r["lvl"], r.get("lev"))
    stat = []
    if full and recs:
        hold = [r["t_out"] - r["t_in"] for r in recs]
        lg = max(recs, key=lambda r: r["t_out"] - r["t_in"])
        stat = ["出場統計", why_line, f"平均持倉 {rn_span(sum(hold) / len(hold))}",
                f"最長持倉 {nm(lg)} {rn_span(lg['t_out'] - lg['t_in'])}"]
        mf = [r for r in recs if r.get("mfe") is not None]
        if mf:
            h = max(mf, key=lambda r: r["mfe"])
            stat.append(f"最高毛利率 {nm(h)} {rn_sp(h['mfe'])}")
        b = max(recs, key=lambda r: (r["net"], r["netu"]))
        w = min(recs, key=lambda r: (r["net"], r["netu"]))
        stat += [f"最好 {nm(b)} {rn_sp(b['net'])}|{rn_su(b['netu'])}|{b['why']}",
                 f"最差 {nm(w)} {rn_sp(w['net'])}|{rn_su(w['netu'])}|{w['why']}"]
    lv = {}
    for r in recs:
        k = (r["side"], r["lvl"])
        x = lv.setdefault(k, [0, Decimal(0), Decimal(0)])
        x[0] += 1; x[1] += rn_q2(r["net"]); x[2] += rn_q4(r["netu"])
    ks = sorted(lv)
    cs = rn_pad([f"{lv[k][0]}單" for k in ks])
    ps = rn_pad([rn_sp(lv[k][1]) for k in ks])
    us = rn_pad([rn_su(lv[k][2]) for k in ks])
    layer = ["各層出場"] + [f"{sd}{l}{rn_lvx(T, l)} {c}|{x}|{y}" for (sd, l), c, x, y in zip(ks, cs, ps, us)]
    return why_line, stat, layer

def rn_end_lines(T):
    """本輪結束（初始單出場、全部清空）。🚦 最後一輪打完＝策略結束。
    v10.1（1111）：拿掉「各單淨損益」一單一行 —— 本輪結束＝這一輪的統計；策略結束＝整個策略（從 /runt 開始、所有輪）的統計。"""
    rnd = [rn_rec_round(T, r) for r in T["rnd"]]
    u = sum((r["netu"] for r in rnd), Decimal(0))
    sides = sorted({r["side"] for r in rnd})
    end = max([r["t_out"] for r in rnd] or [time.time()])
    if rn_whole(T):                                        # v9.3：🚦 這一組打完
        recs = rn_run_recs(T) or rnd                       # 逐單紀錄讀不到 → 至少列這一輪
        t0 = T.get("t_run") or min([r["t_in"] for r in recs] or [T.get("t0") or end])
        why_line, stat, layer = rn_stat_lines(T, recs, True)
        a = rn_acc()
        for r in recs:
            rn_acc_add(a, r)
        n_all = int(T["st"]["n"])
        note = [f"(逐單紀錄只找到 {len(recs)}/{n_all} 單)"] if len(recs) < n_all else []
        L = [f"🚦 策略結束 {T['sym']} {T['dir']} {rn_levs(T)} {pct(T['amt'])}U", RT_SEP,
             f"開始 {rn_mdt(t0)}", f"結束 {rn_mdt(end)}",
             f"歷時 {rn_span(end - t0)}|{T['st']['r']}輪|出場 {n_all}單"] + note
        L += [RT_SEP] + rn_pl(f"整個策略已實現 {len(recs)}單", a)
        L += [RT_SEP] + stat + [RT_SEP] + layer
        tail = "(全部出場,策略結束)"
    else:
        t0 = T.get("t0") or min([r["t_in"] for r in rnd] or [end])
        why_line, _, layer = rn_stat_lines(T, rnd, False)
        a = rn_acc()
        for r in rnd:
            rn_acc_add(a, r)
        span = rn_mdt(t0) + "～" + (rt_t(end) if rn_today(t0) == rn_today(end) else rn_mdt(end))
        L = [f"{E.pnl_emoji(u)} 本輪結束 {rn_head(T)}", RT_SEP,
             f"這一輪 {span}", f"歷時 {rn_span(end - t0)}|出場 {len(rnd)}單", why_line]
        L += [RT_SEP] + layer + [RT_SEP] + rn_pl(f"這一輪已實現 {len(rnd)}單", a)
        tail = f"({rn_init_names(T)}出場,{RN_WAIT}秒後重新來過)"
    L += rn_pl_secs(T, None, sides)                        # v8.2：這一輪有出場的方向：本日已實現
    L += [f"時間:{hhmmss()}", tail]
    return L

def rn_pin(p, px):
    """v10.7（1111）：還沒設 SL 的那一層顯示 📍＝現價到這一層預估強平價的距離（＋強平價在上面、−在下面）；1X 做多不會強平＝📍無。"""
    liq = p.get("liq")
    if liq is None or not px:
        return "📍無"
    return "📍" + rn_sp(rn_q2((liq / px - 1) * 100))

def rn_book(T, px):
    """持倉中的表（持倉通知、/stoprunt 共用）：各單一行＋合計。"""
    tick = T["tick"]
    rows = []                                              # (side, lvl, 排序時間, 名稱, 價格, 狀態) 狀態＝毛利率 或 文字
    for r in T["rnd"]:
        if r["lvl"] == 0:
            rows.append((r["side"], 0, r["t_in"], rn_nm(T, r["side"], 0), r["ent"], "已出場"))
    live = [p for p in T["pos"] if not p["out"]]
    for p in live:
        rows.append((p["side"], p["lvl"], p["t_in"], rn_nm(T, p["side"], p["lvl"]), p["ent"], p))
    for o in T["ords"]:
        rows.append((o["side"], o["lvl"], 9e18, rn_nm(T, o["side"], o["lvl"]), o["px"], "掛單中"))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    gs = rn_pad([rn_sp(rn_g(r[5]["side"], r[5]["ent"], px)) for r in rows if isinstance(r[5], dict)])
    gi = iter(gs)
    L = []
    for side, lvl, _, nm, ent, x in rows:
        if T.get("xx"):                                    # v9.3：XX 每一層槓桿不同，標出來（掛單中、已出場也標）
            nm = f"{nm} {pct(rn_plev(T, x) if isinstance(x, dict) else rn_lev(T, lvl))}X"
        if isinstance(x, dict):
            sl = rn_cur_sltp(x)[0]
            L.append(f"{nm} {rt_q(ent, tick)}|{next(gi)}|" + (rn_pin(x, px) if sl is None else f"SL {rt_q(sl, tick)}"))
        else:
            L.append(f"{nm} {rt_q(ent, tick)}|{x}")
    return L

def rn_wait_lines(T, px):
    """埋伏中（三行：S0 在上、現價、L0 在下）。"""
    tick = T["tick"]
    legs = T.get("legs") or {}
    L = []
    if "S" in legs:
        L.append(f"掛S0 {rt_q(legs['S']['px'], tick)}")
    L.append(f"現價 {rt_q(px, tick)}")
    if "L" in legs:
        L.append(f"掛L0 {rt_q(legs['L']['px'], tick)}")
    return L

def rn_last_lines(T):
    """v9.4：/status 持倉通知標題下面一行：🚦 最後一輪的說明。"""
    if rn_whole(T):
        return ["🚦 最後一輪,全部出場後策略結束"]
    return []

def rn_note_lines(T, px, now, fund=None):
    """/status 用（v8.6 起不再每個 /tf 自動發）：持倉中＝持倉通知；埋伏中＝埋伏通知。都有資金費率兩行。
    fund＝/status 當下查到的資金費率行；沒給就用 bot 每 60 秒查到的。"""
    tick = T["tick"]
    fl = rn_fund_line(T) if fund is None else fund
    if T["h0"] is not None and any(not p["out"] for p in T["pos"]):
        return ([f"📣持倉通知 {rn_head(T)}"] + rn_last_lines(T)
                + [f"⏰持倉 {rt_hms(now - T['h0'])}|現價 {rt_q(px, tick)}"] + fl
                + [RT_SEP] + rn_book(T, px)
                + rn_pl_secs(T, px))   # v8.2：L、S 各自 持倉中／本日已實現
    return ([f"📣埋伏通知 {rn_head(T)}",
             f"⏰埋伏時間 {rt_hms(now - T['t0'])}"] + fl
            + [f"初次 {rt_t(T['t0'])}|{rt_q(T['px0'], tick)}"] + rn_wait_lines(T, px))

def rn_save():
    try:
        s = lambda v: None if v is None else str(v)
        def P(p):
            sl, tp, sl_g, tp_g = rn_cur_sltp(p)               # 存檔時剛送出的 SL/TP 一律當作已生效
            return {"side": p["side"], "lvl": p["lvl"], "ent": str(p["ent"]), "t_in": p["t_in"], "qty": str(p["qty"]),
                    "sl": s(sl), "tp": s(tp), "sl_g": s(sl_g), "tp_g": s(tp_g),
                    "t_arm": p["t_arm"], "arm_px": s(p["arm_px"]), "n_mv": p["n_mv"], "n_tp": p.get("n_tp", 0), "hi": s(p["hi"]),
                    "mfe": str(p["mfe"]), "mae": str(p["mae"]), "fu": str(p.get("fu", 0)), "fn": p.get("fn", 0),
                    "lev": s(p.get("lev")), "liq": s(p.get("liq"))}
        data = [{"v": 77, "sym": T["sym"], "lev": str(T["lev"]), "amt": str(T["amt"]), "off": str(T["off"]),
                 "dir": T.get("dir", "LS"), "xx": int(T.get("xx") or 0), "mmr": s(T.get("mmr")), "last": bool(T.get("last")), "grp": 1, "lev_now": s(T.get("lev_now")),
                 "chat": T["chat"], "t0": T["t0"], "px0": str(T["px0"]), "h0": T["h0"], "wait_until": T.get("wait_until"),
                 "init": {k: str(v) for k, v in T["init"].items()},
                 "pos": [P(p) for p in T["pos"] if not p["out"]],
                 "ords": [{"side": o["side"], "lvl": o["lvl"], "px": str(o["px"]), "t": o["t"]} for o in T["ords"]],
                 "rnd": [{**r, "ent": str(r["ent"]), "net": str(r["net"]), "netu": str(r["netu"]), "gu": str(r["gu"]),
                          "fu": str(r.get("fu", 0)), **{k: str(r[k]) for k in ("g", "fund", "mfe", "lev") if r.get(k) is not None}}
                         for r in T["rnd"]],
                 "t_run": T.get("t_run"),                     # v10.1：/runt 開始的時間（策略結束統計用）
                 "st": {"r": T["st"]["r"], "n": T["st"]["n"], "u": str(T["st"]["u"]), "gu": str(T["st"]["gu"])}}
                for T in RN.values() if not T.get("done")]
        os.makedirs(os.path.dirname(RN_FILE), exist_ok=True)
        rn_day_save()
        tmp = RN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, RN_FILE)
    except Exception as e:
        print("[runt] save fail", e)

async def rn_wait_px(T, on):
    px = None
    while on() and px is None:
        px = await rt_px(T)
        if px is None:
            await asyncio.sleep(0.2)
    return px

def rn_tail(T, p):
    """單一出場通知的最後一行。這一輪全部出場就不寫（後面另發本輪結束）。"""
    live = [q for q in T["pos"] if not q["out"]]
    if not live:
        return ""
    if p["lvl"] == 0:
        return f"({rn_title(p['side'], 0)}出場,等其他單出場後重新來過)"
    if any(q["lvl"] == 0 for q in live):
        return f"({rn_init_names(T, live_only=True)}還在,繼續)"
    return "(等其他單出場後重新來過)"

async def rn_worker(app, key):
    """一組 runt 的迴圈：每 0.25 秒查價一次、判斷規則與層單；成交、出場由 rn_touch（WS 逐筆）判斷。"""
    T = RN[key]
    on = lambda: RN.get(key) is T and not T["done"]
    send = lambda txt: rt_chain(T, lambda t=txt: rt_send(app, T["chat"], t))
    try:
        WS_WANT.add(T["iid"])
        while on():
            now = time.time()
            if now < T["nxt"] and not T["ev"].is_set():
                try:
                    await asyncio.wait_for(T["ev"].wait(), timeout=T["nxt"] - now)
                except asyncio.TimeoutError:
                    pass
            T["ev"].clear()
            if not on():
                return
            # v8.7（1111）：00:00 不再發每個幣種的本日結算，改成 00:00 自動發前一天完整的 /summary。
            if T.get("wait_until"):                        # 本輪結束後等 RN_WAIT 秒（v9.2：300 秒）
                if time.time() < T["wait_until"]:
                    T["nxt"] = min(T["wait_until"], time.time() + 1.0)
                    continue
                px = await rn_wait_px(T, on)
                if not on():
                    return
                rn_round(T, time.time(), rt_q(px, T["tick"]))
                rn_save()
                continue
            if T["legs"] and T["cxl_due"] is not None:     # 初始單成交 → 等反向單撤掉（RT_LAT）→ 進場通知 → 開始判斷
                rem = T["cxl_due"] - time.time()
                if rem > 0:
                    await asyncio.sleep(rem + 0.005)
                    if not on():
                        return
                T["legs"] = {}
                new = [p for p in T["pos"] if not p["told"]]
                for p in new:
                    p["told"] = True
                if new:
                    print(f"[runt] 初始單成交 {key} " + " ".join(f"{p['side']}@{p['ent']}" for p in new))
                    send("\n".join(rn_fill_lines(T, new)))
                    T["h0"] = min(p["t_in"] for p in new)
                    T["note_last"] = rt_note_base(T["h0"])
                rn_save()
                continue
            new = [p for p in T["pos"] if not p["told"]]
            for p in sorted(new, key=lambda p: p["t_in"]):  # 層單成交
                p["told"] = True
                print(f"[runt] 層單成交 {key} {p['side']}{p['lvl']}@{p['ent']}")
                send("\n".join(rn_lay_fill_lines(T, p)))
            outs = sorted([p for p in T["pos"] if p["out"]], key=lambda p: p["out"][0])
            for p in outs:                                 # 每一單各自出場（v10.7：強平也是，出場原因「強平」）
                T["pos"].remove(p)
                m = rn_money(T, p, p["out"][1])
                rn_done(T, p, m)
                print(f"[runt] 出場 {key} {p['side']}{p['lvl']} {p['out'][2]} @{p['out'][1]} 淨{m['netu']}")
                send("\n".join(rn_exit_lines(T, p, m, rn_tail(T, p))))
            if outs and not T["pos"]:                      # 全部出場 → 本輪結束 → 等 RN_WAIT 秒 → 用現價重新來過
                T["ords"] = []
                T["st"]["r"] += 1
                send("\n".join(rn_end_lines(T)))
                if rn_whole(T):                            # v9.3：🚦 最後一輪打完 → 策略結束，不再開新的一輪
                    print(f"[runt] 🚦 策略結束 {key}")
                    T["done"] = True
                    if RN.get(key) is T:
                        RN.pop(key, None)
                    rn_save()
                    return
                T["legs"] = {}
                T["rnd"] = []
                T["wait_until"] = time.time() + RN_WAIT
                T["nxt"] = 0.0
            if new or outs:
                rn_save()
                continue
            now = time.time()
            if now < T["nxt"]:
                continue
            T["nxt"] = now + RN_STEP - (now % RN_STEP)
            px = await rt_px(T)
            if px is None or not on():
                continue
            T["px_last"] = px
            rn_touch(T, px, time.time())                    # WS 斷線時靠查價判斷（WS 正常時逐筆已判斷過，重複不影響）
            f = T["fund"]                                  # v8.2 資金費：查費率（背景）、到結算時間記到持倉的單
            tq = time.time()
            if not f["busy"] and (tq >= f["q"] or (f["t"] and 0 < f["t"] - tq <= RN_FUND_PRE and tq - f["qt"] > RN_FUND_PRE)):
                f["busy"] = True; f["q"] = tq + RN_FUND_Q
                rt_bg(rn_fund_query(T))
            if f["t"] is not None and f["rate"] is not None and tq >= f["t"]:
                L = rn_fund_settle(T, px)
                if L:
                    print(f"[runt] 資金費結算 {key} 費率 {f['rate'] if f['rate'] is not None else ''}")
                    send("\n".join(L))
                    rn_save()
            if T["ev"].is_set():
                continue
            if not T["legs"]:                              # 反向單撤掉之後才判斷
                for p in T["pos"]:
                    if not p["out"]:
                        rn_rule(T, p, px, time.time())
                rn_layers(T, px, time.time())
            # v8.6（1111）：不再每個 /tf 發持倉通知／埋伏通知，有事件才通知；狀態用 /status 查。
            if time.time() >= T["save_t"]:
                T["save_t"] = time.time() + RT_SAVE_SEC
                rn_save()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[runt] worker fail", key, type(e).__name__, e)
        T["done"] = True
        if RN.get(key) is T:
            RN.pop(key, None)
        rn_save()
        await rt_send(app, T["chat"], f"{E.LOSS} runt 異常停止 {rn_head(T)}\n{type(e).__name__}: {e}\n時間:{hhmmss()}")

def rn_start(app, T):
    RN[T["key"]] = T
    T["task"] = asyncio.create_task(rn_worker(app, T["key"]))
    rn_save()

def rn_status_line(T):
    if T.get("wait_until"):
        return f"💡{rn_par(T)}|本輪結束,剩 {max(0, int(T['wait_until'] - time.time()))} 秒重新埋伏"
    live = [p for p in T["pos"] if not p["out"]]
    if not live or T["h0"] is None:
        return f"💡{rn_par(T)}|埋伏中 {rt_hms(time.time() - T['t0'])}"
    px = T.get("px_last")
    s = f"💡{rn_par(T)}|持倉 {rt_hms(time.time() - T['h0'])}|{len(live)}單"
    if px:
        nr = sum((rn_q2(rn_money(T, p, px)["net"]) for p in live), Decimal(0))   # v8.4：各單相加
        s += f"|持倉中淨損益 {rn_sp(nr)}"
    return s

def rn_status_blocks(fund=None):
    """/status（v8.1）：每個 /runt 幣種一段，內容跟持倉通知／埋伏通知一樣；等待 60 秒中的寫剩幾秒。
    v8.5：每一段都有資金費率兩行（fund＝幣種 -> /status 當下向 OKX 查到的行）。"""
    out = []
    now = time.time()
    fund = fund or {}
    for T in rn_groups():                                 # v9.6：同幣種排在一起，L 在前
        key = T["key"]
        fl = fund[key] if key in fund else rn_fund_line(T)
        if T.get("wait_until"):
            out.append([f"⏳{rn_par(T)}", f"本輪結束,剩 {max(0, int(T['wait_until'] - now))} 秒重新埋伏"] + fl)
            continue
        px = T.get("px_last") or (WS_PX.get(T["iid"]) or (None,))[0]
        if px is None:
            out.append([f"💡{rn_par(T)}", "查價中"] + fl)
            continue
        out.append(rn_note_lines(T, px, now, fl))
    return out

async def rn_status_fund():
    """v8.5：/status 當下，所有 /runt 幣種一起向 OKX 查資金費率。"""
    keys = list(RN.keys())
    res = await asyncio.gather(*[rn_fund_now(RN[k]) for k in keys], return_exceptions=True)
    return {k: r for k, r in zip(keys, res) if isinstance(r, list)}

def rn_running():
    return ["進行中:"] + ([rn_status_line(T) for T in rn_groups()] or ["目前沒有進行中的 runt"])

RN_ASK = ("📝 請輸入 /runt 參數(直接打參數,不用打 /runt)\n"
          "幣種 方向 槓桿 保證金 初始埋伏點%\n"
          "例:WLDUSDT LS 1X 1U 0.5%\n"
          "方向:LS 兩邊(各自一組)、L 只做多、S 只做空\n"
          "槓桿:1X～100X 每一層一樣;級距槓桿 XX1～XX5:第 n 層＝(n+1)×倍數\n"
          "　XX1＝L0 1X…L9 10X;XX2＝L0 2X…L9 20X;XX3＝L0 3X…L9 30X;XX4＝L0 4X…L9 40X;XX5＝L0 5X…L9 50X\n"
          "槓桿要加 X,保證金要加 U,埋伏點要加 %")
RN_RETRY = "請重新輸入 /runt 參數:\n幣種 方向 槓桿 保證金 初始埋伏點%\n例:WLDUSDT LS 1X 1U 0.5%"
RN_HINT = "WLDUSDT LS 1X 1U 0.5%"
RN_XX_MIN = Decimal(RN_NLV + 1)              # v9.3：XX 最深一層 L9＝10X，幣種最高槓桿要 ≥10X
# v10.2（1111）：級距槓桿改名 XX1～XX5（XX→XX1、YX→XX2、ZX→XX5，新增 XX3、XX4）。第 n 層＝(n+1)×倍數；L9 要 10×倍數。
#   存檔記的是倍數（T["xx"]＝1～5），不是名稱 → 已經在跑的組重開後自動顯示新名稱，持倉不受影響。
RN_XMUL = {"XX1": 1, "XX2": 2, "XX3": 3, "XX4": 4, "XX5": 5}
RN_XNAME = {v: k for k, v in RN_XMUL.items()}
RN_XOLD = {"XX": "XX1", "YX": "XX2", "ZX": "XX5"}     # v10.2：舊名稱不再接受，打了提示新名稱

def rn_parse(a):
    """解析 5 個參數（v9.3：幣種 方向 槓桿 保證金 初始埋伏點%）。成功回傳 (dict, None)，失敗回傳 (None, 錯誤說明)。"""
    a = [x.strip() for x in a]
    if len(a) != 5:
        return None, f"參數數量錯誤(需5個,收到{len(a)}個)"
    p = {"sym": a[0].upper()}
    if not p["sym"].endswith("USDT") or len(p["sym"]) <= 4:
        return None, f"幣種格式錯誤:{a[0]}(例 WLDUSDT)"
    d = a[1].upper()
    if d in ("LS", "SL"):
        p["dir"] = "LS"
    elif d in ("L", "S"):
        p["dir"] = d
    else:
        return None, f"方向只能 LS、L、S(收到 {a[1]})"
    if a[2].upper() in RN_XOLD:                            # v10.2：舊名稱 → 提示新名稱
        return None, f"{a[2].upper()} 已改名 {RN_XOLD[a[2].upper()]}(XX→XX1、YX→XX2、ZX→XX5)"
    if a[2].upper() in RN_XMUL:                            # v9.9：浮動槓桿；v10.2：XX1～XX5
        p["xx"] = RN_XMUL[a[2].upper()]
        p["lev"] = RN_XX_MIN * p["xx"]                     # L9 的槓桿（幣種最高槓桿要夠）
    else:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)[xX]", a[2])
        if not m:
            return None, "槓桿要加 X(例 1X),或 XX1～XX5"
        p["xx"], p["lev"] = 0, Decimal(m.group(1))
        if p["lev"] < 1:
            return None, "槓桿不可小於 1X"
        if p["lev"] > 100:
            return None, "槓桿不可超過 100X"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)[uU]", a[3])
    if not m:
        return None, "保證金要加 U(例 1U)"
    p["amt"] = Decimal(m.group(1))
    if p["amt"] <= 0:
        return None, "保證金須大於 0"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)%", a[4])
    if not m:
        return None, "初始埋伏點要加 %(例 0.5%)"
    p["off"] = Decimal(m.group(1))
    if p["off"] <= 0:
        return None, "初始埋伏點須大於 0"
    if p["off"] > 20:
        return None, "初始埋伏點不可超過 20%"
    return p, None

async def cmd_runt(u, c):
    """沒帶參數 → 先告訴你參數怎麼打，再等你輸入；打錯 → 回錯誤原因，繼續等你重打。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args or []
    if not a:
        await ask(u, "runt", cmd_runt, "\n".join([RN_ASK, RT_SEP] + rn_running() + [ASK_CANCEL]), RN_HINT); return
    retry = lambda msg: ask(u, "runt", cmd_runt, f"{msg}\n{RN_RETRY}", RN_HINT)
    p, err = rn_parse(a)
    if err:
        await retry(f"{E.WARN} {err}"); return
    sym, sides = p["sym"], list(p["dir"])
    busy = [(x, RN[rn_key(sym, x)]) for x in sides if rn_key(sym, x) in RN]
    if busy:                                               # v9.6（1111）：同幣種同方向只能一組；LS 其中一邊已有 → 整個擋下來（避免併倉）
        msg = []
        for x, T0 in busy:
            msg.append(f"🚦{sym} {x} 最後一輪進行中,全部出場(策略結束)後才能重新開 {x}" if rn_whole(T0)
                       else f"{sym} {x} 已經在跑 runt")
        msg[0] = f"{E.WARN} {msg[0]}"
        msg.append("同一帳戶同幣種同方向只能一組(避免併倉)")
        free = [x for x in sides if rn_key(sym, x) not in RN]
        if free:
            msg.append(f"要開 {free[0]} 請打 /runt {sym} {free[0]} {' '.join(x.strip() for x in a[2:5])}")
        elif not any(rn_whole(T0) for _, T0 in busy):
            msg.append(f"要換請先 /stoprunt {sym} {p['dir']}")
        await retry("\n".join(msg)); return
    try:
        spec = await get_spec(sym)
    except Exception:
        await retry(f"{E.LOSS} 找不到幣種 {sym}"); return
    if p["xx"] and spec["maxlev"] < p["lev"]:             # v9.3：L9 要 10×倍數（v10.2：XX1 10X…XX5 50X）
        nm = RN_XNAME[p["xx"]]
        await retry(f"{E.WARN} {sym} 槓桿最高 {pct(spec['maxlev'])}X\n"
                    f"{nm} 最深一層 L9 要 {pct(p['lev'])}X,這個幣種不能用 {nm}"); return
    if not p["xx"] and p["lev"] > spec["maxlev"]:
        await retry(f"{E.WARN} {sym} 槓桿最高 {pct(spec['maxlev'])}X"); return
    tick = spec["tick"]
    try:
        px0 = rt_q(await get_last(spec["iid"]), tick)
    except Exception:
        await retry(f"{E.LOSS} 查不到 {sym} 現價,請稍後再試"); return
    p["mmr"] = await rn_mmr(spec)                          # v9.3：強平用（OKX 逐倉維持保證金率）
    if any(rn_key(sym, x) in RN for x in sides):           # 查價途中又打了一次
        return
    L = [f"📣 runt 啟動｜{ACCT}"]
    now = time.time()
    for i, x in enumerate(sides):                          # v9.6：LS＝一次開 L、S 兩組（同參數），開完各打各的
        T = rn_new({**p, "dir": x}, spec, u.effective_chat.id)
        rn_round(T, now, px0)
        rn_start(c.application, T)
        L += ([RT_SEP] if i else []) + [rn_par(T)] + rn_wait_lines(T, px0)
    await reply(u, "\n".join(L))

def rn_stop_lines(T, now, last=False):
    """/stoprunt：這一組停止時的狀態。last＝v9.3 🚦 最後一輪（有持倉：照常作戰，全部出場後策略結束）。"""
    L = [rn_par(T)]
    live = [p for p in T["pos"] if not p["out"]]
    px = T.get("px_last") or (WS_PX.get(T["iid"]) or (None,))[0]
    if live and px is not None and T["h0"] is not None:
        L += [f"⏰持倉 {rt_hms(now - T['h0'])}|現價 {rt_q(px, T['tick'])}", RT_SEP] + rn_book(T, px)
    elif live:
        L.append(f"持倉 {len(live)} 單|未出場")
    elif T.get("wait_until"):
        L.append("本輪結束,等待重新埋伏中|已停止")
    else:
        L.append(f"埋伏中 {rt_hms(now - T['t0'])}|已停止")
    if last:
        L += [RT_SEP,
              f"持倉 {len(live)} 單,這一輪照常作戰(層單照掛)",
              "這一輪全部出場後結束策略,不再開新的一輪",
              "BOT 不平倉,單子照自己的 SL/TP 出場"]
    return L + rn_pl_secs(T, px if live else None)   # v8.2

def rn_stop_one(T, now):
    """v9.6（1111：L、S 完全獨立）：/stoprunt 一組（幣種＋方向）。
    沒有持倉（埋伏中、等 300 秒）→ 馬上停止（沒成交的 L0/S0 撤掉），這一組拿掉；
    有持倉 → 🚦 最後一輪：這一輪照常作戰、層單照掛，全部出場後「🚦 策略結束」，這一組拿掉。BOT 永遠不平倉。
    回傳 (種類, 畫面)：stop＝馬上停止；last＝變成 🚦；again＝已經是最後一輪。"""
    key = T["key"]
    live = [p for p in T["pos"] if not p["out"]]
    if live:
        if rn_whole(T):
            return "again", [rn_par(T), f"已經是最後一輪,持倉 {len(live)} 單", "這一輪全部出場後結束策略",
                             "BOT 不平倉,單子照自己的 SL/TP 出場"]
        T["last"] = True
        print(f"[runt] 🚦 最後一輪 {key}")
        return "last", rn_stop_lines(T, now, last=True)
    T["done"] = True
    tk = T.get("task")
    if tk:
        tk.cancel()
    if RN.get(key) is T:
        RN.pop(key, None)
    for p in [p for p in T["pos"] if p["out"]]:            # 停止那一刻剛好出場、還沒發通知的：算進本日
        rn_done(T, p, rn_money(T, p, p["out"][1]))
        T["pos"].remove(p)
    print(f"[runt] 停止 {key}")
    return "stop", rn_stop_lines(T, now)

RN_STOP_ASK = ["📝 請輸入要停止的幣種和方向,或 all",
               "例:WLDUSDT LS(兩邊)、WLDUSDT L(只停多)、WLDUSDT S(只停空)、all(全部幣種、兩邊)",
               "沒有進場＝馬上停止;有進場＝🚦最後一輪(這一輪打完才結束,BOT 不平倉)"]
RN_STOP_RETRY = "請重新輸入要停止的幣種和方向,或 all(例 WLDUSDT LS、WLDUSDT L)"
RN_STOP_HINT = "WLDUSDT LS 或 all"

def rn_stop_parse(a):
    """v9.4：/stoprunt 幣種 方向（LS／L／S，一定要打）或 all。回傳 (幣種或 ALL, 方向, 錯誤說明)。"""
    a = [x.strip() for x in a if x.strip()]
    if not a:
        return None, None, "要有幣種和方向,或 all"
    if a[0].upper() == "ALL":
        if len(a) > 1:
            return None, None, "all 後面不用加方向(all＝全部幣種、兩邊)"
        return "ALL", "LS", None
    if len(a) == 1:
        return None, None, f"要加方向:{a[0].upper()} LS(兩邊)、L(只停多)、S(只停空)"
    if len(a) > 2:
        return None, None, f"參數數量錯誤(需2個:幣種 方向,收到{len(a)}個)"
    d = a[1].upper()
    d = "LS" if d == "SL" else d
    if d not in ("LS", "L", "S"):
        return None, None, f"方向只能 LS、L、S(收到 {a[1]})"
    return a[0].upper(), d, None

async def cmd_stoprunt(u, c):
    """沒帶參數 → 列出進行中，等你輸入；打錯／幣種或方向沒在跑 → 繼續等你重打。
    v9.3（1111）：沒有持倉（埋伏中、等 300 秒）→ 馬上停止；有持倉 → 🚦 最後一輪：這一輪照常作戰（層單照掛），
    全部出場後「🚦 策略結束」，不再開新的一輪。BOT 永遠不平倉（1111 自己去 OKX 平倉）；再打一次也不會強制平倉。
    v9.4（1111）：/stoprunt 幣種 方向（LS 兩邊、L 只停多、S 只停空）；/stoprunt all＝全部幣種、兩邊。
    v9.6（1111）：L、S 完全獨立，每一組（幣種＋方向）各自停，規則見 rn_stop_one。"""
    a = c.args or []
    if not RN:
        await reply(u, f"{E.BOT} 目前沒有進行中的 runt"); return
    run_lines = rn_running()
    retry = lambda msg: ask(u, "stoprunt", cmd_stoprunt, "\n".join([msg, RN_STOP_RETRY] + run_lines), RN_STOP_HINT)
    if not a:
        await ask(u, "stoprunt", cmd_stoprunt, "\n".join(RN_STOP_ASK + run_lines + [ASK_CANCEL]), RN_STOP_HINT); return
    w, sides, err = rn_stop_parse(a)
    if err:
        await retry(f"{E.WARN} {err}"); return
    allk = w == "ALL"
    if allk:
        groups = rn_groups()
    else:
        mine = [T for T in rn_groups() if T["sym"] == w]
        if not mine:
            await retry(f"{E.WARN} {w} 沒有在跑 runt"); return
        groups = [T for T in mine if T["dir"] in sides]
        if not groups:
            await retry(f"{E.WARN} {w} 沒有在跑 {sides}(目前方向 {''.join(T['dir'] for T in mine)})"); return
    B, kinds = [], []                                      # 每一組一段
    now = time.time()
    for T in groups:
        if RN.get(T["key"]) is not T:
            continue
        k, L = rn_stop_one(T, now)
        kinds.append(k); B.append(L)
    rn_save()
    stop_like = any(k == "stop" for k in kinds)
    last_like = any(k in ("last", "again") for k in kinds)
    if last_like and not stop_like:
        title = f"🚦 runt 最後一輪｜{ACCT}"
    elif last_like:
        title = f"🚫 runt 停止／🚦 最後一輪｜{ACCT}"
    else:
        title = f"🚫 runt {'全部停止' if allk else '停止'}｜{ACCT}"
    for part in tg_pack(title, [([RT_SEP] if i else []) + b for i, b in enumerate(B)], [f"時間:{hhmmss()}"]):   # v10.0：太長分頁
        await reply(u, part)

async def rn_recover(app):
    """重開後恢復：持倉、層單掛單接著跑（關機期間沒有看盤）；埋伏中的用重開當下的現價重新來過。
    v7.6 的存檔（沒有初始埋伏點、沒有層單）也認得：埋伏點當 0.2%，持倉當初始單。
    v9.6：存檔一筆＝一組（幣種＋方向，grp）；v9.5 以前一筆＝一個幣種 → 拆成 L、S 兩組（同參數）：
    有持倉的照樣接著打（🚦 也保留）、沒持倉還在跑的用現價重新埋伏、已經停掉的不建立。"""
    rn_day_load()                                      # 本日已實現（v8.2）
    rn_log_prune()                                     # 逐單出場紀錄只留 30 天（v8.7）
    try:
        if not os.path.exists(RN_FILE):
            return
        data = json.load(open(RN_FILE))
    except Exception as e:
        print("[runt] recover read fail", e); return
    names = []; bad = []; chat0 = None
    D = lambda v: None if v is None else Decimal(v)
    for d in data:
        try:
            chat0 = chat0 or d.get("chat")
            sym = d["sym"]
            base = {"sym": sym, "lev": Decimal(d["lev"]), "amt": Decimal(d["amt"]), "off": Decimal(d.get("off", "0.2")),
                    "xx": int(d.get("xx") or 0), "mmr": D(d.get("mmr"))}   # v9.9：倍數（舊存檔 true＝XX）
            dsv, lv = d.get("dir", "LS") or "", d.get("last")
            pos_sides = rn_ls("".join(x["side"] for x in d.get("pos") or []))
            if d.get("grp"):                               # v9.6 的存檔：一筆＝一組（幣種＋方向）
                groups = [(dsv, bool(lv))]
            else:                                          # v9.5 以前：一筆＝一個幣種（LS 綁在一起）→ v9.6 拆成 L、S 兩組
                if lv is True:                             # v9.3：整個幣種 🚦
                    active, lastsides = "", pos_sides or rn_ls(dsv)
                elif isinstance(lv, str):                  # v9.4～v9.5：dir＝還在跑的方向、last＝🚦 的方向
                    active, lastsides = rn_ls(dsv), rn_ls(lv)
                else:
                    active, lastsides = rn_ls(dsv), ""
                groups = []
                for x in "LS":
                    if x in lastsides or (x in pos_sides and x not in active):
                        if x in pos_sides:
                            groups.append((x, True))
                        else:
                            names.append(f"🚦{sym} {x} 最後一輪已全部出場,策略結束")
                    elif x in active:
                        groups.append((x, False))
            spec = await get_spec(sym)
            if base["mmr"] is None:
                base["mmr"] = await rn_mmr(spec)
            st = d.get("st") or {}
            px = None
            for x, last in groups:
                if rn_key(sym, x) in RN:
                    continue
                T = rn_new({**base, "dir": x}, spec, d["chat"])
                T["last"] = last
                T["t_run"] = float(d["t_run"]) if d.get("t_run") is not None else None   # v10.1：舊存檔沒有 → 策略結束時用逐單紀錄回推
                T["st"] = {"r": int(st.get("r", st.get("n", 0))), "n": int(st.get("n", 0)), "u": Decimal(st.get("u", "0"))}
                fee1 = rn_notional(T) * (RN_FEE_IN + RN_FEE_OUT) / 100       # v8.1 以前的存檔沒有毛損益：用淨損益＋每單手續費 0.07% 補回
                T["st"]["gu"] = Decimal(st["gu"]) if "gu" in st else T["st"]["u"] + fee1 * T["st"]["n"]
                pos = []
                for y in d.get("pos") or []:
                    if y["side"] != x:
                        continue
                    pos.append({"side": y["side"], "lvl": int(y.get("lvl", 0)), "ent": Decimal(y["ent"]), "t_in": float(y["t_in"]),
                                "qty": Decimal(y["qty"]), "sl": D(y.get("sl")), "tp": D(y.get("tp")),
                                "sl_g": D(y.get("sl_g")), "tp_g": D(y.get("tp_g")), "pend": None,
                                "t_arm": y.get("t_arm"), "arm_px": D(y.get("arm_px")), "n_mv": int(y.get("n_mv", 0)), "n_tp": int(y.get("n_tp", 0)),
                                "hi": D(y.get("hi")), "mfe": Decimal(y.get("mfe", "0")), "mae": Decimal(y.get("mae", "0")),
                                "fu": Decimal(y.get("fu", "0")), "fn": int(y.get("fn", 0)),
                                "lev": D(y.get("lev")) or rn_lev(T, int(y.get("lvl", 0))), "liq": D(y.get("liq")),
                                "told": True, "out": None})
                    if pos[-1]["liq"] is None and y.get("lev") is None:          # v9.2 以前的存檔：補算預估強平價
                        pos[-1]["liq"] = rn_liq(T, pos[-1]["side"], pos[-1]["ent"], pos[-1]["lev"])
                if pos:                                    # 持倉：接著跑
                    now = time.time()
                    init = {k: Decimal(v) for k, v in (d.get("init") or {}).items() if k == x}
                    for q in pos:
                        if q["lvl"] == 0:
                            init.setdefault(q["side"], q["ent"])
                    h0 = float(d["h0"]) if d.get("grp") and d.get("h0") is not None else min(q["t_in"] for q in pos)
                    T["lev_now"] = D(d.get("lev_now")) if d.get("grp") else None   # v9.8；舊存檔＝持倉裡最高的那一層
                    T.update({"t0": float(d["t0"]), "px0": Decimal(d["px0"]), "pos": pos, "legs": {}, "init": init,
                              "cxl_due": None, "nxt": 0.0, "h0": h0,
                              "ords": [{"side": o["side"], "lvl": int(o["lvl"]), "px": Decimal(o["px"]), "live": now,
                                        "t": float(o.get("t", now))} for o in d.get("ords") or [] if o["side"] == x],
                              "rnd": [{**r, "ent": Decimal(r["ent"]), "net": Decimal(r["net"]), "netu": Decimal(r["netu"]),
                                       "gu": Decimal(r["gu"]) if "gu" in r else Decimal(r["netu"]) + fee1,
                                       "fu": Decimal(r.get("fu", "0")),
                                       **{k: Decimal(r[k]) for k in ("g", "fund", "mfe", "lev") if r.get(k) is not None}}   # v10.1
                                      for r in d.get("rnd") or [] if r.get("side", x) == x]})
                    T["note_last"] = rt_note_base(T["h0"])
                    rn_start(app, T)
                    names.append(f"{rn_par(T)}|持倉 {len(pos)} 單" + (f"|掛單 {len(T['ords'])} 張" if T["ords"] else ""))
                    continue
                if last:                                   # v9.3：🚦 最後一輪已經全部出場 → 策略結束，不再恢復
                    names.append(f"🚦{sym} {x} 最後一輪已全部出場,策略結束")
                    continue
                if d.get("wait_until") and float(d["wait_until"]) > time.time():   # 本輪結束後的 300 秒還沒等完：等完剩下的
                    T.update({"t0": float(d["t0"]), "px0": Decimal(d["px0"]), "wait_until": float(d["wait_until"]), "nxt": 0.0})
                    rn_start(app, T)
                    names.append(f"{rn_par(T)}|等完剩下 {int(T['wait_until'] - time.time())} 秒再埋伏")
                    continue
                for _ in range(3 if px is None else 0):    # 埋伏中：用重開當下的現價重新來過
                    try:
                        px = await get_last(spec["iid"]); break
                    except Exception:
                        await asyncio.sleep(1)
                if px is None:
                    bad.append(rn_par(T)); continue
                rn_round(T, time.time(), rt_q(px, spec["tick"]))
                rn_start(app, T)
                names.append(f"{rn_par(T)}|用現價重新埋伏")
        except Exception as e:
            print("[runt] recover fail", d, type(e).__name__, e)
            bad.append(f"{d.get('sym')}")
    rn_save()
    L = []
    if names:
        L += [f"{E.BOT} runt 已自動恢復(bot 重開)"] + names + ["持倉的 SL/TP、掛單不變,關機期間沒有看盤"]
    if bad:
        L += [f"{E.WARN} 以下沒有恢復,請重新下指令:"] + bad
    if L and chat0:
        L.append(f"時間:{hhmmss()}")
        await rt_send(app, chat0, "\n".join(L))

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

COINS_HIDE = ("HYPEUSDT", "XAUUSDT", "ZECUSDT", "PEPEUSDT")   # v10.1（1111）：/coins 不列（只是不列，/runt 照樣可以用）；v10.4 加 PEPEUSDT
# v10.5（1111）：/coins 多列這 10 個（2026-10-07 查過 OKX 都有 USDT 永續、都在交易中）。跟 symbols.json 合起來、拿掉 COINS_HIDE，照字母排序。
COINS_ADD = ("PUMPUSDT", "NIGHTUSDT", "ETHFIUSDT", "ENAUSDT", "APTUSDT", "JUPUSDT", "ARBUSDT", "ONDOUSDT", "AEROUSDT", "ASTERUSDT")

async def coins_acct_lines():
    """v10.1（1111：下策略時要知道帳戶還有多少錢）：交易帳戶 USDT 四行，當下向 OKX 查（/api/v5/account/balance），名稱照 OKX：
    餘額＝cashBal、權益＝eq（餘額＋未實現損益）、可用＝availBal（還能拿去下單）、佔用＝frozenBal（持倉保證金＋掛單）。
    帳戶裡沒有 USDT＝四行都是 0；查詢失敗就一行說明。"""
    try:
        r = await api("GET", "/api/v5/account/balance?ccy=USDT")
        if str(r.get("code")) != "0":
            raise ValueError(f"code={r.get('code')} {r.get('msg', '')}")
        x = next((d for d in (r.get("data") or [{}])[0].get("details") or [] if d.get("ccy") == "USDT"), {})
    except Exception as e:
        print("[coins] balance fail", type(e).__name__, e)
        return ["交易帳戶(USDT) 查詢失敗,稍後再試"]
    D = lambda k: Decimal(x.get(k) or "0")
    rows = [("餘額", D("cashBal")), ("權益", D("eq")), ("可用", D("availBal")), ("佔用", D("frozenBal"))]
    vs = rn_pad([f"{rn_q4(v):.4f}U" for _, v in rows])
    return ["交易帳戶(USDT)"] + [f"{k} {v}" for (k, _), v in zip(rows, vs)]

async def cmd_coins(u, c):
    fng = asyncio.ensure_future(fng_lines())                 # v9.7：恐懼貪婪指數跟幣種一起查
    acct = asyncio.ensure_future(coins_acct_lines())         # v10.1：交易帳戶放最前面
    on = sorted(({s["symbol"] for s in SYMS if s["enabled"]} | set(COINS_ADD)) - set(COINS_HIDE))   # v10.5：加 COINS_ADD，不重複
    L = [f"{E.BOT} OKX原K｜{ACCT}", "事件：幣種清單（即時）", "━━━━━━━━━━"] + await acct
    L += ["━━━━━━━━━━", "幣種｜最小保證金(1X)｜最大槓桿"]
    for sym in on:
        try:
            sp = await get_spec(sym); last = await get_last(sp["iid"])
            mm = sp["minsz"] * sp["ctval"] * last          # 最小下單量的價值＝1X 的最小保證金（nX 時 ÷n）
            L.append(f"{sym}｜{mm:.4f}U｜{pct(sp['maxlev'])}X")   # v9.9（1111）
        except Exception:
            L.append(f"{sym}｜查詢失敗")
    L += ["━━━━━━━━━━"] + await fng + ["━━━━━━━━━━", f"時間：{hhmmss()}"]
    await reply(u, "\n".join(L))

async def cmd_timeframe(u, c):
    global ACCOUNT_TF
    if not c.args:                                  # v7.5：先顯示目前週期，等你直接輸入新週期
        await ask(u, "tf", cmd_timeframe, f"{E.BOT} 目前週期:{ACCOUNT_TF}\n可選:" + "/".join(TF_SEC.keys())
                  + "\n要改請在60秒內直接輸入週期(例 3m),不改就不用理", "3m", force=False); return   # v8.2：不跳回覆列
    tf = c.args[0].lower()                          # v5.1：/tf 3M 也認得
    if tf not in TF_SEC:
        await ask(u, "tf", cmd_timeframe, f"{E.WARN} 週期須為:" + "/".join(TF_SEC.keys()) + "\n請重新輸入週期(例 3m)", "3m"); return
    ACCOUNT_TF = tf; save_state()
    # v5.1：更正說明 —— /run 的策略沒有各自記週期，一律跟著帳戶週期走，所以是「立即」改用；
    #       v9.7：/test1 刪除，回覆拿掉 /test1 那一行。
    await reply(u, f"{E.BOT} {E.OK} 帳戶週期已設為 {tf}\n/run 立即改用 {tf}")

async def cmd_menu(u, c):
    """/menu 說明。v10.8：舊 /run（A/B 跨式）的說明、【戰術】【緊貼】段拿掉。"""
    await reply(u, f"{E.BOT} OKX原K｜{ACCT} {VERSION}\n使用說明\n━━━━━━━━━━\n"
        "/status 所有 /runt 現況\n/summary 本日 /runt 戰報（總表＋分幣種）；每天 00:00 自動發前一天的\n"
        "/price 幣種　價格階梯（只查價，不掛單）：S9～S0／現價／L0～L9，每一層離上一層 0.5%\n"
        "/runt 幣種 方向 槓桿 保證金 初始埋伏點%\n"
        "　方向：LS 兩邊｜L 只做多｜S 只做空。槓桿：1X～100X 每一層一樣｜級距槓桿 XX1～XX5，第 n 層＝(n+1)×倍數：XX1＝L0 1X…L9 10X｜XX2＝L0 2X…L9 20X｜XX3＝L0 3X…L9 30X｜XX4＝L0 4X…L9 40X｜XX5＝L0 5X…L9 50X（每一層保證金一樣，幣種最高槓桿要夠 L9）\n"
        "　L、S 各自一組、完全獨立（LS＝一次開兩組，同參數）。佈局（模擬，不下單）：初始單 L0（現價下方）／S0（現價上方）「初始埋伏點%」限價埋伏，不改價。"
        "L0/S0 虧損就掛同方向 L1/S1，L1/S1 也虧損就掛 L2/S2…最多 L9/S9；每一層離上一層 0.5%。"
        "每一單最高毛利率 >0.20% SL 先卡 +0.10%；>0.30% 框架形成：SL＝最高−0.20%、TP＝最高+0.15%，之後一起往獲利移動；某一層出場撤掉比它深的掛單，L0/S0 出場撤掉沒成交的單，全部出場後等 300 秒重新來過（有事件才通知，狀態用 /status 看）。"
        "損益 % ＝ U ÷ 每單保證金（可以相加）；毛利率＝價格漲跌%。"
        "強平每一層各自算（OKX 逐倉預估強平價，碰到＝那一層強平、損失那一層保證金）；/status 沒設 SL 的那一層 📍＝離強平價的 %。"
        "損益分持倉中／本日已實現（L、S 分開，台灣時間換日）；資金費照 OKX 費率模擬計入\n"
        "　例：/runt WLDUSDT LS 1X 1U 0.5%、/runt WLDUSDT L XX1 1U 0.5%、/runt WLDUSDT S XX3 1U 0.5%　｜可同時跑多個幣種｜同幣種同方向只能一組\n"
        "/stoprunt 幣種 方向｜all　方向 LS 兩邊／L 只停多／S 只停空；all＝全部幣種兩邊。"
        "沒有進場＝馬上停止；有進場＝🚦最後一輪（這一輪照常作戰，全部出場後那一組結束，BOT 不平倉）；停一邊，另一邊照常\n"
        "/tf 查看/設定週期\n/coins 交易帳戶(餘額｜權益｜可用｜佔用)＋幣種｜最小保證金(1X)｜最大槓桿＋恐懼貪婪指數(近7天)\n"
        "/run /stop 實盤準備中(目前用 /runt)\n"
        "/check 健檢｜/check api｜/check log\n"
        f"━━━━━━━━━━\n週期 {ACCOUNT_TF}｜行情 WS {ws_status()}")

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

RUN_SOON = (f"{E.BOT} 實盤 /run、/stop 準備中\n"
            "v10.8 已拿掉舊的 A/B 跨式 /run;新的實盤版(/runt 搬到 OKX 真的下單)完成後放在這裡\n"
            "目前請用 /runt、/stoprunt(模擬)")

async def cmd_run_w(u, c):
    """v10.8：舊 /run（A/B 跨式）拿掉；實盤 /run 做好之前先回說明。Menu 位置保留（1111 v10.3 定的順序）。"""
    await reply(u, RUN_SOON)

async def cmd_stop_w(u, c):
    """v10.8：舊 /stop 拿掉（同 /run）。/runt 的停止是 /stoprunt。"""
    await reply(u, RUN_SOON)


async def job_summary(ctx):
    """v8.7（1111）：每天台灣時間 00:00 自動發前一天完整 24 小時的 /summary（/runt）；前一天什麼都沒有就不發。"""
    chat = CHAT_ID or next((T["chat"] for T in RN.values()), None)
    if not chat: return
    d = (now8() - timedelta(minutes=5)).strftime("%Y-%m-%d")
    try:
        for m in await rn_sum_msgs(d, full=True):
            for part in tg_split("\n".join(m)):           # v10.0：太長分頁
                await ctx.bot.send_message(chat, part)
    except Exception as e: print("auto summary fail", e)

# ---------- 啟動 ----------
async def _post_init(app):
    global HTTP
    HTTP = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), limits=httpx.Limits(max_connections=40))
    load_state()              # v10.8：聊天室、/tf 週期（原本在舊 /run 的 startup_recover）
    # v10.3（1111 2026-10-07）：左下 Menu 順序 status、summary、coins、price、run、stop、runt、stoprunt、timeframe、check、menu
    #   （週期改列 /timeframe；/tf 照樣可以打）
    CMDS = [BotCommand("status", "現況"),
            BotCommand("summary", "本日戰報（runt）"),
            BotCommand("coins", "帳戶＋幣種＋恐懼貪婪指數"),
            BotCommand("price", "價格階梯（只查價）"),
            BotCommand("run", "實盤（準備中）"),
            BotCommand("stop", "實盤停止（準備中）"),
            BotCommand("runt", "佈局（模擬，不下單）"),
            BotCommand("stoprunt", "停止runt｜幣種 方向或all"),
            BotCommand("timeframe", "週期"),
            BotCommand("check", "健檢｜api｜log"),
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
            t0010 = datetime.strptime("00:00:10", "%H:%M:%S").time().replace(tzinfo=TZ8)   # v8.7：00:00 發前一天完整的
            jq.run_daily(job_summary, time=t0010, name="daily_summary")
            print("已排程：每日 00:00 自動 /summary（前一天）")
    except Exception as e:
        print("schedule fail", e)
    # WebSocket：逐筆成交價（/runt 判斷成交、SL/TP 出場）。斷線自動降級回 REST 查價。
    asyncio.create_task(ws_public_task())
    print(f"/runt 移動任務已啟動（{VERSION}）")   # deploy5.sh 用這一行確認版本（v10.8 以前是舊 /run 的 frame_mover 印的）
    await rn_recover(app)     # v7.6／v7.7：/runt 佈局

async def _post_stop(app):
    global SHUTTING_DOWN
    save_state()          # 關閉前最後一次存檔（聊天室、/tf 週期）
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
                    ("run", cmd_run_w), ("stop", cmd_stop_w),                # v10.8：實盤準備中（舊 A/B /run、/confirm 拿掉）
                    ("status", cmd_status), ("summary", cmd_summary),
                    ("check", cmd_check), ("price", cmd_price),             # v10.8：/log /selftest /test2 舊別名拿掉
                    ("runt", cmd_runt), ("stoprunt", cmd_stoprunt),          # v7.6／v7.7：/runt＝佈局（模擬）
                    (["tf", "timeframe"], cmd_timeframe), ("coins", cmd_coins)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, on_text))   # v7.5：直接打參數
    app.run_polling()

if __name__ == "__main__":
    main()

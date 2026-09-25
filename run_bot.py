#!/usr/bin/env python3
# 設計此腳本的目的在於用bot取代我在交易所app上的一切手動行為，切記
"""B6-1 原K｜多帳戶 — v4.3 緊貼度參數化

【戰術】
  同時掛 A限價 + B觸發（反向、同量）。兩單都成交時完全對沖，損益鎖死 = -兩單間距，
  與價格無關。價格衝出箱子時一邊被SL掃掉、一邊獨活順勢起飛。
  目標順序：先求打平，再求小勝；大勝靠機構把價格打出控制範圍。

【緊貼只有兩個模式】—— 全部實作在 sl_decide() 一個純函式裡，別處不准自己判斷
  開關只有一個：這一場戰役【B單觸發過沒有】。時間不是訊號。

  模式一　B單還沒觸發（只有 A 在場）＝ 按兵不動
    規則1 價格不動            → 不動
    規則3 往SL                → 不動。不怕碰到靜態SL —— 間距(0.1%)一定比
                                靜態SL(0.4%)先被打到，對沖會先成形。
    規則3 往TP 未達手續費率     → 不動。局勢不明朗，別把保護單擋在門外。
    規則2 往TP 超過手續費率     → 緊貼到 0.1%。
      比較的數字就是 A單來回手續費率 FEE_A = 0.070%，沒有另一個常數。

  模式二　B單觸發過（閂，不翻回去）＝ 全面緊貼
    規則2 往TP → 緊貼鎖利　　　規則4 往SL → 緊貼減損
    兩邊一起貼，夾出 0.2% 的壓縮帶；誰被擠出去，另一單就獨走。
    對沖成形後損益和鎖死 = -間距，勝負全在「落單者獨走多遠」。

  SL 一律 = 現價 ∓ 緊貼距離，【不判斷峰值】。
  唯一限制：只准拉近，不准放鬆（否則 SL 會跟著價格跑，永遠不會出場）。
  方向以「現價 vs 進場價」認定，不看逐筆漲跌 —— 逐筆會被雜訊左右。
  靜態SL 只是個基準：第一次緊貼就被取代，OKX 上始終只有一張 SL。

【持倉暫存器】A格/B格，一律以 OKX 查回來的持倉為準，不看內部旗標。
  現值總和歸零（且本場曾經>0）→ 戰役結束、撤單重新部署
  B格曾經=1 → 緊貼模式二（閂：只從一翻到二，戰役結束才歸零）

【資料鐵則】凡走過必留下痕跡
  每一次「沒有緊貼」都要留下名字與次數（skip_n），寫進交易紀錄。
  贏要知道怎麼贏，輸要知道怎麼輸 —— 不明不白的數據等於沒有數據。

【鐵則】
  1. OKX 為唯一真相來源：撤單、持倉、損益一律回查 OKX 確認。
  2. 下單必帶 TP/SL（attachAlgoOrds），成交當下即生效，無裸倉空窗。
  3. 重啟時接管 OKX 上的既有持倉與掛單，不留孤兒。
  4. Telegram 與 WebSocket 皆為旁路：失效絕不影響交易與保護。
  5. 守門狗只告警不自動平倉；唯一的程式自動平倉是上面那條「越界平倉」。
"""
import sys, hmac, base64, hashlib, json, time, asyncio, uuid, os, re, builtins
from collections import deque
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
import httpx
from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, MessageHandler, filters
sys.path.insert(0, "/srv/1111bot")
from app.core import emoji as E
from app.strategy.normal import next_open_epoch as _noe_unused, TF_SEC as _TFS_unused

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
TF_SEC = {"5m": 300, "6m": 360, "8m": 480, "10m": 600,
          "12m": 720, "15m": 900, "20m": 1200, "25m": 1500, "30m": 1800}

def next_open_epoch(now_epoch, tf):
    sec = TF_SEC[tf]
    return ((now_epoch // sec) + 1) * sec

VERSION = "v4.8"     # 腳本版本號：回報問題時請附上（/status 最後一行顯示）
BASE = "https://www.okx.com"
ACCT = os.environ.get("ACCT", "o3333o")  # 由 systemd 注入
TZ8 = timezone(timedelta(hours=8))
ACCOUNT_TF = "5m"
STATE_FILE = f"/srv/1111bot/data/strategies_{ACCT}.json"
NAKED_ALERT_SEC = 15  # 守門狗：有倉但未掛止盈止損超過 N 秒 → 只發一次 TG 告警（絕不自動平倉）

# ---------- 交易所相關常數（換交易所時改這裡，不是改參數） ----------
# 來回手續費率。OKX 永續合約普通用戶：掛單 maker 0.020%、吃單 taker 0.050%。
#   A單 = 限價埋伏進場(maker) + 市價出場(taker) = 0.070%
#   B單 = 觸發市價進場(taker) + 市價出場(taker) = 0.100%
# 【對過帳，不是查表】2026-09-21 SUI 實單：
#   進場 0.9711 × 0.020% = 0.00019422
#   出場 0.9715 × 0.050% = 0.00048575   合計 0.00068
#   OKX 回傳的 fee 欄位正是 -0.000680 —— 完全吻合。
# 【為什麼拿不到 maker+maker 的 0.040%】出場一律市價（tpOrdPx/slOrdPx = -1）。
# 要 maker 出場就得掛限價 SL，而限價 SL 可能掛不上去 = 裸奔，違反下單鐵則。
FEE_A = Decimal("0.00070")   # A單 0.070%
FEE_B = Decimal("0.00100")   # B單 0.100%
FEE_TOTAL = FEE_A + FEE_B    # 一場雙邊成交戰役的固定成本 0.170%

# ========== 緊貼節拍（v3.10：整套戰術的獲利關鍵就是這裡） ==========
# 【為什麼要快】SL = 現價 ∓ 0.1%，取樣越密，越貼近真正的針尖。
# 一根 3% / 3秒 的針，價格每秒走 1%：
#   取樣 0.5 秒 → 最多落後真峰 0.5%（比緊貼距離本身還大）
#   取樣 0.2 秒 → 最多落後 0.2%
# 針越猛，取樣間隔的代價越大 —— 而針正是這套戰術唯一的獲利來源。
#
# 【為什麼不再更快】OKX amend-algos 限 20 次/2秒。所有幣種、A/B 兩側的
# amend 會合併成【一個】批次請求，所以每個心跳最多送 1 次：
#   0.2 秒心跳 → 5 次/秒 = 10 次/2秒，用掉額度的一半，留一半餘裕。
#   0.1 秒心跳 → 20 次/2秒，正好踩滿 —— 一旦 50011，緊貼會整個停擺，
#                比慢一點更糟。穩定優先於極速。
MOVE_TICK      = 0.2   # frame_mover 心跳（秒）← 緊貼速度由這一行決定
# 【必須小於 MOVE_TICK】這是每一側的送單節流。
# v3.9 以前兩者都是 0.5：節流的起算點是「週期開始時刻」，和下一個週期的
# 起算點剛好相差一個 MOVE_TICK，浮點抖動就會讓 (now - last) 略小於門檻
# → 整個週期被靜默跳過。實測 40 個週期被吃掉 8 個，有效節拍變成 0.62 秒。
# 設成心跳的一半，永遠不會誤殺，而且仍然擋得住同一週期內的重複送單。
PRICE_TICK_SEC = 0.1
MIN_HUG_TICKS  = 3     # 緊貼距離下限（檔）。低於此值會被買賣價差直接掃掉
AMEND_BACKOFF  = (1, 2, 4, 8)  # amend 連續失敗的退避秒數（問題18：不再每秒無限重試）
AMEND_COOL     = 2.0   # amend 撞到 50011 後，整個緊貼泵冷卻幾秒（降速，不停擺）

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
SAVE_FIELDS = ("sym","dir","lev","margin","offset","gap","tp","sl","hug","hug_auto","chat",
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
# 【v3.10】心跳加快到 0.2 秒後，TTL 若仍是 0.4 秒會變成「每兩拍打一次網路」，
# 讓一半的緊貼週期要等持倉查詢回來。拉到 0.5 秒：每 5 拍才打一次（2 次/秒 =
# 4 次/2秒，額度 10 次/2秒，留 60% 餘裕），其餘 4 拍純讀快取、零延遲。
# 出場判定不受影響 —— _confirm_gone 一律 force=True，繞過快取。
POS_TTL   = 0.5
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

# 【v3.8】這些 sCode 代表「這張 algo 單天生改不動」，重試沒有意義，
# 唯一的解是作廢它、自己重掛一張。51506 由 /apitest 在 L/S 兩個方向實測確認。
AMEND_DEAD_CODES = {"51506"}
# 【v3.13】邊界錯誤 ≠ 壞單。這兩碼代表「要放的 SL 已經在現價的錯誤一側」，
# 由 /apitest 在 SUIUSDT 實測確認：
#   51280 做多：SL trigger price must be less than the last price
#   51278 做空：SL trigger price cannot be lower than the last price
# 成因是【送單往返期間價格又動了】—— 心跳 0.2 秒、針一秒走 1%，
# 算的時候合法、到 OKX 時已經越界。這在針來的時候本來就會發生。
# 正確反應：下一拍用新價重算（本來就會），不計入失敗、不退避，
# 更不可以因為連續三次就把一張好好的 OCO 當廢單撤掉重掛 ——
# 那是在針的正中央製造保護空窗。v3.9~v3.12 有這個缺陷，這裡修掉。
AMEND_SOFT_CODES = {"51280", "51278"}
AMEND_DEAD_FAILS = 3        # 連續「非邊界」失敗這麼多次，才當成廢單處理
AMEND_COOL_UNTIL = 0.0      # 撞到 50011 後的冷卻截止時刻（全域，所有幣種共用）


async def amend_frames(items):
    """批次修改 algo 單觸發價。
    items 元素可為 3 元組 (instId, algoId, sl) → 只改 SL（移動SL 用，TP 不動）；
    或 4 元組 (instId, algoId, sl, tp) → SL/TP 一起改（依實際成交價校正用）。
    OKX 一次最多 10 筆，超過自動分批。

    【問題10】回傳「成功的 algoId 集合」，不是筆數。
    舊版用一個布林判定整批成敗：同批任一張失敗，OKX 回 code!=0，
    其餘明明成功的也被標成失敗 → 程式內部 SL 值與 OKX 實際值失聯。
    改成逐筆看 data[i].sCode，各自認帳。

    【v3.8】回傳 (成功集合, 永久失敗集合)。
    51506 = Order modification unavailable for the order type ——
    這是「這張單天生不能改」，不是暫時性失敗，重試一萬次也是一樣。
    B單的母單是 trigger，OKX 自動生成的附屬 OCO 就是這種（/apitest 已實測）。
    舊版把它當一般失敗，於是無限重試＋退避，B 整場緊貼 0 次。
    挑出來讓呼叫端把它作廢、重掛一張自己的。"""
    global AMEND_COOL_UNTIL
    okset = set(); deadset = set(); softset = set()
    for i in range(0, len(items), 10):
        batch = items[i:i+10]
        body = []
        for it in batch:
            e = {"instId": it[0], "algoId": it[1], "newSlTriggerPx": str(it[2])}
            if len(it) >= 4 and it[3] is not None:
                e["newTpTriggerPx"] = str(it[3])
            body.append(e)
        r = await api("POST", "/api/v5/trade/amend-algos", body)
        # 【v3.10】撞到速率限制就降速，不是停擺。緊貼慢一點還能救，
        # 整個停掉就是 WIF 第12輪重演。
        if str(r.get("code")) == "50011":
            AMEND_COOL_UNTIL = time.time() + AMEND_COOL
            print(f"[緊貼降速] amend 撞到 50011，冷卻 {AMEND_COOL} 秒")
        data = r.get("data") or []
        for j, it in enumerate(batch):
            d = data[j] if j < len(data) else {}
            sc = str(d.get("sCode", ""))
            if sc == "0":
                okset.add(it[1])
            elif sc in AMEND_SOFT_CODES:
                # 邊界錯誤：價格在往返途中又動了。下一拍用新價重算即可，
                # 不算失敗、不退避、不作廢。針來的時候這個會很常見。
                softset.add(it[1])
                print(f"amend 邊界 {it[0]} {it[1]} sCode={sc} {d.get('sMsg')}"
                      f"　← 價格在往返中走掉了，下一拍重算")
            else:
                if sc in AMEND_DEAD_CODES:
                    deadset.add(it[1])
                print(f"amend fail {it[0]} {it[1]} sCode={sc} {d.get('sMsg')}"
                      + ("　← 永久失敗，將作廢重掛" if sc in AMEND_DEAD_CODES else ""))
        if r.get("code") not in ("0", "2") and not data:
            print("amend_frames 整批失敗", r.get("msg"))
    return okset, deadset, softset

# ==================== 緊貼決策：唯一真相來源 ====================
# 【v4.2 把 30 秒拿掉，改用「B單觸發過沒有」當開關】
# 時間從來不是訊號，B單有沒有被觸發才是。整場戰役只有兩個模式：
#
# ── 模式一　B單還沒觸發過（只有 A 在場）＝ 按兵不動 ────────────
#   往SL          → 不動。不怕碰到靜態SL，因為 B 的觸發價（間距 0.1%）
#                   一定比靜態SL（0.4%）先被打到，對沖會先成形。
#                   護欄在 cmd_run：間距 >= SL% 直接拒絕開單。
#   往TP 未達手續費率 → 不動。局勢不明朗，別亂動 SL 把保護單擋在門外。
#   往TP 超過手續費率 → 緊貼到 0.1%。比較的就是 A單來回手續費 0.070%。
#
# ── 模式二　B單觸發過（閂，不翻回去）＝ 全面緊貼 ────────────────
#   往TP → 緊貼（鎖利）
#   往SL → 緊貼（減損）      ← 兩邊一起貼，夾出 0.2% 的壓縮帶
#   對沖成形後兩單損益和鎖死 = -間距，勝負全在「落單者獨走多遠」，
#   所以落難方要快點被擠出去，得勢方才能早點獨走。
#
# 【閂為什麼不能翻回去】壓縮帶把 B 擠出場後暫存器從 2 掉回 1，
# 但那一刻 A 正在獨走 —— 那一段才是勝負所在。模式若跟著掉回一，
# A 會剛好在最關鍵的時候停止緊貼。所以只從一翻到二，戰役結束才歸零。
#
# 【不判斷峰值】SL 一律 = 現價 ∓ 緊貼距離，不是峰值 ∓ 緊貼距離。
# 唯一的限制是「只准拉近，不准放鬆」——若允許放鬆，SL 會一路跟著
# 價格往回跑，永遠不會出場。
#
# 【方向怎麼認】拿現價跟進場價比，不看上一筆的漲跌。
# 逐筆比較會被雜訊左右（同一秒可能一上一下），拿進場價當基準才穩定：
#   做多 現價>進場價 = 往TP；現價<進場價 = 往SL；相等 = 不動。做空鏡像。
#
# 這是一個【純函式】：不碰 S、不碰網路、不改任何狀態。
# 給一組數字，回答該做什麼 —— 所以能離線跑十萬組驗證（/check rule）。


def sl_decide(d, entry, cur, cur_sl, F, tick, hedged, gate):
    """緊貼決策。回傳 (action, price, reason, rule)

      action = "hold" 不動 ／ "move" 把 SL 移到 price
      rule   = 命中第幾條規則（1~4，0=棘輪擋下）

    參數全為純數值，不依賴任何全域狀態。
      d        "L"/"S"   這一單自己的方向（A/B 各自算，不共用）
      entry    進場成交價
      cur      現價
      cur_sl   目前生效的 SL（第一次 = 靜態SL；靜態SL 只是個基準，
               第一次緊貼就被取代掉，OKX 上從頭到尾只有一張 SL）
      F        緊貼距離（0.001 = 0.1%）
      hedged   這場戰役 B單觸發過沒有。True = 模式二，全面緊貼。
               B單自己在場時恆為 True（觸發進場，方向已確定）。
      gate     模式一比較用的手續費率（0.0007 = 0.070%，呼叫端傳 FEE_A）。
               只在模式一、且往TP 時才會用到；模式二完全不看。
    """
    try:
        e = Decimal(str(entry)); c = Decimal(str(cur)); sl = Decimal(str(cur_sl))
        g = Decimal(str(gate))
    except Exception:
        return ("hold", None, "資料不全", 0)
    if e <= 0 or c <= 0:
        return ("hold", None, "資料不全", 0)
    L = (d == "L")

    # 規則1：價格不動 → SL 不動
    if c == e:
        return ("hold", None, "規則1 價格不動", 1)

    toward_tp = (c > e) if L else (c < e)

    # ── 模式一：B 還沒觸發過 —— 按兵不動，把戰場留給保護單 ──
    if not hedged:
        if not toward_tp:
            return ("hold", None, "規則3 按兵不動·往SL等保護單", 3)
        gain = ((c - e) / e) if L else ((e - c) / e)
        if gain <= g:
            return ("hold", None,
                    f"規則3 按兵不動·獲利{gain * 100:.3f}%未達手續費率"
                    f"{g * 100:.3f}%", 3)
        # 走到這裡 = 獲利超過手續費率，往下走規則2 的緊貼

    # 規則2（往TP）與規則4（模式二往SL）落點相同：現價 ∓ 緊貼距離
    tgt = align(c * (Decimal("1") - F), tick, "S") if L else \
          align(c * (Decimal("1") + F), tick, "L")

    # 【安全側保險 v4.2】tick 粗到比緊貼距離還大時（例：tick=1、價格 660、
    # 0.1% 只有 0.66），對齊之後 SL 會落到現價的【另一側】—— 一送上去
    # OKX 立刻觸發，當場市價平倉。隨機測試 12 萬組抓到 41 筆。
    # 正常跑不會發生（hug_pct() 有 MIN_HUG_TICKS 地板），但 sl_decide 是
    # 唯一真相來源，不能靠呼叫端替它守規矩。
    if tick > 0:
        for _ in range(3):
            if (L and tgt < c) or ((not L) and tgt > c):
                break
            tgt = (tgt - tick) if L else (tgt + tick)
    if tgt <= 0 or (L and tgt >= c) or ((not L) and tgt <= c):
        return ("hold", None, "規則1 tick過粗，SL會越過現價", 0)

    # 只准拉近。擋下來就是「價格在回撤」或「SL 已經更近」——都屬於規則1 的不動。
    if (L and tgt <= sl) or (not L and tgt >= sl):
        return ("hold", None, "規則1 SL已更近，不放鬆", 0)

    if toward_tp:
        return ("move", tgt, "規則2 往TP緊貼", 2)
    return ("move", tgt, "規則4 對沖中·往SL緊貼減損", 4)


def reg_update(S, cur_a, cur_b):
    """【v4.2 持倉暫存器】A格／B格，各 0 或 1，一律以 OKX 查回來的持倉為準，
    絕不看內部旗標（孤兒倉正是旗標錯的那種）。呼叫端必須先確認查詢成功；
    查詢失敗時整輪跳過，不准拿「不知道」餵進來當「沒有」。

        reg_a / reg_b   這一刻各格的值
        reg_now         現值總和 0/1/2
        reg_max         本場戰役到過的最高水位
        hedged          B格曾經=1 → 緊貼模式二（閂，戰役結束才歸零）

    兩個用途分得很清楚：
      戰役是否結束 → 看【現值】：reg_max>0 而 reg_now==0，兩單都確認無持倉
      緊貼哪個模式 → 看【曾經】：hedged

    回傳 True = 這一刻剛翻成模式二（呼叫端可印一行 log）。"""
    a = 1 if cur_a else 0
    b = 1 if cur_b else 0
    S["reg_a"] = a
    S["reg_b"] = b
    S["reg_now"] = a + b
    S["reg_max"] = max(a + b, int(S.get("reg_max", 0) or 0))
    if b and not S.get("hedged"):
        S["hedged"] = True
        return True
    return False


def _skip(S, side, why):
    """【v3.8】每一次「沒有緊貼」都要留下名字和次數。
    舊版 11 個 continue 裡有 3 個完全靜默 —— 戰術沒執行，log 一個字都沒有，
    只能事後猜。現在 /status 和交易紀錄都看得到。
    印 log 用階梯式（1/10/100/1000…）避免洗版，計數則是每次都加。"""
    c = S.get("skip_n")
    if not isinstance(c, dict):
        c = {}; S["skip_n"] = c
    k = f"{side}|{why}"
    c[k] = c.get(k, 0) + 1
    n = c[k]
    if n in (1, 10, 100, 1000) or n % 5000 == 0:
        print(f"[跳過] {S.get('sym')} {side} {why} ×{n}")


# 【沒有主動平倉】SL 一律由現價算出，永遠落在現價的安全側，
# 不會出現「算出來的 SL 越過現價」那種狀況，所以不需要市價平倉的逃生口。
# 出場一律由 OKX 上的 OCO 執行 —— 程式死了 SL 照樣守著。


# 【v3.7】緊貼距離改回【固定值】。
# 自動版（均幅×1.5）算出 DOGE 0.134%／SUI 0.347%／WIF 0.242%，實測鎖利太少：
# WIF 峰值 +0.6% 只鎖到 +0.358%，回吐 0.242%；固定 0.1% 可鎖到 +0.500%。
# 均幅仍照算並寫進交易紀錄（/tune 分析要用），只是不再決定緊貼距離。
# 【v4.3】緊貼距離改由 /run 第 7 個參數【緊貼度%】指定，不再是全域常數。
# 理由：1秒K 實測顯示各幣種的雜訊差距極大（ETH 一檔 0.0004%、WIF 一檔 0.0392%，
# 差 98 倍），共用一個 0.1% 在 ETH 上太寬、在 WIF 上會被 3 檔地板頂到 0.115%。
# 下面這個值只剩兩個用途：(a) 舊存檔沒有 hug 欄位時的回退值 (b) 自檢/說明的預設展示。
HUG_FIXED   = Decimal("0.001")  # 緊貼距離預設 0.1%（/run 沒帶或舊存檔才會用到）

# 【沒有第二個常數】模式一（B單還沒觸發）A單往TP 要走超過多少才准緊貼？
# 答案就是 A單的來回手續費率 FEE_A = 0.070%，直接用它，不另外定義。
# 多一個常數就多一個會跟你講的數字不一致的地方。
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
    """該單實際使用的緊貼距離。

    【v4.3】由 /run 的第 7 個參數【緊貼度%】指定，存在 S["hug"]（小數，0.001=0.1%）。
    沒有這個欄位（舊存檔、或重啟接管 v4.2 以前的戰役）→ 回退 HUG_FIXED。
    A/B 共用同一個值，但 tick 地板各用各的參考價各算各的。

    只保留一道 tick 地板：低於 MIN_HUG_TICKS 檔會被買賣價差直接掃掉。
    【取捨】緊貼若低於 B 單的來回手續費 0.100%，B 剛好在這個距離被掃會小虧。
    這是為了多鎖利刻意接受的代價，由你在 /run 決定要不要接受。"""
    F = HUG_FIXED
    try:
        _v = S.get("hug")
        if _v not in (None, ""):
            _v = Decimal(str(_v))
            if _v > 0:
                F = _v
    except Exception:
        pass
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
    # 【v4.3】hug_note 同時記下「你指定的」和「均幅參考值」，事後分析才分得開
    S["hug_note"] = f"指定 {float(hug_pct(S, 'front') * 100):.4f}%｜參考 {_hn}"
    _c = _HUG_CACHE.get(iid)
    S["amp"] = str(_c[0]) if _c else ""     # 進場當下的1分K均幅，事後分析的基準
    print(f"[緊貼] {S['sym']} 指定{float(hug_pct(S, 'front')*100):.4f}%｜{_hn}")

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
    # 【v4.2】暫存器與緊貼模式閂：每一場戰役重新開始。
    # hedged 在戰役期間只會 False→True，絕不翻回去 —— 落單者獨走那一段
    # 才是勝負所在，那時候不能退回按兵不動。歸零的時機只有這裡。
    S["reg_a"] = S["reg_b"] = S["reg_now"] = S["reg_max"] = 0
    S["hedged"]          = False
    S.pop("_pending_front", None)
    S.pop("_pending_back", None)
    # 【v3.8】每場戰役重置：跳過計數、自我修復計數、廢單黑名單
    S["skip_n"]          = {}
    S["fix_n"]           = 0
    S["front_bad_algo"]  = []
    S["back_bad_algo"]   = []
    # 【v4.1】緊貼間隔的計時基準要跟著戰役重置。
    # 不清掉的話，新戰役第一次移動會拿上一場的時刻去減，
    # 跑出 602599ms 這種荒謬數字，把速度中位數整個拉歪。
    S.pop("_last_move_t_front", None)
    S.pop("_last_move_t_back", None)
    S["_need_replace"]   = False
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


async def _okx_oco_id(iid, ps, tries=4, gap=0.2, skip=None):
    """【問題4 配套】向 OKX 索取守護該持倉方向的 OCO(止盈止損) algoId。
    下單時 attachAlgoOrds 已帶 TP/SL，成交當下由 OKX「自動」生成這張 OCO ——
    程式不再自己補掛第二張（那會變成 2 張），改成回頭跟 OKX 要它的 algoId。
    移動SL(amend-algos) 與贏家方向判定(_algo_actual_side) 都要靠這個 ID。"""
    # 【v3.8】skip = 已知改不動的 algoId（黑名單）。不跳過的話，修復程序會把
    # 同一張廢單又撿回來，變成原地打轉。
    bad = set(skip or ())
    for _ in range(tries):
        try:
            r = await api("GET", f"/api/v5/trade/orders-algo-pending?ordType=oco&instId={iid}")
            if r.get("code") == "0":
                for o in (r.get("data") or []):
                    if o.get("posSide") == ps and o.get("algoId") \
                       and o.get("algoId") not in bad:
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


# 【已刪除 hug_started】沒有「啟動一次」這種狀態 —— 每一拍都重新問 sl_decide。


def sl_key_of(side):
    """這一側目前生效的 SL 欄位名。重掛保護單時要用它，不能退回靜態SL ——
    緊貼已經把 SL 推近了，退回去等於把前面收緊的風險全部放掉。"""
    return "front_sl_px" if side == "front" else "back_sl_px"


def _mark_dead_algo(S, side, aid_f, algo_id, why):
    """把一張改不動的 algo 單作廢：清掉 id、列入黑名單，下一輪自動走修復。
    這是 B單「整場緊貼 0 次」的根治點 —— 舊版把 OKX 自動生成那張
    （永遠回 51506）當成正常保護單沿用，於是每 0.5 秒失敗一次、退避、
    再失敗，直到戰役結束。"""
    if not algo_id:
        return
    bl = S.get(f"{side}_bad_algo")
    if not isinstance(bl, list):
        bl = []; S[f"{side}_bad_algo"] = bl
    if algo_id not in bl:
        bl.append(algo_id)
        if len(bl) > 10:
            del bl[:-10]
    S[aid_f] = None
    S[f"{side}_move_fail_n"] = 0          # 換新的重新計數，不要帶著退避
    print(f"[作廢廢單] {S.get('sym')} {side} algoId={algo_id} 原因={why} → 下一輪重掛")


async def _repair_algo(S, iid, ps, sd, side, aid_f, now_t):
    """【v3.8 自我修復】持倉中卻沒有可用的 algoId → 每 3 秒嘗試修一次。

    先回頭跟 OKX 要一張【不在黑名單上】的（換單失敗時，OKX 自動生成那張
    還掛著，但它改不動，不能撿回來用）；真的沒有就依原始 TP/SL 自己掛一張。
    掛成功後才撤掉黑名單上那些廢單 —— 先掛新、後撤舊，絕不製造裸倉空窗。"""
    k = f"_fix_t_{side}"
    if now_t - float(S.get(k, 0) or 0) < 3.0:
        return
    S[k] = now_t
    bad = list(S.get(f"{side}_bad_algo") or [])
    got = await _okx_oco_id(iid, ps, tries=1, skip=bad)
    src = "取回"
    if not got:
        tp = S.get("front_tp_px"     if side == "front" else "back_tp_px")
        sl = S.get(sl_key_of(side))   # 用【目前生效的 SL】重掛，不是退回靜態SL
        sz = S.get("front_sz"        if side == "front" else "back_sz")
        if tp and sl and sz:
            got = await place_algo(iid, ps, sd, sz, tp, sl)
            src = "重掛"
            if got:
                for b in bad:            # 新的掛上了，才撤廢單
                    await cancel_frame(iid, b)
                S[f"{side}_bad_algo"] = []
    if got:
        S[aid_f]   = got
        S["fix_n"] = int(S.get("fix_n", 0) or 0) + 1
        print(f"[自我修復] {S.get('sym')} {side} {src} algoId={got} SL={S.get(sl_key_of(side))}"
              f"（第{S['fix_n']}次）")


async def frame_mover(app):
    """【v3.1 移動SL 引擎】追蹤與送單分離。

    追蹤：WS 逐筆推送時已在 _ws_push_px 更新峰值（零成本、永不漏頂）。
          WS 不可用時，這裡每 MOVE_TICK 秒用 REST 補一次。
    送單：每 PRICE_TICK_SEC 秒檢查一次 —— A/B 各自獨立節流，
          所以 A 一秒兩次、B 一秒兩次，互不排擠。
          amend-algos 限額 20筆/2秒/商品，兩側全開只用掉 8 筆，額度充裕。

    【v3.8】這裡不再有任何緊貼規則 —— 全部交給 sl_decide()。
    本函式只負責：取數字 → 問 sl_decide → 執行（移動 / 記錄跳過原因）。
    TF 逼倉整段刪除，它的職責已被緊貼引擎取代。
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
            # 【v3.10】速率限制冷卻期：本拍不送 amend，下一拍再說。降速不停擺。
            if now_t < AMEND_COOL_UNTIL:
                for S in candidates:
                    _skip(S, "全體", "amend速率冷卻中")
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
                        # 【#27】查詢未回應，本輪不動任何 SL。
                        # 【v3.8】這一條會凍結【兩側】，是最容易被忽略的停擺原因，
                        # 所以一定要計數 —— 次數高就代表被限流，不是「沒行情」。
                        _skip(S, "全體", "持倉查詢未回應")
                        continue

                    # 【v4.2】暫存器：查詢成功才更新。B 一進場就把模式閂扣上，
                    # 不等 loop 那邊偵測 —— 緊貼要在 B 成交的下一拍就全開。
                    if reg_update(S, cur_f, cur_b):
                        print(f"[緊貼模式] {S['sym']} B單在場 → 模式二 全面緊貼")

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
                        _skip(S, "全體", "取不到現價")
                        continue
                    _update_peak(S, Decimal(str(px)))   # 峰值只為事後兵推記錄，決策不看
                    S["_last_px"] = str(px)             # /status 判斷現在是哪一條規則用

                    for side, sd, ps, cur_pos, sl_f, aid_f in (
                            ("front", d,  fps, cur_f, "front_sl_px", "algo_id"),
                            ("back",  bd, bps, cur_b, "back_sl_px",  "back_algo2_id")):
                        # 這一側本來就沒倉 —— 正常，不算「跳過緊貼」，不計數
                        if not cur_pos:
                            continue
                        # ↓↓ 以下每一條都會讓這一側【這一輪不緊貼】，一律留名字＋計數
                        if not S.get("front_filled" if side == "front" else "back_filled"):
                            _skip(S, side, "進場旗標未設"); continue
                        if S.get("closing"):
                            _skip(S, side, "戰役結算中"); continue
                        # 退避：連續失敗後拉長重試間隔，不再每秒打爆速率額度
                        fail_n = int(S.get(f"{side}_move_fail_n", 0) or 0)
                        if fail_n:
                            wait = AMEND_BACKOFF[min(fail_n, len(AMEND_BACKOFF)) - 1]
                            if now_t - float(S.get(f"_last_fail_t_{side}", 0)) < wait:
                                _skip(S, side, f"送單失敗退避中({wait}秒)"); continue
                        if now_t - float(S.get(f"_last_move_t_{side}", 0)) < PRICE_TICK_SEC:
                            _skip(S, side, "節流：0.5秒內剛移動過"); continue

                        # 【v3.8】沒有 algoId 不再是死路 —— 每 3 秒嘗試自我修復一次
                        algo_id = S.get(aid_f)
                        if not algo_id:
                            _skip(S, side, "無algoId（修復中）")
                            await _repair_algo(S, iid, ps, sd, side, aid_f, now_t)
                            continue

                        F      = hug_pct(S, side)
                        ent    = S["front_px" if side == "front" else "back_px"]
                        cur_sl = S.get(sl_f) or S["front_static_sl" if side == "front"
                                                  else "back_static_sl"]
                        tick   = S["spec"]["tick"]

                        # 【v4.2 模式開關】只看一件事：這場 B單觸發過沒有。
                        #   B單自己（side=="back"）恆為模式二 —— 它是觸發進場，
                        #   成交那一刻方向就確定了，沒有按兵不動這回事。
                        #   閂由 reg_update() 扣上，只從一翻到二，戰役結束才歸零。
                        hedged = bool(S.get("hedged")) or (side == "back")

                        # ── 規則全在 sl_decide，本函式不再自己判斷任何事 ──
                        act, price, why, rule = sl_decide(
                            sd, ent, px, cur_sl, F, tick, hedged, FEE_A)

                        if act != "move" or price is None:
                            _skip(S, side, why)
                            continue
                        # 規則4（對沖中往SL減損）標 ⏰，規則2（往TP鎖利）標 ✅
                        # —— 只影響顯示，不影響任何決策
                        mtype = E.MOVE_TIME if rule == 4 else E.MOVE_PROFIT
                        pk = S.get("front_peak" if side == "front" else "back_peak") or ent
                        # cur_sl = 移動前的 SL，出場明細要印「SL舊→SL新」
                        S[f"_pending_{side}"] = (str(price), str(pk), mtype, why,
                                                 str(cur_sl))
                        amends.append((iid, algo_id, price, S, side, sl_f))
                except Exception as e:
                    print("frame_mover 單策略錯誤", S.get("sym"), type(e).__name__, e)

            if not amends:
                continue

            okset, deadset, softset = await amend_frames(
                [(a[0], a[1], a[2]) for a in amends])
            for iid, algo_id, nsl, S, side, sl_f in amends:
                pend = S.pop(f"_pending_{side}", None)
                # 長度檢查：舊版存檔留下的 4 元素 _pending_* 會讓解包炸掉，
                # 而這裡一炸就是整個 frame_mover 停擺。寧可丟掉一筆。
                if not pend or len(pend) != 5:
                    continue
                # 【v3.8】why = 這次移動的理由；【v4.2】sl0_s = 移動前的 SL
                nsl_s, pk_s, mtype, why, sl0_s = pend
                hist_key = f"{side}_move_hist"
                mh = S.get(hist_key)
                if not isinstance(mh, list):
                    mh = []; S[hist_key] = mh
                label = f"{S['sym']} {S['dir'] if side == 'front' else S.get('back_d','?')}"
                if algo_id in okset:
                    # 【v3.10】記下和上一次成功移動相差幾毫秒 —— 這就是「緊貼速度」
                    # 的客觀量測值。理論下限 = MOVE_TICK × 1000。實際跑出來的
                    # 中位數若明顯大於它，就代表有東西在拖慢，看 skip_n 找元兇。
                    prev_t = float(S.get(f"_last_move_t_{side}", 0) or 0)
                    dt_ms = int((now_t - prev_t) * 1000) if prev_t else 0
                    S[sl_f] = nsl_s
                    S[f"{side}_move_n"] = int(S.get(f"{side}_move_n", 0)) + 1
                    S[f"_last_move_t_{side}"] = now_t
                    S[f"{side}_move_fail_n"] = 0
                    print(f"[SL移動] {label} {side} {mtype} {why} 新SL={nsl_s} "
                          f"第{S[f'{side}_move_n']}次 間隔{dt_ms}ms")
                    # 【v4.1.1】存下當下現價：出場明細要用它算「這一次貼了幾 %」。
                    # 只存 SL 價算不出來 —— 必須知道當時 SL 距離現價多遠。
                    mh.append({"t": hhmmss(), "type": mtype, "peak": pk_s,
                               "sl0": sl0_s, "sl": nsl_s, "why": why,
                               "dt": dt_ms, "px": str(px)})
                elif algo_id in softset:
                    # 【v3.13】邊界錯誤：不是這張單壞了，是價格在往返途中走掉了。
                    # 不計入失敗、不退避、不作廢 —— 下一拍用新價重算就好。
                    # 針來的時候價格一秒走 1%，這個本來就會頻繁出現。
                    S[f"{side}_soft_n"] = int(S.get(f"{side}_soft_n", 0) or 0) + 1
                    _skip(S, side, "邊界(價格往返中走掉)")
                    mh.append({"t": hhmmss(), "type": E.MOVE_FAIL, "peak": pk_s,
                               "sl0": sl0_s, "sl": nsl_s,
                               "why": why + "｜邊界重算", "soft": 1})
                else:
                    S[f"{side}_move_fail_n"] = int(S.get(f"{side}_move_fail_n", 0)) + 1
                    S[f"_last_fail_t_{side}"] = now_t
                    fn = S[f"{side}_move_fail_n"]
                    print(f"[SL移動失敗] {label} {side} {why} 欲改SL={nsl_s} "
                          f"累計失敗{fn}次")
                    mh.append({"t": hhmmss(), "type": E.MOVE_FAIL, "peak": pk_s,
                               "sl0": sl0_s, "sl": nsl_s, "why": why})
                    # 【v3.8】永久失敗（51506）或連續失敗太多次 → 作廢這張單，
                    # 下一輪由 _repair_algo 自己重掛一張改得動的。
                    aid_key = "algo_id" if side == "front" else "back_algo2_id"
                    if algo_id in deadset:
                        _mark_dead_algo(S, side, aid_key, algo_id, "sCode=51506 天生不可改")
                    elif fn >= AMEND_DEAD_FAILS:
                        _mark_dead_algo(S, side, aid_key, algo_id, f"連續失敗{fn}次")
                if len(mh) > 200:
                    S[hist_key] = mh[-200:]
            save_state()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("frame_mover error", type(e).__name__, e)
            await asyncio.sleep(5)


ALGO_TYPES = ("trigger", "oco")   # 本腳本用到的兩種 algo 單：觸發進場、OCO止盈止損


# 【v3.13】查詢是否成功，供需要「零掛單」這個結論的地方判斷。
# 為什麼要有：/apitest 實測到 OKX 會回 51054 Request timed out —— 那是一個
# 【查不到答案】的回應，但 data 是空的，和「真的沒有掛單」長得一模一樣。
# 這和持倉查詢的假出場災情是同一種錯：把「不知道」當成「沒有」。
# 有這個旗標，「淨場」才能只在真的查到的時候才成立。
ORDERS_OK = {"ok": True}


async def list_all_orders(iid=None, pos_side=None):
    """查該幣種所有掛單。回傳 (普通單list, algo單list)。
    【統一入口】algo 單一律涵蓋 trigger + oco 兩類 ——
    只查 trigger 會漏掉 OCO，撤不乾淨、數量也不準。全檔查掛單都走這裡。

    任一次查詢失敗（逾時、限流…）就把 ORDERS_OK["ok"] 設 False，
    呼叫端要下「沒有掛單」這種結論前必須檢查它。"""
    ok = True
    q = f"?instId={iid}" if iid else ""
    r1 = await api("GET", f"/api/v5/trade/orders-pending{q}")
    if str(r1.get("code")) != "0":
        ok = False
        print(f"[查單失敗] orders-pending code={r1.get('code')} {r1.get('msg')}"
              f"　← 本次結果不完整，不可當成『沒有掛單』")
    orders = [o for o in (r1.get("data") or [])
              if (not pos_side or o.get("posSide") == pos_side)]
    algos = []
    for ot in ALGO_TYPES:
        sep = "&" if q else "?"
        r2 = await api("GET", f"/api/v5/trade/orders-algo-pending{q}{sep}ordType={ot}")
        if str(r2.get("code")) != "0":
            ok = False
            print(f"[查單失敗] algo/{ot} code={r2.get('code')} {r2.get('msg')}"
                  f"　← 本次結果不完整，不可當成『沒有掛單』")
        algos += [o for o in (r2.get("data") or [])
                  if (not pos_side or o.get("posSide") == pos_side)]
    ORDERS_OK["ok"] = ok
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

      15:08:48 | 0.09866▶️0.09904 (0.091%)

    括號裡是搬完之後 SL 離【當時現價】多遠。設定 0.100%，對齊 tick 之後
    實際落在 0.09~0.11%；偏離太多就是緊貼出問題了 —— 這是唯一能當場驗算的數字。

    【為什麼成功的那些不印圖示】九成以上都是成功，每行一個 ✅ 只是噪音。
    但【不是成功的一定要看得見】：規則4 減損（⏰）和送單失敗（🚫）會保留圖示，
    標題也會補上次數。全部成功時標題就乾淨地只有一行 —— 乾淨代表真的沒事，
    不是因為把壞消息藏起來。
    """
    p, t, f = _move_stat(mhist)
    head = f"SL移動 {mn} 次"
    if t or f:                       # 只有「不是全部成功」時才標次數
        head += f"（{E.MOVE_PROFIT}{p} {E.MOVE_TIME}{t} {E.MOVE_FAIL}{f}）"
    lines = ["━━━━━━━━━━", head]
    if not mhist:
        return "\n".join(lines)

    def _hug_pct(m):
        """這一次 SL 離當時現價多遠（%）。舊格式沒存現價就回空字串。"""
        try:
            sl = Decimal(str(m.get("sl")))
            px = Decimal(str(m.get("px") or 0))
            if px > 0:
                return f" ({abs(px - sl) / px * 100:.3f}%)"
        except Exception:
            pass
        return ""

    for m in mhist[-10:]:
        sl0 = m.get("sl0")
        sl  = m.get("sl", "")
        arrow = f"{sl0}▶️{sl}" if sl0 else f"{sl}"
        ty = m.get("type", "")
        mark = "" if ty == E.MOVE_PROFIT else f"{ty} "
        lines.append(f"{m.get('t','')} | {mark}{arrow}{_hug_pct(m)}")
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
        # 【v4.2.1】張數 —— 算「名目本金」用，它才是損益%的正確分母。
        "sz": S.get(f"{pre}_sz"),
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
        # 【v3.8】沒有緊貼的原因（哪一條規則擋下、幾次）與自我修復次數。
        # 這是「為什麼贏／為什麼輸」的關鍵證據，戰役一結束就會被重置，
        # 所以必須在這裡連同其他欄位一起快照下來。
        "skip_n": dict(S.get("skip_n") or {}),
        "fix_n":  int(S.get("fix_n", 0) or 0),
        "amp": S.get("amp", ""),
        "sl_set": S.get("sl"), "tp_set": S.get("tp"),
        "gap_set": S.get("gap"), "offset_set": S.get("offset"),
        "hug_note_full": S.get("hug_note", ""),
    }


def _exit_reason(snap, close_px):
    """判定出場原因：TP / 靜態SL / 緊貼SL（規則2）/ 減損SL（規則4）/ 手動。

    【v4.1 修正滑價誤判】SL 觸發後送的是【市價單】，成交價一定比 SL 差一點。
    舊版用對稱的 3 檔容差，滑超過 3 檔就被歸成「手動/其他」——
    實測 2026-09-21 14:45：最後SL 0.9556、實際成交 0.9550（滑 6 檔），
    明明是緊貼SL 打的，卻記成「手動/其他」，兵推數據直接被污染。

    改成【不對稱】比對：滑價只會往不利的那一側，所以那一側放寬、
    有利那一側維持嚴格。這樣既認得出滑價，也不會把別的出場誤認成 SL。
    """
    try:
        cp = Decimal(str(close_px))
        tick = snap["tick"]
        tol  = tick * 3                       # 有利側：嚴格
        slip = max(tick * 30, cp * Decimal("0.003"))   # 不利側：容許滑價
        tp  = Decimal(str(snap.get("tp_px") or 0))
        ssl = Decimal(str(snap.get("static_sl") or 0))
        lsl = Decimal(str(snap.get("last_sl") or ssl))
        L = (snap.get("dir") == "L")
    except Exception:
        return "手動/其他"

    def _hit(level):
        """成交價是不是這個 SL 打的（含市價滑價）。
        做多：SL 觸發後往下滑 → 成交價落在 [level-slip, level+tol]
        做空：往上滑 → [level-tol, level+slip]"""
        if not level:
            return False
        return (level - slip <= cp <= level + tol) if L else \
               (level - tol <= cp <= level + slip)

    if tp and abs(cp - tp) <= max(tol, slip):
        return "TP"
    if _hit(ssl) and abs(lsl - ssl) <= tol:
        return "靜態SL"
    if _hit(lsl):
        last_ok = None
        for m in reversed(snap.get("move_hist") or []):
            if m.get("type") != E.MOVE_FAIL:
                last_ok = m; break
        if last_ok and last_ok.get("type") == E.MOVE_TIME:
            return "減損SL(規則4)"
        return "緊貼SL(規則2)"
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
        return "峰值 -"
    try:
        e = Decimal(str(ent)); p = Decimal(str(pk))
        gain = (p - e) / e * 100 if snap["dir"] == "L" else (e - p) / e * 100
        return f"峰值 {p}（{gain:+.3f}%）"
    except Exception:
        return f"峰值 {pk}"


async def _build_exit_msg(snap, rec):
    """組出場通知全文（從快照，不碰 S）。回傳 (訊息, 毛, 費, 淨, 原因)。"""
    d = snap["dir"]
    # 【v4.2.1】損益%的分母改用【名目本金】，不是保證金。理由見 _notional()。
    _nv, _nv_ok = _notional(snap)
    mv = float(_nv)
    # 【#30】不再查 OKX 即時掛單。記帳是背景執行的，那時新戰役的掛單早就掛上去了，
    # live 查詢會抓到【下一場】的 TP/SL（實測第63輪顯示 0.09086/0.08701，
    # 那是新 A 埋伏價 0.08736 算出來的，不是這一單的）。
    # 【v4.2】出場畫面已不印靜態TP/SL（進場通知印過了），但 _exit_reason 還是
    # 用快照裡的 tp_px/static_sl 判定，一樣不碰即時查詢。
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

    hold_s = int(time.time() - float(ee)) if ee else 0
    msg = (
        f"{ico} {snap['name']}出場成交 {snap['sym']} {E.dir_word(d)} "
        f"{snap.get('lev')}x {snap.get('margin')}\n"
        f"出場原因：{reason}\n"
        f"━━━━━━━━━━\n"
        f"進場 {snap.get('entry_px','-')}　{entry_t}\n"
        f"出場 {xpx}　{hhmmss()}　持倉 {hold_s} 秒\n"
        f"{_peak_line(snap)}\n"
        f"━━━━━━━━━━\n"
        f"毛利(率)　：{g_r:+.6f}（{g_pct:+.3f}%）\n"
        f"手續費(率)：{fee_r:.6f}（{fee_pct:+.3f}%）\n"
        f"淨利(率)　：{net_r:+.6f}（{net_pct:+.3f}%）{ico}\n"
        + ("" if _nv_ok else "（%以保證金估算：查不到張數）\n")
        + f"{_sl_block(snap['move_n'], snap['move_hist'])}"
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


def _dt_stat(mh, what):
    """從移動歷史抽出「兩次緊貼間隔」的統計（毫秒）。第一次沒有前值，略過。"""
    v = sorted(int(m.get("dt") or 0) for m in (mh or [])
               if m.get("dt") and int(m.get("dt")) > 0)
    if not v:
        return 0
    if what == "min": return v[0]
    if what == "max": return v[-1]
    return v[len(v) // 2]


def _notional(snap):
    """這一單的【名目本金】= 進場價 × 張數 × 每張合約價值。

    【為什麼不能用保證金當分母】保證金是「我打算投入多少」，名目本金是
    「實際買到多少」，兩者因為張數要無條件捨去而不相等。
    實測 2026-09-22 DOGE：保證金 1.2U，1.2÷0.09906÷10 = 1.21 張 → 捨去成
    1 張 = 10 DOGE = 0.9906U。用 1.2 當分母，手續費算出來是 0.058%，
    但真實費率是 0.070% —— 正好是引擎拿來比較的那個數字，對不起來就等於
    畫面在騙人。
    算不出來（缺張數或合約價值）才退回保證金，並回傳 False 讓呼叫端知道。
    回傳 (分母, 是否為名目本金)。"""
    try:
        sz = Decimal(str(snap.get("sz") or 0))
        cv = Decimal(str((snap.get("spec") or {}).get("ctval") or 0))
        px = Decimal(str(snap.get("entry_px") or 0))
        nv = sz * cv * px
        if nv > 0:
            return nv, True
    except Exception:
        pass
    try:
        return Decimal(str(snap.get("margin") or 1)), False
    except Exception:
        return Decimal("1"), False


def _slip_pct(snap, close_px):
    """市價出場滑了多少（%，相對出場價）。正值 = 比 SL 差。

    只有被 SL 打出去的那些單才有意義（TP 或手動平倉回 None）。
    做多：SL 觸發後市價往【下】滑 → slip = (SL - 成交價)/成交價
    做空：往【上】滑            → slip = (成交價 - SL)/成交價
    """
    try:
        if not close_px:
            return None
        cp = Decimal(str(close_px))
        sl = Decimal(str(snap.get("last_sl") or snap.get("static_sl") or 0))
        if cp <= 0 or sl <= 0:
            return None
        v = (sl - cp) if snap.get("dir") == "L" else (cp - sl)
        return round(float(v / cp * 100), 4)
    except Exception:
        return None


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
    # 【v4.2.1】nv 改為【名目本金】（實際買到多少），不再是保證金（打算投入多少）。
    # v4.2.1 之前的紀錄 nv 是保證金，兩者相差張數捨去的那一截，%不可直接互比。
    _nv_d, _nv_ok = _notional(snap)
    nv  = float(_nv_d) or 1.0
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
        # nvsrc 記下分母是什麼 —— 混著舊紀錄分析時才知道哪些可以比
        "nvsrc": "名目" if _nv_ok else "保證金",
        "margin": float(Decimal(str(snap.get("margin") or 0)) or 0),
        "sz": str(snap.get("sz") or ""),
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
        # 【v4.2】市價出場的滑價：SL 掛在哪、實際成交在哪，差了幾 %。
        # 這是手續費之外的第三筆成本，而且量級相當（實測 SUI 滑過 6 檔 ≈ 0.062%）。
        # 正值 = 比 SL 差（往不利方向滑），這是常態；負值 = 比 SL 好。
        # 決定緊貼距離要用真實中位數，不能用猜的 —— 這一欄就是它的來源。
        # 只有 SL 打出去的單才記；TP 與手動平倉記 None，免得污染中位數。
        "slip": (_slip_pct(snap, xpx) if "SL" in (reason or "") else None),
        # SL 移動全紀錄
        "move_n": int(snap.get("move_n", 0)),
        "move_ok": n_ok, "move_tf": n_tf, "move_fail": n_fa,
        # 【v3.10】緊貼速度的客觀量測：兩次成功移動之間隔了幾毫秒。
        # 理論下限 = MOVE_TICK×1000。中位數明顯偏大 → 有東西在拖，看 skips。
        "dt_min": _dt_stat(mh, "min"), "dt_med": _dt_stat(mh, "med"),
        "dt_max": _dt_stat(mh, "max"),
        "moves": mh,
        # 【v3.8】沒有緊貼的原因 —— 贏要知道怎麼贏，輸要知道怎麼輸。
        # skips 記下本場每一個「這一輪不緊貼」的理由與次數（哪一條規則擋的）；
        # moves 裡每一筆的 why 則記下是規則2 還是規則4 讓它動的。
        # 兩邊合起來，任何一場戰役的每 0.5 秒都能還原「當時為什麼這樣做」。
        "skips": dict(snap.get("skip_n") or {}),
        "fix_n": int(snap.get("fix_n", 0) or 0),
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
        # 【v4.2】戰役結束說明只接在【最後一則】出場通知下面。
        # 兩單同一輪一起出場時 results 有兩則，每則都加就變成講兩次。
        # tail 為空字串 = 戰役還沒結束（對手還在場上獨走），什麼都不加。
        tail = tail_fn() or ""
        for i, m in enumerate(results):
            last = (i == len(results) - 1)
            await notify(app, chat, f"{m}\n{tail}" if (last and tail) else m)
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
    if not ORDERS_OK["ok"]:
        # 【v3.13】查單失敗（51054 逾時等）→ 一律回「未清空」。
        # 空的 data 和「真的沒掛單」長得一樣，拿它去宣告淨場會帶著
        # 沒撤掉的單重新部署。和持倉查詢同一個鐵則：不知道 ≠ 沒有。
        print(f"[淨場判定中止] {iid} 查單未回應，本輪不宣告清空")
        return False, -1, -1
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
        ok, dead, _soft = await amend_frames([(iid, aid, n_sl, n_tp)])
        if aid in dead:
            # A 的 OCO 理論上改得動（limit 母單），萬一真的改不動就當場作廢，
            # 讓 frame_mover 下一輪重掛一張 —— 絕不沿用一張改不動的保護單。
            _mark_dead_algo(S, "front", "algo_id", aid, "A單OCO 天生不可改")
            return None
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
    # 【v3.8 關鍵修正】換不成【絕不沿用舊的】。
    # 舊版寫 S["back_algo2_id"] = old_id「至少還有保護」—— 保護是還在（OKX 上
    # 那張 OCO 的確守著靜態SL），但它【永遠改不動】（51506，/apitest 兩個方向
    # 都實測過）。程式拿著它當可用的 algoId，於是每 0.5 秒 amend 一次、失敗、
    # 退避、再失敗，直到戰役結束 —— B 單整場緊貼 0 次。
    # 這就是 WIF 第12輪：B 峰值到過 +0.648%，一次都沒貼，吃滿靜態SL。
    # 正確做法：id 留空並把那張列入黑名單，frame_mover 下一輪會自己重掛一張
    # 改得動的（先掛新、後撤舊），保護從頭到尾沒有空窗。
    S["back_algo2_id"] = None
    if old_id:
        bl = S.get("back_bad_algo")
        if not isinstance(bl, list):
            bl = []; S["back_bad_algo"] = bl
        if old_id not in bl:
            bl.append(old_id)
    print(f"[警告] {S['sym']} B單 OCO 換單失敗 → 自動那張已作廢並列入黑名單，"
          f"下一輪自我修復會重掛一張改得動的（OKX 上的保護不中斷）")
    return False


def _trigger_line(S, side):
    """進場通知用：這一單的 SL【第一次】會被貼到哪裡。

    【v4.2】A/B 不一樣，因為兩單一進場就處在不同模式：
      A單 = 模式一（B還沒觸發）→ 要先往TP走超過手續費率 FEE_A 才准貼，
            所以第一次的落點是 進場×(1±手續費率) 再退 緊貼距離。
      B單 = 模式二（它自己就是對沖成形）→ 一往TP走就貼，
            所以第一次的落點是 進場 ∓ 緊貼距離。
    印出來的數字要跟引擎真的會做的事一致 —— 印一個不會發生的價格，
    等於沒有印。"""
    pre = "front" if side == "front" else "back"
    d = S["dir"] if side == "front" else S.get("back_d", "S" if S["dir"] == "L" else "L")
    try:
        e = Decimal(str(S[f"{pre}_px"]))
        F = hug_pct(S, pre)
        tick = S["spec"]["tick"]
        # B單 恆模式二，不看手續費率；A單 要先走超過 FEE_A
        g = Decimal("0") if side == "back" else FEE_A
        arm = e * (1 + g) if d == "L" else e * (1 - g)
        sl0 = align(arm * (1 - F), tick, "S") if d == "L" else \
              align(arm * (1 + F), tick, "L")
        return str(sl0)
    except Exception:
        return "-"


async def _pending_side_note(S, iid, side):
    """對手單的狀態。回傳 (訊息, 戰役是否續行)。

    【v4.1 關鍵修正】戰役要不要結束，只看一件事：【對手有沒有持倉】。

    舊版對「持倉中」和「只有掛單、沒進場」回傳同一種東西，呼叫端一律
    當成續行 —— 但你的規則是「任一單出場，另一單若沒進場就結束戰役、
    撤掉掛單、重新部署」。結果 A 出場後戰役卡在「B單等待觸發」，
    要等到下一個 TF 邊界才被救回來，最久卡 5 分鐘。
    更糟的是它取決於「那一瞬間查不查得到 B 的掛單」—— 查到就卡住、
    剛好查不到就正常結束，於是同一種情況有時對有時錯。

    現在只認持倉：
      對手持倉中（或持倉查詢失敗）→ 續行（生還者獨走，這是戰術核心）
      對手沒持倉（不管有沒有掛單）→ 結束，撤單重新部署
    """
    pre = "front" if side == "front" else "back"
    nm  = "A單" if side == "front" else "B單"
    d = S["dir"] if side == "front" else S.get("back_d", "S" if S["dir"] == "L" else "L")
    ps = "long" if d == "L" else "short"
    # 【#28】查不到答案時保守視為「對手還在」—— 寧可晚一輪結束戰役，
    # 也不能因為一次查詢異常就宣告結束、撤光掛單。
    ok_p, pos_p = await okx_pos_ex(iid, ps)
    if not ok_p:
        # 【#28】持倉查不到答案 → 保守視為對手還在，寧可晚一輪結束
        return (f"戰場狀態：{nm} 持倉查詢未回應，保守視為仍在場", True)
    if pos_p:
        pk = S.get(f"{pre}_peak") or S.get(f"{pre}_px") or "-"
        sl = S.get(f"{pre}_sl_px") or S.get(f"{pre}_static_sl") or "-"
        return (f"戰場狀態：{nm} 持倉中，繼續獨走\n峰值 {pk}｜SL {sl}", True)
    # 對手沒持倉 → 戰役結束。掛單有沒有都一樣，等一下會一併撤掉。
    return (f"戰場狀態：{nm} 未進場 → 撤單，戰役結束", False)


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
            S["_need_replace"] = True      # 【v4.1】失敗了要快速重試，不等 TF
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
            # 【v4.2】暫存器：查詢成功才更新（上面已擋掉查詢失敗）
            if reg_update(S, cur_a, cur_b):
                print(f"[緊貼模式] {S['sym']} B單在場 → 模式二 全面緊貼")

            if loop_tick % 10 == 0:
                print(f"[loop] {S['sym']} {VERSION} "
                      f"A={'倉' if cur_a else '空'}/{'開' if a_open else '待'} "
                      f"B={'倉' if cur_b else '空'}/{'開' if b_open else '待'} "
                      f"暫存器={S.get('reg_now',0)}(max{S.get('reg_max',0)}) "
                      f"模式{'二' if S.get('hedged') else '一'} "
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
                    f"{E.ENTRY} A單進場成交 {S['sym']} {E.dir_word(d)} "
                    f"{S.get('lev')}x {S.get('margin')}\n"
                    f"━━━━━━━━━━\n"
                    f"進場 {fpx}　{hhmmss()}\n"
                    f"初始TP {S['front_tp_px']}（{'+' if d=='L' else '-'}"
                    f"{float(S['tp']):.1f}%）\n"
                    f"初始SL {S['front_static_sl']} → {_trigger_line(S,'front')}\n"
                    f"━━━━━━━━━━\n"
                    f"對手 {_bnote}")

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
                    f"{E.ENTRY} B單進場成交 {S['sym']} {E.dir_word(back_d)} "
                    f"{S.get('lev')}x {S.get('margin')}\n"
                    f"━━━━━━━━━━\n"
                    f"進場 {bpx}　{hhmmss()}\n"
                    f"初始TP {S['back_tp_px']}（{'+' if back_d=='L' else '-'}"
                    f"{float(S['tp']):.1f}%）\n"
                    f"初始SL {S['back_static_sl']} → {_trigger_line(S,'back')}\n"
                    f"━━━━━━━━━━\n"
                    f"對手 {_anote}")

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
                note, alive = (await _pending_side_note(S, iid, other)) if other else (None, False)
                if note and alive:
                    # 對手【持倉中】才續行 —— 生還者要獨走完才算一場。
                    # 【v4.2】戰役還沒結束，出場通知就到此為止，不加任何尾巴。
                    # 對手出場時會有它自己的通知，戰役結束說明接在那一則下面。
                    print(f"[戰役續行] {S['sym']} {note.splitlines()[0]}")
                    asyncio.create_task(_exit_report(app, chat, S, snaps, lambda: ""))
                    continue
                if note:
                    print(f"[戰役結束] {S['sym']} {note.splitlines()[0]}")

                # 【#32 硬性檢查】仍有未結清的單就絕不宣告戰役結束
                if S.get("front_filled") or S.get("back_filled"):
                    print(f"[戰役續行] {S['sym']} 仍有未結清的單，不重新部署")
                    asyncio.create_task(_exit_report(app, chat, S, snaps, lambda: ""))
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
                skey_now = skey(S["sym"], S.get("locked_dir", d))
                S["dir"] = S.get("locked_dir", d)
                S["pair_state"] = "waiting"
                redeploy = await _place_pair(S, iid, chat, app, label="新戰役")
                S["_need_replace"] = (not redeploy)   # 【v4.1】失敗→快速重試

                def _settle_tail(S=S, prev=prev, had=had, rnd=rnd, kk=skey_now):
                    """【v4.2】戰役結束只說三行：誰打的、A怎麼走的、B怎麼走的。

                    損益一概不印 —— 上面的出場通知已經有 OKX 回傳的毛/費/淨，
                    這裡再算一次只會變成「我自己算的數字」，那不是數據。
                    重新部署也不印 —— 沒有戰役就不需要說明。"""
                    a_r = S.get("front_exit_reason") or prev["front"][0]
                    b_r = S.get("back_exit_reason")  or prev["back"][0]
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
                    bump(kk, f"form_{form}")
                    return (f"━━━━━━━━━━\n"
                            f"🏁 戰役結束 第{rnd}輪｜{form}\n"
                            f"A單 {a_txt}\n"
                            f"B單 {b_txt}")

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
                    _ok_tf = await _place_pair(S, iid, chat, app, label="TF重錨定")
                    S["_need_replace"] = (not _ok_tf)

                # 【v4.1】埋伏中卻一張掛單都沒有 → 立刻補掛，不等下一個 TF。
                # 舊版 _place_pair 失敗只留一句「稍後重試」，而「稍後」是下一個
                # TF 邊界 —— 最久整整 5 分鐘這個策略完全不在戰場上。
                # WIF 第13輪 /status 顯示「A單 無 B單 無」就是卡在這裡。
                elif S.get("_need_replace") and \
                        time.time() - float(S.get("_replace_t", 0) or 0) >= 15:
                    S["_replace_t"] = time.time()
                    print(f"[補掛] {S['sym']} {d} 零持倉零掛單 → 立刻重新部署")
                    await cancel_all_orders(iid)
                    _ok_rp = await _place_pair(S, iid, chat, app, label="補掛")
                    S["_need_replace"] = (not _ok_rp)
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
         # 【v4.3】沒有 hug 欄位 = v4.2 以前存的戰役，回退預設值繼續接管
         "hug": Decimal(str(d.get("hug", HUG_FIXED))),
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
            f"{pct(S.get('gap',0))} {pct(S['tp'])} {pct(S['sl'])}"
            f"｜緊貼{float(hug_pct(S, 'front')*100):.4f}%")


async def cmd_run(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args
    fmt = (f"{E.BOT} 用法：/run 商品 方向 槓桿 保證金 A單埋伏% 兩單間距% 緊貼度% TP% SL%\n"
           f"例：/run WIFUSDT S 1x 1 1.2 0.25 0.15 6 0.8\n"
           f"兩單間距% = B進場價比A進場價低(做L)/高(做S)多少；0 = 同價完全對沖\n"
           f"緊貼度% = 動態SL 貼住現價的距離，A/B 共用\n"
           f"共9個參數，方向只能 L 或 S\n"
           f"（查價 {PRICE_TICK_SEC} 秒，不需輸入）")
    if len(a) != 9:
        await reply(u, f"{E.BOT} 參數數量錯誤（需9個）\n"
                       f"{E.WARN} v4.3 起第7個參數是【緊貼度%】，順序在間距之後、TP之前\n{fmt}"); return
    try:
        sym = a[0].upper(); dr = a[1].upper(); lev = int(a[2].replace("x", ""))
        margin = Decimal(a[3]); offset = Decimal(a[4].rstrip("%")); gap = Decimal(a[5].rstrip("%"))
        hug_in = Decimal(a[6].rstrip("%"))
        tp = Decimal(a[7].rstrip("%")); sl = Decimal(a[8].rstrip("%"))
    except Exception:
        await reply(u, f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if dr not in ("L", "S"):
        await reply(u, f"{E.BOT} 方向須 L 或 S"); return
    for nm, v in (("A單埋伏", offset), ("兩單間距", gap), ("緊貼度", hug_in),
                  ("TP", tp), ("SL", sl)):
        if v < 0:
            await reply(u, f"{E.BOT} {nm} 不可為負數"); return

    # ── 護欄⓪ 緊貼度本身 ──
    # 緊貼度 = 0 → SL 會貼在現價上，一送出 OKX 立刻觸發當場市價平倉。
    # 緊貼度 >= SL → 動態SL 落在靜態SL 之外，棘輪永遠擋下，緊貼從頭到尾不會動。
    if hug_in <= 0:
        await reply(u, f"{E.BOT} {E.LOSS} 緊貼度必須大於 0\n"
                       f"緊貼度 0 代表 SL 貼在現價上，一送出就當場觸發平倉"); return
    if hug_in >= sl:
        await reply(u, f"{E.BOT} {E.LOSS} 緊貼度過大\n"
                       f"緊貼 {pct(hug_in)}% ≥ SL {pct(sl)}%\n"
                       f"動態SL 會落在靜態SL 之外，棘輪永遠擋下 —— 緊貼等於沒作用。\n"
                       f"請把緊貼度降到 {pct(sl)}% 以下，或把 SL% 加大"); return

    # ── 護欄① 兩單間距 ──
    # 【v3.8 重寫】舊護欄 `間距 ≤ SL% − 手續費` 的理由是「B 的緊貼才會啟動」，
    # 那是舊版啟動條件時代的邏輯。現在 B 一進場就緊貼，該理由作廢。
    #
    # 真正還成立的只有一條：間距是【對沖成形前 A 的虧損上限】。
    #   價格跌不到間距 → B 沒觸發 → A 的浮虧一定小於間距
    #   價格跌到間距   → B 觸發，對沖成形，損益和鎖死 = −間距
    # 所以只要 間距 ≥ SL%，A 的靜態SL 會比 B 的觸發價先被打到 ——
    # A 先死，對沖從頭到尾形不成，整套戰術失效。這是硬性拒絕。
    hug_p = hug_in                   # 緊貼距離（%）← v4.3 由參數決定
    if gap >= sl:
        await reply(u, f"{E.BOT} {E.LOSS} 兩單間距過大\n"
                       f"間距 {pct(gap)}% ≥ SL {pct(sl)}%\n"
                       f"A 的靜態SL 會比 B 的觸發價先被打到，A 先死、對沖形不成。\n"
                       f"請把間距降到 {pct(sl)}% 以下，或把 SL% 加大"); return
    if gap + hug_p >= sl:
        await reply(u, f"{E.BOT} {E.WARN} 提醒：A 的減損空間 = SL {pct(sl)}% − 間距 "
                       f"{pct(gap)}% − 緊貼 {pct(hug_p)}% = "
                       f"{float(sl - gap - hug_p):+.3f}%\n"
                       f"→ 對沖成形後 A 的減損落點已到靜態SL，緊貼算出來的新SL 等於或劣於舊的，"
                       f"棘輪會擋下 —— A 一步都動不了，直接吃滿 SL 加滑價（對 B 不影響）。\n"
                       f"2026-09-22 WIF 就是這個結構，單場虧 1.244%。仍可執行，但請確認這是你要的。")
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
    hug_a = hug_b = hug_in / 100       # v4.3：由參數指定，A/B 共用（地板各算各的）
    hug_note = (f"指定 {pct(hug_in)}%"
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
    # 【v4.3】地板抬高之後 A 的減損空間會跟著縮水，而前面那道護欄比的是你輸入的
    # 原值 —— 用地板後的真實值重算一次，免得你看到的數字比實際樂觀。
    # 只在地板真的生效時才印，細 tick 的幣種不會多這一行。
    if hug_a != hug_in / 100:
        warn += (f"\n{E.WARN} 地板生效後 A 減損空間 = SL {pct(sl)}% − 間距 {pct(gap)}%"
                 f" − 緊貼 {float(hug_a*100):.4f}% = {float(sl - gap - hug_a*100):+.3f}%")

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
        # 【v4.3】存【你輸入的原值】，tick 地板由 hug_pct() 在成交時依實際進場價套用，
        # 不在這裡先套 —— 埋伏價和成交價不一定相同，先套會用錯參考價。
        "hug": hug_in / 100,
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
        f"緊貼距離：A {hug_display(spec, front_amb, hug_a)}｜B {hug_display(spec, back_amb, hug_b)}\n"
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
        stage = _hug_state(S, pre)
        try:
            out.append(f"  緊貼 {hug_display(S['spec'], S.get(f'{pre}_px') or 0, hug_pct(S, pre))}")
        except Exception:
            pass
        mn = int(S.get(f"{pre}_move_n", 0))
        _p, _t, _f = _move_stat(S.get(f"{pre}_move_hist") or [])
        stat = f" {E.MOVE_PROFIT}{_p} {E.MOVE_TIME}{_t} {E.MOVE_FAIL}{_f}" if (_p or _t or _f) else ""
        out.append(f"  SL {S.get(f'{pre}_sl_px','-')} {stage}{stat}")
        if mn:
            for m in (S.get(f"{pre}_move_hist") or [])[-5:]:
                out.append(f"    {m.get('t','')} {m.get('type','')} 止{m.get('sl','')} {m.get('why','')}")
    elif waiting:
        amb = S.get(f"{pre}_px", "-")
        out.append(f"{nm} 埋伏 @{amb}")
        out.append(f"  TP {S.get(f'{pre}_tp_px','-')}｜SL {S.get(f'{pre}_static_sl','-')}")
        try:
            out.append(f"  緊貼 {hug_display(S['spec'], amb, hug_pct(S, pre))}")
        except Exception:
            pass
    else:
        r = S.get(f"{pre}_exit_reason")
        out.append(f"{nm} 已出場（{r}）" if r else f"{nm} 無")
    return out


def _tf_note(S, holding_a, holding_b):
    """下個 TF 到期時會發生什麼。
    【v3.8】TF 不再碰 SL —— 舊版的「TF 逼倉」早已被緊貼引擎取代。
    TF 現在只剩一個職責：零持倉時撤單、重新取價、重新部署。"""
    tf_sec = TF_SEC.get(S.get("tf", ACCOUNT_TF), 300)
    left = int((int(time.time() // tf_sec) + 1) * tf_sec - time.time())
    m, sec = divmod(left, 60)
    t = f"{m}分{sec:02d}秒" if m else f"{sec}秒"
    return (f"下個TF：尚有{t}\n"
            f"（確認零持倉才會撤單重新部署）")


def _hug_state(S, side):
    """這一側的緊貼現況，一行講完：現在哪一條規則在管、移動幾次。

    【v4.2】顯示的判斷邏輯必須和 sl_decide 完全一致，否則畫面會騙人。
    這裡只是把同一組條件用中文講出來，不做任何決策。"""
    if not S.get("front_filled" if side == "front" else "back_filled"):
        return "未進場"
    n   = int(S.get(f"{side}_move_n", 0) or 0)
    fn  = int(S.get(f"{side}_move_fail_n", 0) or 0)
    ee  = S.get("front_ee" if side == "front" else "back_ee")
    el  = int(time.time() - float(ee)) if ee else 0
    try:                                   # /status 永遠不該因為顯示而炸掉
        d   = S["dir"] if side == "front" else S.get("back_d", "S" if S["dir"] == "L" else "L")
        e   = Decimal(str(S["front_px" if side == "front" else "back_px"]))
        cur = Decimal(str(S.get("_last_px") or e))
        hedged = bool(S.get("hedged")) or (side == "back")
        if cur == e:
            st = "規則1 價格不動"
        elif (cur > e) if d == "L" else (cur < e):
            gain = ((cur - e) / e) if d == "L" else ((e - cur) / e)
            if hedged or gain > FEE_A:
                st = "規則2 往TP，持續緊貼"
            else:
                st = (f"規則3 按兵不動，獲利{gain*100:.3f}%"
                      f"／手續費率{float(FEE_A*100):.3f}%")
        elif hedged:
            st = "規則4 對沖中往SL，持續緊貼減損"
        else:
            st = "規則3 按兵不動，往SL等保護單"
    except Exception:
        st = f"進場{el}秒"
    mode = "模式二" if (bool(S.get("hedged")) or side == "back") else "模式一"
    return f"{mode}｜{st}｜移動{n}次" + (f"｜失敗{fn}" if fn else "")


def _skip_note(S, top=4):
    """本場戰役「沒有緊貼」的原因排行 —— 戰術沒執行，理由必須看得見。"""
    c = S.get("skip_n")
    if not isinstance(c, dict) or not c:
        return "跳過：無"
    items = sorted(c.items(), key=lambda kv: -kv[1])[:top]
    return "未緊貼原因：" + "｜".join(f"{k.split('|',1)[0]}·{k.split('|',1)[1]}×{v}"
                                     for k, v in items)


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
        # 【v3.8】未緊貼原因 —— 這行是判斷「戰術有沒有執行」的依據。
        # 緊貼現況已由 _side_block 逐邊印出，這裡只補全場的跳過統計。
        if front_in or back_in:
            L.append(_skip_note(s))
            if int(s.get("fix_n", 0) or 0):
                L.append(f"自我修復：{s['fix_n']} 次")
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
    # 【v4.2 修正】出場原因字串是「緊貼SL(規則2)」「減損SL(規則4)」，
    # 舊版寫死比對 == "緊貼SL" 永遠 0 筆 —— 整段診斷一直是空的卻長得像正常。
    # 這正是「沒有資料不能長得像一切正常」那條原則被自己違反的例子。
    hgs = [r for r in rows if str(r.get("reason") or "").startswith(("緊貼SL", "減損SL"))]
    L.append("")
    L.append(f"【緊貼診斷】動態SL 出場 {len(hgs)} 筆"
             + ("" if hgs else "　⚪ 尚無資料"))
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
    L.append(f"【SL移動】平均 {_avg(mv):.1f} 次／單｜規則4減損 {tfs} 次｜失敗 {fails} 次")
    nomove = len([x for x in mv if x == 0])
    L.append(f"  完全沒移動 {nomove}/{len(rows)} 筆（{nomove/len(rows)*100:.0f}%）"
             f"← 模式一按兵不動也算在內")

    # 【市價滑價】v4.2：手續費之外的第三筆成本，決定緊貼距離要用真實數字。
    sp = sorted(float(r["slip"]) for r in rows if r.get("slip") is not None)
    L.append("")
    if sp:
        med = sp[len(sp) // 2]
        L.append(f"【滑價】SL出場 {len(sp)} 筆　中位 {med:+.3f}%"
                 f"｜最差 {sp[-1]:+.3f}%｜最好 {sp[0]:+.3f}%")
        L.append(f"  ▸ 真實打平點 ≈ 緊貼 {hug:.3f}% + A費 "
                 f"{float(FEE_A*100):.3f}% + 滑價 {max(med,0):.3f}% = "
                 f"{hug + float(FEE_A*100) + max(med, 0):.3f}%")
    else:
        L.append("【滑價】⚪ 尚無資料（v4.2 起才記錄，跑幾場就有）")

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
        # 【v4.0.1】觸發單改成【追價】，不再賭方向。
        # OKX 觸發單一定要「價格真的走到觸發價」才會成交，掛在上方等漲、
        # 下方等跌。所以不管掛哪一側，都在賭接下來幾十秒往哪走 ——
        # v4.0.0 量了 3 秒動能就押一次，兩次都押錯（SUIUSDT 第4組：
        # 押跌，結果那 46 秒漲了 6 檔；換邊押漲，結果又不動了）。
        #
        # 追價的作法：每 8 秒撤掉重掛一次，永遠掛在【當下現價 ±1 檔】，
        # 方向取最近一次的漲跌。價格只要動 1 檔就成交，而且每 8 秒重新
        # 對準一次，不會像固定掛單那樣被行情走開就再也追不上。
        # SUIUSDT 那 46 秒走了 6 檔 —— 用追價早就成交好幾次了。
        async def _place_trg(ref, go_up, note):
            t = align(ref + tick if go_up else ref - tick, tick,
                      "S" if go_up else "L")
            rr = await api("POST", "/api/v5/trade/order-algo", {
                "instId": iid, "tdMode": "isolated", "side": open_side, "posSide": ps,
                "ordType": "trigger", "sz": str(sz), "triggerPx": str(t),
                "orderPx": "-1", "triggerPxType": "last",
                "algoClOrdId": "b" + uuid.uuid4().hex[:14], "attachAlgoOrds": attach})
            say(f"  掛觸發 {open_side} @{t}（現價 {ref}，{note}）　{_brief(rr, ['algoId'])}")
            return rr, (rr.get("data") or [{}])[0].get("algoId")

        say(f"  追價模式：每 8 秒對準現價重掛一次，只需價格動 1 檔")
        r = {"code": "0"}; trg_aid = None; pos = None; waited = 0
        prev = px
        for attempt in range(1, 9):                 # 最多 8 次 ≈ 64 秒
            ref = await get_last(iid)
            up  = (ref >= prev); prev = ref
            if trg_aid:
                await cancel_frame(iid, trg_aid)
            r, trg_aid = await _place_trg(ref, up, f"第{attempt}次 追{'漲' if up else '跌'}")
            if r.get("code") != "0":
                say(f"  {E.LOSS} 下單失敗，本路徑略過")
                return None, created
            t0 = time.time()
            while time.time() - t0 < 8:
                await asyncio.sleep(2)
                _ok, pos = await okx_pos_ex(iid, ps, force=True)
                if pos: break
            waited += int(time.time() - t0)
            if pos:
                break
        if not pos:
            if trg_aid:
                await cancel_frame(iid, trg_aid)
            say(f"  {E.LOSS} 追價 8 次共 {waited} 秒仍沒吃到倉 —— "
                f"該幣種此刻幾乎不動，本路徑略過（不是 API 問題）")
            return None, created

    if path == "limit":
        if r.get("code") != "0":
            say(f"  {E.LOSS} 下單失敗，本路徑略過")
            return None, created
        t0 = time.time(); pos = None
        while time.time() - t0 < 6:
            await asyncio.sleep(2)
            _ok, pos = await okx_pos_ex(iid, ps, force=True)
            if pos: break
        waited = int(time.time() - t0)
        if not pos:
            say(f"  {E.LOSS} 等 {waited} 秒仍沒吃到倉，本路徑略過")
            return None, created
    entry_px = Decimal(str(pos.get("avgPx") or px))
    say(f"  ✅ 成交 avgPx={entry_px} 張數={pos.get('pos')}（等待 {waited} 秒）")

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
    async def _amend_try(label, off=None, pct=None):
        """回傳 True=持倉還在可以繼續；False=倉位已被打掉。
        off = 檔數偏移（邊界測試用）；pct = 百分比偏移（安全側用）。"""
        cur  = await get_last(iid)
        if pct is not None:
            raw = cur * (Decimal("1") - pct) if d == "L" else cur * (Decimal("1") + pct)
        else:
            raw = cur + tick * off
        t_sl = align(raw, tick, "S" if d == "L" else "L")
        rr = await api("POST", "/api/v5/trade/amend-algos",
                       [{"instId": iid, "algoId": oco_id, "newSlTriggerPx": str(t_sl)}])
        say(f"    {label:12} 現價={cur} SL={t_sl}　{_brief(rr)}")
        await asyncio.sleep(1.2)
        _ok, _p = await okx_pos_ex(iid, ps, force=True)
        if not _p:
            say("    ⚠️ 此筆之後持倉消失 → 該 SL 是「立刻觸發成交」而非「被拒絕」")
            return False
        return True

    # ── 第一段：安全側 ──
    # 【v4.0】距離改用【百分比】，不再用檔數。
    # v3.13 實測：SUIUSDT「安全側5檔」= 0.053%，正常行情 1 秒就走完，
    # 倉位當場被掃掉，後面的測試全部作廢（第3組就是這樣掛掉的）。
    # 這一段的目的只是回答「這條路徑的 OCO 能不能 amend」，
    # 距離遠近完全不影響那個答案，所以拉遠到不會被雜訊碰到。
    say("  amend-algos 測試（安全側 0.3% / 0.15%，遠到不會被行情掃到）：")
    alive = True
    if not oco_id:
        say("    （無 OCO 可測）")
    else:
        for pc, label in ((Decimal("0.003"), "安全側0.3%"),
                          (Decimal("0.0015"), "安全側0.15%")):
            alive = await _amend_try(label, pct=pc)
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


# ---------- /selftest 緊貼決策情境表 ----------
# 緊貼規則是一個純函式 sl_decide()，不碰網路、不碰持倉，
# 所以可以在正式機上直接餵情境進去，當場看它每一種怎麼決定。
# 部署完先跑這個：表全過，才代表這台機器上的規則跟談好的一致。
# 不下任何單、不碰任何持倉，隨時可跑。
#
# 進場價 100.00、tick 0.01 —— 緊貼 0.1% 剛好 0.10 U，手續費率 0.07% 剛好 0.07 U，
# 數字可心算核對。做多靜態SL 99.60（-0.4%）；做空靜態SL 100.30（+0.4% of 99.90）
# (編號, 規則, 說明, 方向, 進場, 現價, 現SL, hedged, 預期action, 預期價)
SELFTEST_A = [
 (1, 1,"價格不動",                     "L","100.00","100.00"," 99.60",0,"hold",None),
 (2, 3,"往TP +0.05U（未達手續費率0.07U）","L","100.00","100.05"," 99.60",0,"hold",None),
 (3, 3,"★往TP +0.07U（剛好等於，不貼）", "L","100.00","100.07"," 99.60",0,"hold",None),
 (4, 2,"★往TP +0.08U（超過，開始緊貼）", "L","100.00","100.08"," 99.60",0,"move","99.98"),
 (5, 2,"往TP +1.00U（一路跟上去）",     "L","100.00","101.00"," 99.60",0,"move","100.90"),
 (6, 3,"★往SL -0.20U（按兵不動等B）",   "L","100.00"," 99.80"," 99.60",0,"hold",None),
 (7, 3,"★往SL -0.39U（快碰靜態SL也不動）","L","100.00"," 99.61"," 99.60",0,"hold",None),
 (8, 0,"往TP後回撤，SL已更近",          "L","100.00","101.00","100.95",0,"hold",None),
]
SELFTEST_H = [
 (9, 2,"★模式二 往TP +0.02U（不看手續費率）","L","100.00","100.02"," 99.60",1,"move","99.92"),
 (10,4,"★模式二 往SL -0.20U（減損）",   "L","100.00"," 99.80"," 99.60",1,"move","99.71"),
 (11,0,"模式二 往SL續跌，不准放鬆",      "L","100.00"," 99.75"," 99.71",1,"hold",None),
 (12,1,"模式二 價格不動",               "L","100.00","100.00"," 99.71",1,"hold",None),
]
SELFTEST_B = [
 (13,1,"B：價格不動",                  "S"," 99.90"," 99.90","100.30",1,"hold",None),
 (14,2,"B：往TP -0.05U（立刻貼）",      "S"," 99.90"," 99.85","100.30",1,"move","99.94"),
 (15,2,"B：往TP -0.40U",               "S"," 99.90"," 99.50"," 99.94",1,"move","99.59"),
 (16,4,"★B：往SL +0.05U（減損）",       "S"," 99.90"," 99.95","100.30",1,"move","100.04"),
 (17,0,"B：往SL續漲，不准放鬆",         "S"," 99.90","100.05","100.04",1,"hold",None),
 (18,0,"B：往TP後反彈，SL已更近",       "S"," 99.90"," 99.70"," 99.59",1,"hold",None),
]
RULE_CN = {0:"棘輪擋(規則1)", 1:"規則1不動", 2:"規則2往TP貼",
           3:"規則3按兵不動", 4:"規則4對沖減損"}


def _selftest_rows(cases, out):
    tick = Decimal("0.01"); F = HUG_FIXED
    bad = 0
    for (n, rl, desc, d, ent, cur, sl, hg, exp_a, exp_p) in cases:
        ent = ent.strip(); cur = cur.strip(); sl = sl.strip()
        try:
            a, p, why, rule = sl_decide(d, ent, cur, sl, F, tick, bool(hg), FEE_A)
        except Exception as ex:
            out.append(f"{n:>2} {desc} 💥 {type(ex).__name__}: {ex}"); bad += 1; continue
        okA = (a == exp_a)
        okP = (exp_p is None) or (p is not None and Decimal(str(p)) == Decimal(exp_p))
        okR = (rule == rl)
        good = okA and okP and okR
        if not good:
            bad += 1; mark = f"❌應為{RULE_CN.get(rl,rl)}/{exp_a}{exp_p or ''}"
        else:
            mark = "✅"
        act = f"移SL→{p}" if a == "move" else "不動"
        out.append(f"{n:>2} {desc}")
        out.append(f"   現{cur} SL{sl} {'模式二' if hg else '模式一'} → {act}　{mark}")
        out.append(f"   {why}")
    return bad


def _selftest_lines():
    """跑情境表，回傳給 TG 的行陣列。純計算，不碰網路。"""
    F = HUG_FIXED; tick = Decimal("0.01")
    out = [f"緊貼 {float(F*100):.3f}% = 0.10 U｜"
           f"模式一 往TP 要超過手續費率 {float(FEE_A*100):.3f}% = 0.07 U 才貼",
           "模式一 = B單未觸發（按兵不動）｜模式二 = B單觸發過（全面緊貼）",
           "進場價 100.00｜tick 0.01（0.1% = 0.10 U，可心算）", ""]
    out.append("━━ 模式一　A單（限價埋伏）進場100.00 靜態SL 99.60 ━━")
    bad = _selftest_rows(SELFTEST_A, out)
    out.append("")
    out.append("━━ 模式二　A單（B已觸發過）進場100.00 ━━")
    bad += _selftest_rows(SELFTEST_H, out)
    out.append("")
    out.append("━━ B單（觸發對沖，恆模式二）進場99.90 靜態SL 100.30 ━━")
    bad += _selftest_rows(SELFTEST_B, out)

    # ── 壓縮帶：兩單都在場時，兩條動態SL 從兩側夾住現價 ──
    out.append("")
    out.append("━━ 壓縮帶：兩單都在場，價格被關在多寬的箱子 ━━")
    out.append("現價　│ A(多)SL　B(空)SL │ 箱寬　→ 破哪邊誰出場")
    for p in ("100.00", "99.95", "99.90", "99.80"):
        c = Decimal(p)
        sa = align(c * (Decimal("1") - F), tick, "S")
        sb = align(c * (Decimal("1") + F), tick, "L")
        out.append(f"{c} │ {sa}　{sb} │ {sb-sa}")
    out.append("→ 價格動 0.1% 就有一單被逼出，存活的那單繼續緊貼")

    out.append("")
    tot = len(SELFTEST_A) + len(SELFTEST_H) + len(SELFTEST_B)
    out.append(f"{tot} 種情境｜通過 {tot-bad}｜失敗 {bad}")
    if bad:
        out.append(f"{E.LOSS} 有情境不符預期 —— 這台機器的規則跟談好的不一致，先別交易")
    return out


# ---------- /log 診斷紀錄 ----------
# 【和 /summary 的分工】
#   /summary → 損益。看賺賠、看哪個幣種哪個方向表現好。
#   /log     → 腳本本身。API 回傳值、錯誤碼、速率用量、緊貼引擎為什麼沒動。
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


def _log_engine_lines():
    out = ["━━━ 緊貼引擎現況 ━━━",
           f"心跳 {MOVE_TICK}s（{1/MOVE_TICK:.0f} 拍/秒）｜"
           f"每側節流 {PRICE_TICK_SEC}s｜緊貼預設 {float(HUG_FIXED*100):.3f}%"
           f"（實際值每場由 /run 指定，見下方各策略）"]
    live = [S for S in STRATS.values() if S.get("pair_state", "idle") != "idle"]
    if not live:
        out.append("（目前沒有運行中的策略）")
        return out
    for S in live:
        out.append(f"· {S.get('sym')} {E.dir_word(S.get('dir'))}")
        for side, nm in (("front", "A"), ("back", "B")):
            if not S.get(f"{side}_filled"):
                continue
            mh = S.get(f"{side}_move_hist") or []
            out.append(f"  {nm}單 移動{int(S.get(f'{side}_move_n',0))}次"
                       f"｜失敗{int(S.get(f'{side}_move_fail_n',0) or 0)}"
                       f"｜最快{_dt_stat(mh,'min')}ms 中位{_dt_stat(mh,'med')}ms"
                       f"｜algoId={'有' if S.get('algo_id' if side=='front' else 'back_algo2_id') else '【無】'}")
        out.append("  " + _skip_note(S, top=5))
        if int(S.get("fix_n", 0) or 0):
            out.append(f"  自我修復 {S['fix_n']} 次")
    return out


def _selftest_fails():
    """規則自檢的失敗數（不印表，只要結論）。"""
    bad = 0; tick = Decimal("0.01"); F = HUG_FIXED; tot = 0
    for cases in (SELFTEST_A, SELFTEST_H, SELFTEST_B):
        tot += len(cases)
        for (n, rl, desc, d, ent, cur, sl, hg, exp_a, exp_p) in cases:
            try:
                a, p, why, rule = sl_decide(d, ent.strip(), cur.strip(), sl.strip(),
                                            F, tick, bool(hg), FEE_A)
            except Exception:
                bad += 1; continue
            if a != exp_a or rule != rl:
                bad += 1
            elif exp_p is not None and (p is None or Decimal(str(p)) != Decimal(exp_p)):
                bad += 1
    return bad, tot


def _check_health_lines():
    """一頁健檢：每一項一個顏色，紅的才需要往下查。

    【設計原則】「沒有資料」絕對不能長得像「一切正常」——
    那正是 B 單整場不動卻看不出來的同一種錯。沒資料一律標 ⚪。
    """
    out = []

    # ① 規則：這台機器上的緊貼規則和談好的一致嗎
    bad, tot = _selftest_fails()
    out.append(f"{'🟢' if not bad else '🔴'} 規則　　{tot-bad}/{tot} 通過"
               f"｜緊貼(自檢用){float(HUG_FIXED*100):.3f}%　"
               f"模式一手續費率{float(FEE_A*100):.3f}%")

    # ② 連線
    lat = [v for dq in API_LAT.values() for v in dq]
    med = sorted(lat)[len(lat) // 2] if lat else None
    ws_pub = bool(WS_LIVE.get("pub"))     # 公有斷線 = 取價退回 REST，緊貼直接變慢
    ws_pri = bool(WS_LIVE.get("pri"))
    if med is None:
        out.append(f"⚪ 連線　　尚無 API 呼叫紀錄（剛重啟？）｜WS {ws_status()}")
    else:
        ico = "🔴" if not ws_pub else ("🟡" if (not ws_pri or med >= 500) else "🟢")
        note = "" if ws_pub else "　← 公有WS斷線，取價退回REST，緊貼會變慢"
        out.append(f"{ico} 連線　　WS {ws_status()}｜OKX 延遲中位 {med}ms{note}")

    # ③ 速率：離 OKX 上限還有多遠
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
        out.append(f"{ico} 速率　　尖峰 {worst*100:.0f}%"
                   + ("｜" + "　".join(parts) if parts else "（全部低於一半）"))

    # ④ API 錯誤碼
    if not API_SCODE:
        out.append("🟢 API錯誤　一次都沒有")
    else:
        tally = "　".join(f"{k}×{v}" for k, v in
                          sorted(API_SCODE.items(), key=lambda kv: -kv[1])[:4])
        ico = "🔴" if "50011" in API_SCODE else "🟡"
        out.append(f"{ico} API錯誤　{tally}")

    # ⑤ 緊貼：戰術到底有沒有在執行（最重要的一項）
    live = [S for S in STRATS.values() if S.get("pair_state", "idle") != "idle"]
    held = []
    for S in live:
        for side, nm in (("front", "A"), ("back", "B")):
            if S.get(f"{side}_filled"):
                held.append((S, side, nm))
    if not held:
        out.append(f"⚪ 緊貼　　目前無持倉（{len(live)} 個策略埋伏中）")
    else:
        bad_l = []
        for S, side, nm in held:
            aid = S.get("algo_id" if side == "front" else "back_algo2_id")
            mn  = int(S.get(f"{side}_move_n", 0) or 0)
            ee  = S.get(f"{side}_ee")
            el  = int(time.time() - float(ee)) if ee else 0
            if not aid:
                bad_l.append(f"{S['sym']}{nm}單 algoId【無】")
            elif mn == 0 and el > 30:
                bad_l.append(f"{S['sym']}{nm}單 進場{el}s 移動0次")
        if bad_l:
            out.append("🔴 緊貼　　" + "　".join(bad_l[:3]))
        else:
            tot_mv = sum(int(S.get(f"{sd}_move_n", 0) or 0) for S, sd, _ in held)
            out.append(f"🟢 緊貼　　{len(held)} 單持倉中｜累計移動 {tot_mv} 次")

    out.append(f"{'🟢' if not DIAG_ERR else '🟡'} 異常　　"
               f"{len(DIAG_ERR)} 筆（緩衝共 {len(DIAG)} 行）")
    try:
        rows = [r for r in _load_days(1) if r.get("reason") != "補登"]
        out.append(f"🟢 今日　　{len(live)} 個策略運行｜{len(rows)} 筆出場紀錄")
    except Exception:
        out.append("⚪ 今日　　讀不到交易紀錄")
    return out


class _ShiftArgs:
    """把 /check data XXX 3 後面的參數轉交給既有指令。"""
    def __init__(self, args): self.args = args


async def cmd_check(u, c):
    """/check —— 唯一的診斷入口。不帶參數給一頁健檢，有問題再往下查。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = (c.args or [])
    arg = (a[0].lower() if a else "")
    head = [f"{E.BOT} 🩺 健檢　{VERSION}｜{ACCT}"]
    tail = [f"時間：{hhmmss()}（UTC+8）"]

    if arg == "api":
        await _reply_long(u, head, _log_api_lines(), tail); return
    if arg in ("sl", "hug", "engine", "引擎"):
        await _reply_long(u, head, _log_engine_lines(), tail); return
    if arg in ("rule", "rules"):
        await _reply_long(u, head, _selftest_lines(), tail); return
    if arg in ("log", "all") or arg.isdigit():
        n = int(arg) if arg.isdigit() else 60
        n = max(5, min(n, DIAG_MAX))
        body = [f"━━━ 最近 {n} 行原始輸出 ━━━"] + list(DIAG)[-n:]
        await _reply_long(u, head, body, tail); return
    if arg in ("data", "tune"):
        await cmd_tune(u, _ShiftArgs(a[1:])); return

    body = _check_health_lines()
    if DIAG_ERR:
        body.append("")
        body.append(f"━━━ 最近 {min(len(DIAG_ERR), 8)} 筆異常 ━━━")
        body += list(DIAG_ERR)[-8:]
    body += ["", "━━━ 要看細節 ━━━",
             "/check sl　　緊貼引擎（移動幾次、多快、為何沒動）",
             "/check api　 API用量與錯誤碼",
             "/check log　 原始輸出（/check 100 = 最近100行）",
             "/check rule　規則自檢 18 項",
             "/check data　歷史交易數據（可加幣種、天數）",
             "",
             "實盤探測 OKX 行為 → /apitest（會下真單，需閒置帳戶）"]
    await _reply_long(u, head, body, tail)


# ---------- 舊指令別名（保留相容，選單上已移除，統一走 /check） ----------
# /log /selftest /tune 都是我在不同時間點為了解決不同症狀加的，四個指令
# 職責重疊、互相不知道對方存在 —— 典型的頭痛醫頭。現在全部收進 /check，
# 這三個留著只是怕你手指記憶還在，打了不會出錯。
async def cmd_log(u, c):
    """（舊）等同 /check。"""
    await cmd_check(u, c)


async def cmd_selftest(u, c):
    """（舊）等同 /check rule。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    await _reply_long(u, [f"{E.BOT} 🩺 規則自檢　{VERSION}｜{ACCT}"],
                      _selftest_lines(), [f"時間：{hhmmss()}　（新入口：/check rule）"])


async def cmd_apitest(u, c):
    """/apitest 幣種 [L|S|LS] —— 用最小張數實測 OKX API，把原始回應印出來。
    純診斷：不碰 loop、不碰 frame_mover、不碰任何出場邏輯。"""
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    a = c.args or []
    if not a:
        await reply(u, f"{E.BOT} 用法：/apitest 幣種 [L|S|LS]\n"
                       f"例：/apitest WIFUSDT L　（只測多）\n"
                       f"　　/apitest WIFUSDT S　（只測空）\n"
                       f"　　/apitest WIFUSDT LS　（多空都測，約 8 分鐘）\n"
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
                   f"（觸發單用追價模式，每 8 秒對準現價重掛，單組最多 70 秒）")
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

# ---------- /runtest 模擬埋伏＋900秒逐秒記錄（v4.4） ----------
# 純模擬：只查價格，不下任何單，不碰 STRATS / 掛單 / 持倉，不影響 /run。
# 流程：下指令當下埋伏 → 每 5 分鐘 K 線開盤用新現價重新埋伏 → 碰到埋伏價即進場
#       → 每秒記錄 900 筆 → 產生 Excel（TG 傳檔）→ 自動回到埋伏，循環到 /stopruntest。
# v4.5：折返次數改為「燈號改變就 +1」；取消 Email；查價改 WS 優先、REST 備援。
# v4.6：記錄 900 秒；改模擬【觸發式委託】—— 觸發價位置不變（L 在下、S 在上），
#       碰到即以觸發價進場（無法模擬滑價），盈虧方向與限價相反（L 觸發=做空、S 觸發=做多）。
# v4.7：更正觸發位置 —— L 觸發價放【上方】現價×(1+%)、S 放【下方】現價×(1-%)，
#       L 做多 / S 做空；每 0.5 秒一筆（900 秒共 1800 筆）；TG 檔名縮短；完成訊息改版。
RT_REARM_SEC = 300          # 重新埋伏週期：固定 5 分鐘（不跟 /timeframe 變）
RT_ROWS      = 900          # 進場後記錄秒數
RT_STEP      = 0.5          # 每幾秒記錄一筆（一秒 2 次）
# v4.8：時間一律 hh:mm:ss（不顯示小數秒）；完成訊息加查詢價格時間、TF 與折返次數。
RT_N         = int(RT_ROWS / RT_STEP)   # 總筆數 1800
RT_FILE      = f"/srv/1111bot/data/runtest_{ACCT}.json"
RT_DIR       = "/srv/1111bot/data/runtest"
RT = {}                     # key -> {"sym","dr","off","chat","task","state","n"}

def rt_save():
    try:
        data = [{"sym": v["sym"], "dr": v["dr"], "off": str(v["off"]), "chat": v["chat"]}
                for v in RT.values()]
        tmp = RT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, RT_FILE)
    except Exception as e:
        print("[runtest] save fail", e)

def rt_amb_price(px, dr, off, tick):
    """觸發式：L 觸發價 = 現價×(1+埋伏%) 放上方（往上取到 tick）；
    S 觸發價 = 現價×(1-埋伏%) 放下方（往下取到 tick）。"""
    if dr == "L":
        v = px * (1 + off / 100)
        return (v / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    v = px * (1 - off / 100)
    return (v / tick).to_integral_value(rounding=ROUND_FLOOR) * tick

async def rt_send(app, chat, text):
    for i in range(2):
        try:
            await app.bot.send_message(chat, text); return
        except Exception as e:
            print("[runtest] tg fail", i, e); await asyncio.sleep(2)

async def rt_px(iid):
    """runtest 查價：WS 逐筆報價優先（不吃 REST 額度），沒有就走 REST，REST 失敗重試一次；
    再失敗但 WS 有最後成交價就用它。全部失敗才回 None（該秒記為查價失敗）。"""
    WS_WANT.add(iid)
    for _ in range(2):
        try:
            return await get_px(iid)
        except Exception:
            await asyncio.sleep(0.15)
    v = WS_PX.get(iid)
    return v[0] if v else None

def rt_build_excel(path, meta, rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    F = "Arial"; fb = Font(name=F, bold=True); fn = Font(name=F)
    wb = Workbook(); ws = wb.active; ws.title = "runtest"
    ws["A1"] = f"/runtest {meta['sym']} {meta['dr']} {meta['off_s']}%　{ACCT}"
    ws["A1"].font = Font(name=F, bold=True, size=13)
    ws["A2"] = (f"觸發價 {meta['amb']}（{'上方' if meta['dr']=='L' else '下方'}觸發；埋伏時現價 {meta['base']} × (1{'+' if meta['dr']=='L' else '−'}"
                f"{meta['off_s']}%)）　進場時間 {meta['t_in']}　進場時價 {meta['hit_px']}　"
                f"記錄 {RT_ROWS} 秒（每 {RT_STEP} 秒一筆，共 {len(rows)} 筆）　純模擬、未扣手續費")
    ws["A2"].font = fn
    n = len(rows)
    iM = max(range(n), key=lambda i: (rows[i]["rate"], -i))
    im = min(range(n), key=lambda i: (rows[i]["rate"], i))
    has_g = rows[iM]["rate"] > 0; has_r = rows[im]["rate"] < 0
    g = sum(1 for r in rows if r["rate"] > 0); rd = sum(1 for r in rows if r["rate"] < 0)
    summ = [("最高獲利", f"{float(rows[iM]['rate']):+.4f}%（{rows[iM]['t']}｜{rows[iM]['px']}）" if has_g else "無（全程未獲利）"),
            ("最大虧損", f"{float(rows[im]['rate']):+.4f}%（{rows[im]['t']}｜{rows[im]['px']}）" if has_r else "無（全程未虧損）"),
            (f"第{RT_ROWS}秒毛利率", f"{float(rows[-1]['rate']):+.4f}%"),
            ("最終振幅", f"{float(rows[-1]['amp']):.4f}%"),
            ("總折返次數", f"{rows[-1]['flip']} 次"),
            ("綠燈筆數 / 紅燈筆數 / 平盤筆數", f"{g} / {rd} / {n - g - rd}")]
    if meta.get("miss"):
        summ.append(("查價失敗筆數", f"{meta['miss']} 筆（該筆沿用前一筆價格）"))
    for k, (a, v) in enumerate(summ):
        ws.cell(4 + k, 1, a).font = fb; ws.cell(4 + k, 3, v).font = fn
    hr = 4 + len(summ) + 1
    H = ["幣種", "時間", "進場價", "當時價", "振幅%", "毛利價", "毛利率", "燈號", "折返次數"]
    for j, h in enumerate(H, 1):
        c = ws.cell(hr, j, h)
        c.font = Font(name=F, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="404040")
        c.alignment = Alignment(horizontal="center")
    GF = PatternFill("solid", fgColor="C6EFCE"); RF = PatternFill("solid", fgColor="FFC7CE")
    thin = Side(style="thin", color="BBBBBB")
    dec = max(0, -meta["tick"].as_tuple().exponent)
    pxf = "0" if dec == 0 else "0." + "0" * dec
    fmts = [None, None, pxf, pxf, '0.0000"%"', "+" + pxf + ";-" + pxf + ";" + pxf,
            '+0.0000"%";-0.0000"%";0.0000"%"', None, "0"]
    for i, r in enumerate(rows):
        vals = [meta["sym"], r["t"], float(meta["amb"]), float(r["px"]), float(r["amp"]),
                float(r["gp"]), float(r["rate"]), r["light"], r["flip"]]
        for j, v in enumerate(vals, 1):
            c = ws.cell(hr + 1 + i, j, v); c.font = fn; c.border = Border(bottom=thin)
            if fmts[j - 1]: c.number_format = fmts[j - 1]
            if j in (1, 2, 8, 9): c.alignment = Alignment(horizontal="center")
            if has_g and i == iM: c.fill = GF
            if has_r and i == im: c.fill = RF
    ws.freeze_panes = ws.cell(hr + 1, 1)
    for col, w in zip("ABCDEFGHI", [12, 12, 12, 12, 10, 11, 11, 6, 9]):
        ws.column_dimensions[col].width = w
    wb.save(path)
    return {"iM": iM, "im": im, "has_g": has_g, "has_r": has_r}

async def rt_record(app, T, spec, amb, base, hit_px, base_t):
    """已碰到觸發價：第 1 筆 = 進場那一刻（當時價 = 觸發價，振幅 0%），之後每 0.5 秒一筆。
    hit_px = 觸發那一刻實際查到的價格（進場時價，看得出跳過觸發價多少）。"""
    sym, dr, chat = T["sym"], T["dr"], T["chat"]
    iid, tick = spec["iid"], spec["tick"]
    os.makedirs(RT_DIR, exist_ok=True)
    t0 = time.time()                      # 第 1 筆 = 觸發當下
    t_in = datetime.fromtimestamp(t0, TZ8)
    stamp = t_in.strftime("%Y%m%d_%H%M%S")
    base_name = f"runtest.{ACCT}.{sym}.{dr}.{pct(T['off'])}.{stamp}"
    log_path = f"{RT_DIR}/{base_name}.log"
    T["state"] = "記錄中"; T["n"] = 0
    t_end = (t_in + timedelta(seconds=RT_ROWS - 1)).strftime("%H:%M:%S")
    await rt_send(app, chat,
        f"{E.BOT} runtest 進場｜{sym} {E.dir_word(dr)}\n"
        f"進場價 {amb}（觸發價）｜進場時價 {hit_px}\n"
        f"開始記錄 {RT_ROWS} 秒，預計 {t_end} 完成\n"
        f"時間：{t_in.strftime('%H:%M:%S')}")
    rows = []; hi = lo = amb; prev = amb; last_light = None; flip = 0; miss = 0
    lf = open(log_path, "w")
    lf.write("筆,時間,進場價,當時價,振幅%,毛利價,毛利率%,燈號,折返次數\n")
    try:
        for i in range(RT_N):
            if i == 0:
                px = amb
            else:
                wait = t0 + i * RT_STEP - time.time()
                if wait > 0: await asyncio.sleep(wait)
                px = await rt_px(iid)
                if px is None:
                    px = prev; miss += 1
            if px > hi: hi = px
            if px < lo: lo = px
            amp = (hi - lo) / amb * 100
            prev = px
            gp = (px - amb) if dr == "L" else (amb - px)   # L 上方觸發做多、S 下方觸發做空
            rate = gp / amb * 100
            light = "🟢" if gp > 0 else ("🔴" if gp < 0 else "⚪")
            # 折返次數：燈號和上一秒相同不累計，燈號改變就 +1（第 1 行固定 0）
            if last_light is not None and light != last_light: flip += 1
            last_light = light
            _dt = datetime.fromtimestamp(t0 + i * RT_STEP, TZ8)
            t = _dt.strftime("%H:%M:%S")
            rows.append({"t": t, "px": px, "amp": amp, "gp": gp, "rate": rate,
                         "light": light, "flip": flip})
            lf.write(f"{i+1},{t},{amb},{px},{float(amp):.6f},{gp},{float(rate):.6f},{light},{flip}\n")
            if i % 60 == 0: lf.flush()
            T["n"] = int((i + 1) * RT_STEP)
    finally:
        lf.close()

    xlsx = f"{RT_DIR}/{base_name}.xlsx"
    meta = {"sym": sym, "dr": dr, "off_s": pct(T["off"]), "amb": amb, "base": base, "hit_px": hit_px,
            "t_in": rows[0]["t"], "tick": tick, "miss": miss}
    try:
        info = await asyncio.get_running_loop().run_in_executor(None, rt_build_excel, xlsx, meta, rows)
    except Exception as e:
        await rt_send(app, chat, f"{E.LOSS} runtest {sym} Excel 產生失敗：{type(e).__name__}: {e}\n"
                                 f"逐秒紀錄仍保留在 {log_path}")
        return
    r = rows
    L = [f"{E.BOT} {E.OK} runtest 完成｜{sym} {dr} {pct(T['off'])}",
         f"查詢價格 {base_t} | {base} | {amb}",
         f"進場價格 {meta['t_in']} | {hit_px}"]
    ev = []   # (筆序, 文字) → 依時間先後排列
    if info["has_g"]:
        x = r[info["iM"]]; ev.append((info["iM"], f"最大獲利 {x['t']} | {x['px']} | {float(x['rate']):+.4f}%"))
    if info["has_r"]:
        x = r[info["im"]]; ev.append((info["im"], f"最大虧損 {x['t']} | {x['px']} | {float(x['rate']):+.4f}%"))
    L += [txt for _, txt in sorted(ev)]
    if not info["has_g"]: L.append("最大獲利 無（全程未獲利）")
    if not info["has_r"]: L.append("最大虧損 無（全程未虧損）")
    L.append(f"TF={RT_ROWS // 60}m, 折返次數={r[-1]['flip']}次")
    if miss: L.append(f"{E.WARN} 查價失敗 {miss} 筆（沿用前一筆價格）")
    L.append("已回到埋伏，下一輪繼續")
    try:
        with open(xlsx, "rb") as f:
            await app.bot.send_document(chat, f, filename=f"runtest.{sym}.{dr}.{pct(T['off'])}.{t_in.strftime('%Y%m%d')}.xlsx",
                                        caption="\n".join(L)[:1000])
    except Exception as e:
        print("[runtest] send_document fail", e)
        L.append(f"{E.WARN} TG 傳檔失敗，下載：scp 1111bot:{xlsx} ~/Downloads/")
        await rt_send(app, chat, "\n".join(L))

async def rt_worker(app, key):
    T = RT[key]
    sym, dr, off = T["sym"], T["dr"], T["off"]
    try:
        spec = await get_spec(sym)
        iid, tick = spec["iid"], spec["tick"]
        while key in RT:
            # ── 埋伏 ──
            base = T.pop("base0", None); base_t = hhmmss()
            while base is None:
                base = await rt_px(iid)
                if base is None: await asyncio.sleep(1)
            amb = rt_amb_price(base, dr, off, tick)
            T["state"] = "埋伏中"; T["amb"] = amb
            next_rearm = (int(time.time()) // RT_REARM_SEC + 1) * RT_REARM_SEC
            print(f"[runtest] {sym} {dr} 埋伏 現價{base} → {amb}")
            hit = False
            while key in RT and not hit:
                now_s = time.time()
                if now_s >= next_rearm:
                    break                            # 新的 5 分鐘 K 線 → 重新埋伏
                await asyncio.sleep(RT_STEP - (now_s % RT_STEP))
                px = await rt_px(iid)
                if px is None:
                    continue
                hit = (px >= amb) if dr == "L" else (px <= amb)
            if hit:
                await rt_record(app, T, spec, amb, base, px, base_t)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[runtest] worker fail", key, type(e).__name__, e)
        await rt_send(app, T["chat"], f"{E.LOSS} runtest {sym} {E.dir_word(dr)} 異常停止：{type(e).__name__}: {e}")
        RT.pop(key, None); rt_save()

def rt_start(app, sym, dr, off, chat, base0=None):
    key = skey(sym, dr)
    RT[key] = {"sym": sym, "dr": dr, "off": off, "chat": chat, "state": "啟動中", "n": 0,
               "base0": base0}
    RT[key]["task"] = asyncio.create_task(rt_worker(app, key))
    rt_save()

async def cmd_runtest(u, c):
    global CHAT_ID; CHAT_ID = u.effective_chat.id
    fmt = (f"{E.BOT} 用法：/runtest 商品 方向 埋伏%\n"
           f"例：/runtest BTCUSDT L 0.4%\n"
           f"純模擬不下單；每5分鐘重新埋伏，觸發後每0.5秒記錄一筆共{RT_ROWS}秒，\n"
           f"完成後 Excel 傳到 TG，然後自動繼續埋伏\n"
           f"全部停止：/stopruntest")
    a = c.args or []
    if not a:
        L = [fmt, "━━━━━━━━━━"]
        if RT:
            L.append("進行中：")
            for v in RT.values():
                extra = f"{v.get('n', 0)}/{RT_ROWS}" if v.get("state") == "記錄中" else f"埋伏價 {v.get('amb', '-')}"
                L.append(f"{v['sym']} {E.dir_word(v['dr'])} {pct(v['off'])}%｜{v.get('state')}｜{extra}")
        else:
            L.append("目前沒有進行中的 runtest")
        await reply(u, "\n".join(L)); return
    if len(a) != 3:
        await reply(u, f"{E.BOT} 參數數量錯誤（需3個）\n{fmt}"); return
    sym = a[0].upper(); dr = a[1].upper()
    try:
        off = Decimal(a[2].rstrip("%"))
    except Exception:
        await reply(u, f"{E.BOT} 埋伏% 格式錯誤\n{fmt}"); return
    if dr not in ("L", "S"):
        await reply(u, f"{E.BOT} 方向須 L 或 S\n{fmt}"); return
    if not (Decimal("0") < off <= Decimal("20")):
        await reply(u, f"{E.BOT} 埋伏% 須大於 0 且不超過 20\n{fmt}"); return
    key = skey(sym, dr)
    if key in RT:
        await reply(u, f"{E.WARN} {sym} {E.dir_word(dr)} 已經在跑 runtest（埋伏 {pct(RT[key]['off'])}%）\n"
                       f"要換參數請先 /stopruntest"); return
    try:
        spec = await get_spec(sym); px = await get_last(spec["iid"])
    except Exception:
        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return
    amb = rt_amb_price(px, dr, off, spec["tick"])
    rt_start(c.application, sym, dr, off, u.effective_chat.id, base0=px)
    await reply(u, f"{E.BOT} runtest 啟動｜{ACCT}\n"
                   f"{sym} {E.dir_word(dr)}｜埋伏 {pct(off)}%\n"
                   f"現價 {px} → 觸發價 {amb}\n"
                   f"觸發式委託：{'L 放上方做多' if dr == 'L' else 'S 放下方做空'}，碰到即以觸發價進場\n"
                   f"每 5 分鐘重新埋伏，進場後記錄 {RT_ROWS} 秒\n"
                   f"純模擬，不下單\n"
                   f"時間：{hhmmss()}")

async def cmd_stopruntest(u, c):
    if not RT:
        await reply(u, f"{E.BOT} 目前沒有進行中的 runtest"); return
    L = [f"{E.BOT} runtest 全部停止｜{ACCT}"]
    first = True
    for key in list(RT.keys()):
        v = RT.pop(key)
        st = v.get("state")
        note = f"（記錄中 {v.get('n', 0)}/{RT_ROWS}，作廢）" if st == "記錄中" else f"（{st}）"
        L.append(("已取消：" if first else "　　　　") + f"{v['sym']} {E.dir_word(v['dr'])}{note}")
        first = False
        t = v.get("task")
        if t: t.cancel()
    rt_save()
    L.append(f"時間：{hhmmss()}")
    await reply(u, "\n".join(L))

async def rt_recover(app):
    """重開後恢復 runtest 清單：一律回到埋伏狀態（重開當下正在記錄的那一輪作廢）。"""
    try:
        if not os.path.exists(RT_FILE): return
        data = json.load(open(RT_FILE))
    except Exception as e:
        print("[runtest] recover read fail", e); return
    names = []
    for d in data:
        try:
            rt_start(app, d["sym"], d["dr"], Decimal(d["off"]), d["chat"])
            names.append(f"{d['sym']} {E.dir_word(d['dr'])} {pct(d['off'])}%")
        except Exception as e:
            print("[runtest] recover fail", d, e)
    if names and data:
        await rt_send(app, data[0]["chat"],
            f"{E.BOT} runtest 已自動恢復（bot 重開）\n" + "\n".join(names) +
            f"\n全部回到埋伏；重開當下若有正在記錄的那一輪已作廢\n時間：{hhmmss()}")

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
        "/run 商品 方向 槓桿 保證金 A單埋伏% 兩單間距% 緊貼度% TP% SL%\n"
        f"例：/run WIFUSDT S 1x 1 1.2 0.25 0.15 6 0.8\n週期依 /timeframe（目前 {ACCOUNT_TF}）\n"
        "/confirm 確認啟動\n/stop 商品 方向\n/stopall 停全部+清殘單\n"
        "/status 所有策略現況\n/summary 總表＋分幣種/方向戰報\n"
        "/tune [幣種] [天數] 調參報告（SL/緊貼建議）\n"
        "/apitest 幣種 [L|S|LS]　API 探測（限價+觸發兩條路徑，自動清場）\n"
        "/amp 幣種 年份  整年5m振幅報表 Excel 寄信\n"
        "/runtest 商品 方向 埋伏%　模擬埋伏＋觸發後900秒每0.5秒記錄（不下單，Excel 傳TG）\n"
        "　例：/runtest BTCUSDT L 0.4%　｜不帶參數＝查看進行中\n"
        "/stopruntest 停止全部 runtest\n"
        "/timeframe 查看/設定週期\n/coins 幣種\n"
        "━━━━━━━━━━\n"
        "【戰術】A限價 + B觸發 同時埋伏（反向同量）。\n"
        "兩單都成交時完全對沖，損益鎖死=-間距，與價格無關；\n"
        "價格衝出箱子時一邊被SL掃、一邊獨活順勢起飛。\n"
        "━━━━━━━━━━\n"
        "【緊貼兩個模式】開關只有一個：B單觸發過沒有\n"
        "模式一 B未觸發＝按兵不動\n"
        f"　往SL→不動｜往TP未超過手續費率 {float(FEE_A*100):.3f}%→不動\n"
        f"　往TP超過 {float(FEE_A*100):.3f}%→緊貼到【緊貼度%】\n"
        "模式二 B觸發過＝全面緊貼（閂，不翻回去）\n"
        "　往TP→貼鎖利｜往SL→貼減損，夾出【緊貼度×2】壓縮帶\n"
        f"SL=現價±【緊貼度%】（下限 {MIN_HUG_TICKS} 檔），"
        f"不判斷峰值，只准拉近\n"
        f"比較用的就是 A單來回手續費率，沒有第二個常數\n"
        f"每 {PRICE_TICK_SEC}s 一次，A/B 各自獨立\n"
        "規則全在 sl_decide() 一處，/check rule 可當場驗證\n"
        f"手續費 A {float(FEE_A*100):.3f}%（maker+taker）／"
        f"B {float(FEE_B*100):.3f}%（taker×2）\n"
        "━━━━━━━━━━\n"
        "【TF】只剩一個職責：零持倉→撤單重新部署\n"
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
            BotCommand("check", "健檢 sl｜api｜log｜rule｜data"),
            BotCommand("apitest", "實盤API探測(會下真單)"),
            BotCommand("coins", "幣種"),
            BotCommand("amp", "振幅報表 Excel"),
            BotCommand("runtest", "模擬觸發900秒記錄"),
            BotCommand("stopruntest", "停止全部runtest"),
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
    await rt_recover(app)
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
                    ("check", cmd_check), ("apitest", cmd_apitest),
                    ("selftest", cmd_selftest), ("log", cmd_log),
                    ("amp", cmd_amp),
                    ("runtest", cmd_runtest), ("stopruntest", cmd_stopruntest),
                    ("timeframe", cmd_timeframe), ("coins", cmd_coins)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.run_polling()

if __name__ == "__main__":
    main()

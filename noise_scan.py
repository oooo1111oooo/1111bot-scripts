#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
noise_scan.py v3 —— 1秒K 雜訊掃描（獨立腳本，不碰 run_bot.py）

v3 改了什麼（v2 的結論不可信，原因是方法不是數字）：
  1. 【埋伏條件進場】v2 每 5 秒取一點當進場 —— 那是隨機進場，本來就沒有優勢。
     v3 只在「價格剛從近期低/高點走了 AMBUSH% 」的那一刻進場，模擬 A 單真實成交。
     同時保留隨機進場當對照組，兩個數字並排你才知道埋伏本身有沒有價值。
  2. 【緊貼網格改用檔數】3~85 檔。v2 用固定 % 網格，結果 WIF/ADA/TAO/WLD
     最佳值落在 0.05%，但它們的 3 檔地板就 0.07~0.14%，那個值生產環境設不出來。
  3. 【直接印淨利】毛利 − 手續費。v2 只印毛利，害你得自己減。
  4. 【三個窗口】60/120/300 秒。你的持倉是 14~217 秒，v2 只測 120 秒可能砍掉長單。
  5. 滑價緩衝改 4 檔（WIF 實測值），可用 --slip-ticks 調。
  6. 刪掉 instId 與秒波動P50（16 個幣有 13 個是 0），tick 改文字避免科學符號。
  7.「被停損%」更名「停損觸發%」並加真正的「勝率%」——
     追蹤停損被打到本來就是正常出場，不是虧損。

用法
  python3 noise_scan.py probe
  python3 noise_scan.py run                     預設 3 天，會直接吃既有快取
  python3 noise_scan.py run --days 3 --ambush 0.5
  python3 noise_scan.py run --no-mail --only WIFUSDT,SUIUSDT
"""

import os, sys, time, json, smtplib, datetime
import urllib.request
from email.message import EmailMessage

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill

# ==================== 設定 ====================
BASE      = "/srv/1111bot"
CACHE_DIR = os.path.join(BASE, "data", "noise_cache")
OUT_DIR   = os.path.join(BASE, "data")
OKX       = "https://www.okx.com"

SYMBOLS = ["AAVEUSD", "ADAUSDT", "AVAXUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT",
           "HYPEUSDT", "LINKUSDT", "SOLUSDT", "SUIUSDT", "TAOUSDT", "WIFUSDT",
           "WLDUSDT", "XAUUSDT", "XRPUSDT", "ZECUSDT"]

HUG_TICKS   = [3, 4, 5, 6, 8, 10, 13, 16, 20, 26, 33, 42, 53, 67, 85]
WINDOWS     = [60, 120, 300]      # 秒
AMBUSH_PCT  = 0.5                 # 埋伏率%（可用 --ambush 改）
AMBUSH_LOOK = 600                 # 往回幾秒找低/高點
MIN_GAP     = 120                 # 兩次埋伏成交至少隔幾秒（保持樣本獨立）
MAX_ENTRY   = 20000               # 進場點上限（超過就等距抽樣）
FEE_A       = 0.070               # A單來回手續費%
FEE_B       = 0.100               # B單來回手續費%
SLIP_TICKS  = 4                   # 滑價緩衝（檔）
REQ_PER_SEC = 6.0

# ==================== 小工具 ====================
def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)

def load_env(path):
    d = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return d

_last_req = [0.0]
def http_get(path, tries=4):
    for i in range(tries):
        gap = 1.0 / REQ_PER_SEC - (time.time() - _last_req[0])
        if gap > 0:
            time.sleep(gap)
        _last_req[0] = time.time()
        try:
            req = urllib.request.Request(OKX + path, headers={"User-Agent": "noise-scan/3.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.loads(r.read().decode("utf-8"))
            if str(j.get("code")) == "0":
                return j.get("data") or []
            if str(j.get("code")) == "50011":
                time.sleep(1.5 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(1.0 * (i + 1))
    return None

def candidates(sym):
    s = sym.upper()
    for suf in ("USDT", "USDC", "USD"):
        if s.endswith(suf):
            base = s[: -len(suf)]; break
    else:
        base = s
    out = [f"{base}-USDT-SWAP", f"{base}-USD-SWAP"]
    if base == "XAU":
        out.insert(0, "XAUT-USDT-SWAP")
    return out

def fetch_instruments():
    d = http_get("/api/v5/public/instruments?instType=SWAP")
    return {x["instId"]: x for x in d} if d else {}

def resolve(sym, insts):
    for c in candidates(sym):
        if c in insts:
            return c
    return None

def probe_bar(iid):
    for bar in ("1s", "1m"):
        for limit in (300, 100):
            d = http_get(f"/api/v5/market/candles?instId={iid}&bar={bar}&limit={limit}")
            if d and len(d) > 1:
                return bar, (300 if len(d) > 100 else 100)
    return None, 0

def tick_str(t):
    """避免 Excel 把 1e-05 顯示成科學符號。"""
    s = f"{t:.10f}".rstrip("0")
    return s + "0" if s.endswith(".") else s

# ==================== 下載（與 v2 相同，快取可直接沿用）====================
def fetch_series(iid, bar, page, seconds_back):
    need_ms = seconds_back * 1000
    newest = None; oldest = None
    chunks = []; total = 0; pages = 0
    while True:
        q = f"/api/v5/market/history-candles?instId={iid}&bar={bar}&limit={page}"
        if oldest:
            q += f"&after={oldest}"
        d = http_get(q)
        if not d:
            break
        chunks.append(np.array([[float(r[0]), float(r[1]), float(r[2]),
                                 float(r[3]), float(r[4])] for r in d], dtype=np.float64))
        total += len(d); pages += 1
        if newest is None:
            newest = float(d[0][0])
        oldest = d[-1][0]
        if pages % 100 == 0:
            od = datetime.datetime.fromtimestamp(float(oldest) / 1000).strftime("%m-%d %H:%M")
            log(f"    …{total} 根，最舊 {od}")
        if newest - float(oldest) >= need_ms or total > 1_500_000:
            break
    if not chunks:
        return None
    arr = np.concatenate(chunks); del chunks
    arr = arr[np.argsort(arr[:, 0])]
    _, keep = np.unique(arr[:, 0], return_index=True)
    return arr[np.sort(keep)]

def get_data(sym, iid, bar, page, days):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cf = os.path.join(CACHE_DIR, f"{sym}_{bar}_{days}d.npy")
    if os.path.exists(cf):
        try:
            a = np.load(cf)
            log(f"  {sym} 讀快取 {len(a)} 根（不重新下載）")
            return a
        except Exception:
            pass
    log(f"  {sym} 下載 {bar} 約 {days} 天 …")
    a = fetch_series(iid, bar, page, days * 86400)
    if a is None or len(a) < 5000:
        log(f"  {sym} 資料不足，略過"); return None
    np.save(cf, a)
    log(f"  {sym} 完成 {len(a)} 根")
    return a

# ==================== 統計 ====================
def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")

def adverse_depth(h, l, c, W):
    """逆行深度：在『這一單最後會賺』的前提下，中途往虧損方向最多走多少 %。"""
    n = len(c) - W - 1
    if n <= 100:
        return np.array([])
    fmax = sliding_window_view(h[1:], W).max(axis=1)[:n]
    fmin = sliding_window_view(l[1:], W).min(axis=1)[:n]
    c0 = c[:n]
    up = (fmax - c0) / c0 * 100.0
    dn = (c0 - fmin) / c0 * 100.0
    return np.concatenate([up[dn > up], dn[up > dn]])

def _thin(mask, min_gap):
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return idx
    out = [idx[0]]; last = idx[0]
    for i in idx[1:]:
        if i - last >= min_gap:
            out.append(i); last = i
    return np.array(out, dtype=np.int64)

def ambush_entries(h, l, c, off_pct, look, W, min_gap):
    """模擬 A 單埋伏成交點。
    做空埋伏掛在上方 → 價格從近 look 秒的低點【上漲】off% 時被吃到。
    做多埋伏掛在下方 → 價格從近 look 秒的高點【下跌】off% 時被吃到。"""
    n = len(c) - W - 2
    if n <= look + 50:
        return np.array([], np.int64), np.array([], np.int64)
    off = off_pct / 100.0
    rmin = np.full(len(l), np.inf)
    rmin[look - 1:] = sliding_window_view(l, look).min(axis=1)
    rmax = np.full(len(h), -np.inf)
    rmax[look - 1:] = sliding_window_view(h, look).max(axis=1)
    s_hit = h[:n] >= rmin[:n] * (1 + off)      # 做空埋伏被吃
    l_hit = l[:n] <= rmax[:n] * (1 - off)      # 做多埋伏被吃
    return _thin(s_hit, min_gap), _thin(l_hit, min_gap)

def trail_one(h, l, c, idx, F, d, W):
    """棘輪追蹤停損模擬（落單者）。回 (毛利%, 是否被停損, 持倉秒)。"""
    entry = c[idx].copy()
    ext = entry.copy()
    stop = entry * (1 - F) if d == "L" else entry * (1 + F)
    alive = np.ones(len(idx), bool)
    out = np.full(len(idx), np.nan)
    hold = np.full(len(idx), float(W))
    for k in range(1, W + 1):
        j = idx + k
        hi, lo = h[j], l[j]
        hit = (alive & (lo <= stop)) if d == "L" else (alive & (hi >= stop))
        if hit.any():
            g = (stop - entry) / entry * 100.0
            out[hit] = g[hit] if d == "L" else -g[hit]
            hold[hit] = k
            alive &= ~hit
        if d == "L":
            ext = np.maximum(ext, hi)
            cand = ext * (1 - F)
            stop = np.where(alive & (cand > stop), cand, stop)
        else:
            ext = np.minimum(ext, lo)
            cand = ext * (1 + F)
            stop = np.where(alive & (cand < stop), cand, stop)
    last = c[idx + W]
    g2 = (last - entry) / entry * 100.0
    out[alive] = g2[alive] if d == "L" else -g2[alive]
    return out, ~alive, hold

def scan(h, l, c, idx_s, idx_l, F, W):
    """合併多空兩邊。回 dict。"""
    parts, stops, holds = [], [], []
    for idx, d in ((idx_s, "S"), (idx_l, "L")):
        if len(idx) == 0:
            continue
        o, st, hd = trail_one(h, l, c, idx, F, d, W)
        parts.append(o); stops.append(st); holds.append(hd)
    if not parts:
        return None
    g = np.concatenate(parts); st = np.concatenate(stops); hd = np.concatenate(holds)
    net = g - FEE_A
    return {"n": len(g), "mean": float(g.mean()), "med": float(np.median(g)),
            "net": float(net.mean()), "win": float((net > 0).mean() * 100),
            "stop": float(st.mean() * 100), "hold": float(hd.mean())}

def analyse(sym, iid, tick, arr, ambush, slip_ticks):
    h, l, c, ts = arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 0]
    px = float(np.median(c))
    tickp = tick / px * 100.0
    floor3 = tickp * 3.0
    adv = adverse_depth(h, l, c, 120)
    Wmax = max(WINDOWS)
    a_s, a_l = ambush_entries(h, l, c, ambush, AMBUSH_LOOK, Wmax, MIN_GAP)
    # 隨機進場對照組
    step = max(1, (len(c) - Wmax - 2) // MAX_ENTRY)
    r_all = np.arange(0, len(c) - Wmax - 2, step, dtype=np.int64)
    rows = []
    for W in WINDOWS:
        for nt in HUG_TICKS:
            F = (tick * nt) / px
            if F >= 0.05:              # 超過 5% 沒有意義
                continue
            a = scan(h, l, c, a_s, a_l, F, W)
            r = scan(h, l, c, r_all, r_all, F, W)
            if not a:
                continue
            rows.append({"W": W, "ticks": nt, "F": F * 100, **a,
                         "rand_net": (r["net"] if r else float("nan"))})
    best = max(rows, key=lambda x: x["net"]) if rows else None
    gap = pct(adv, 70)
    slip = tickp * slip_ticks
    hug = best["F"] if best else float("nan")
    minsl = gap + hug + slip if best else float("nan")
    note = []
    if best and best["net"] <= 0:
        note.append("★沒有任何緊貼能賺到手續費")
    if best and best["ticks"] == HUG_TICKS[0]:
        note.append("最佳落在3檔地板，可能更小才好但設不出來")
    if tickp > 0.03:
        note.append("tick粗")
    if len(a_s) + len(a_l) < 200:
        note.append(f"埋伏樣本僅{len(a_s)+len(a_l)}筆")
    return {"sym": sym, "px": px, "tick": tick, "tickp": tickp, "floor3": floor3,
            "bars": len(c), "days": (ts[-1] - ts[0]) / 86400000.0,
            "amb_n": len(a_s) + len(a_l),
            "a60": pct(adv, 60), "a70": pct(adv, 70), "a80": pct(adv, 80),
            "gap": gap, "slip": slip, "minsl": minsl, "best": best, "rows": rows,
            "note": "／".join(note)}

# ==================== Excel ====================
HEAD = PatternFill("solid", fgColor="1F3864")
BADF = PatternFill("solid", fgColor="FCE4E4")
WARNF = PatternFill("solid", fgColor="FFF2CC")

def write_xlsx(res, path, days, bar, ambush, slip_ticks):
    wb = Workbook(); ws = wb.active; ws.title = "雜訊總表"
    cols = [("幣種", 11), ("最新價", 12), ("tick", 12), ("一檔%", 9), ("3檔地板%", 10),
            ("天數", 7), ("根數", 9), (f"埋伏{ambush}%\n成交次數", 11),
            ("逆行P60%", 10), ("逆行P70%", 10), ("逆行P80%", 10),
            ("▶建議間距%", 11), ("▶建議緊貼%", 11), ("緊貼檔數", 9), ("最佳窗口秒", 10),
            ("毛利%", 9), ("▶淨利%\n(扣A費0.07)", 13), ("勝率%", 9),
            ("停損觸發%", 10), ("平均持倉秒", 10), ("樣本數", 9),
            ("隨機進場淨利%", 13), (f"滑價緩衝%\n({slip_ticks}檔)", 11),
            ("▶最低可行SL%", 13), ("備註", 34)]
    ws.append([c[0] for c in cols])
    for i, (t, w) in enumerate(cols, 1):
        cl = ws.cell(row=1, column=i)
        cl.font = Font(bold=True, color="FFFFFF", size=9); cl.fill = HEAD
        cl.alignment = Alignment(horizontal="center", wrap_text=True)
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "B2"
    for r in sorted(res, key=lambda x: -(x["best"]["net"] if x["best"] else -9)):
        b = r["best"]
        ws.append([r["sym"], round(r["px"], 8), tick_str(r["tick"]), round(r["tickp"], 4),
                   round(r["floor3"], 4), round(r["days"], 2), r["bars"], r["amb_n"],
                   round(r["a60"], 4), round(r["a70"], 4), round(r["a80"], 4),
                   round(r["gap"], 3),
                   round(b["F"], 4) if b else None, b["ticks"] if b else None,
                   b["W"] if b else None,
                   round(b["mean"], 4) if b else None, round(b["net"], 4) if b else None,
                   round(b["win"], 1) if b else None, round(b["stop"], 1) if b else None,
                   round(b["hold"], 1) if b else None, b["n"] if b else None,
                   round(b["rand_net"], 4) if b else None,
                   round(r["slip"], 4), round(r["minsl"], 3), r["note"]])
        i = ws.max_row
        for cc in (12, 13, 17, 24):
            ws.cell(row=i, column=cc).font = Font(bold=True)
        if b and b["net"] <= 0:
            for cc in range(1, len(cols) + 1):
                ws.cell(row=i, column=cc).fill = BADF
        elif r["note"]:
            for cc in range(1, len(cols) + 1):
                ws.cell(row=i, column=cc).fill = WARNF

    ws2 = wb.create_sheet("緊貼掃描")
    hd2 = ["幣種", "窗口秒", "檔數", "緊貼%", "毛利%", "淨利%(扣A費)", "勝率%",
           "停損觸發%", "平均持倉秒", "樣本數", "隨機進場淨利%"]
    ws2.append(hd2)
    for i in range(1, len(hd2) + 1):
        cl = ws2.cell(row=1, column=i); cl.font = Font(bold=True, color="FFFFFF"); cl.fill = HEAD
    ws2.freeze_panes = "A2"
    for r in res:
        bf = (r["best"]["W"], r["best"]["ticks"]) if r["best"] else None
        for x in r["rows"]:
            ws2.append([r["sym"], x["W"], x["ticks"], round(x["F"], 4),
                        round(x["mean"], 4), round(x["net"], 4), round(x["win"], 1),
                        round(x["stop"], 1), round(x["hold"], 1), x["n"],
                        round(x["rand_net"], 4)])
            if bf == (x["W"], x["ticks"]):
                for cc in range(1, len(hd2) + 1):
                    ws2.cell(row=ws2.max_row, column=cc).font = Font(bold=True)

    ws3 = wb.create_sheet("怎麼看")
    for ln in [
        f"資料：OKX {bar} K線 約 {days} 天｜埋伏率 {ambush}%｜滑價緩衝 {slip_ticks} 檔",
        "",
        "■ 和上一版最大的不同：進場條件",
        "　上一版每 5 秒取一點當進場 = 隨機進場，本來就沒有優勢，結果必然難看。",
        f"　這一版只在「價格剛從近 {AMBUSH_LOOK} 秒的低/高點走了 {ambush}%」時進場，",
        "　模擬 A 單埋伏被吃到的那一刻。「隨機進場淨利%」留著當對照組：",
        "　兩者差距 = 埋伏這個動作本身值多少。差距接近 0 = 埋伏沒有加值。",
        "",
        "■ 淨利% = 毛利% − A單來回手續費 0.070%。B 單當落單者的話要再扣 0.030%。",
        "　★ 淨利為負 = 這個幣在這個時間尺度上，追蹤停損賺不到手續費。整列會標紅。",
        "",
        "■ 停損觸發% 不是虧損率。追蹤停損被打到本來就是正常出場方式，",
        "　它可以是獲利出場。真正要看的是【勝率%】（扣完手續費還賺的比例）。",
        "",
        "■ 緊貼檔數：3 檔是引擎硬下限（MIN_HUG_TICKS），低於此會被買賣價差掃掉。",
        "　最佳值若落在 3 檔，代表真正的最佳可能更小，但生產環境設不出來。",
        "",
        "■ 逆行深度：從任一點進場、在『最後會賺』的前提下中途往虧損方向最多走多少。",
        "　→ 決定【兩單間距】。建議間距取 P70。",
        "",
        "■ 最低可行SL% = 建議間距 + 建議緊貼 + 滑價緩衝。",
        "　★ SL 必須大於這個值，否則 B 觸發時 A 的緊貼落點等於或超過靜態SL，",
        "　　A 一步都動不了，直接吃滿 SL 加滑價。2026-09-22 WIF 那場就是這樣虧 1.244%。",
        "　這是【下限】不是建議值，實際設定請留餘裕。",
    ]:
        ws3.append([ln])
    ws3.column_dimensions["A"].width = 100
    wb.save(path)

def send_mail(path, name, res, days, bar, ambush):
    env = load_env(os.path.join(BASE, ".env"))
    user = env.get("GMAIL_USER"); pwd = (env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = env.get("REPORT_TO") or env.get("REPORT_EMAIL_TO") or user
    if not user or not pwd:
        log("！ .env 未設定 Gmail，略過寄送"); log(f"  檔案在：{path}"); return
    m = EmailMessage()
    pos = [r for r in res if r["best"] and r["best"]["net"] > 0]
    m["Subject"] = f"OKX 雜訊掃描 v3 {bar} {days}天 埋伏{ambush}%（{len(pos)}/{len(res)} 個幣淨利為正）"
    m["From"] = user; m["To"] = to
    b = [f"{'幣種':<10}{'間距%':>8}{'緊貼%':>8}{'檔':>4}{'窗口':>6}{'淨利%':>9}{'勝率%':>7}{'最低SL%':>9}"]
    for r in sorted(res, key=lambda x: -(x["best"]["net"] if x["best"] else -9)):
        x = r["best"]
        if not x:
            continue
        b.append(f"{r['sym']:<10}{r['gap']:>8.3f}{x['F']:>8.4f}{x['ticks']:>4}"
                 f"{x['W']:>6}{x['net']:>9.4f}{x['win']:>7.1f}{r['minsl']:>9.3f}")
    b += ["", "淨利已扣 A單來回手續費 0.070%。為負代表這個幣賺不到手續費。",
          "欄位說明見附件「怎麼看」分頁。"]
    m.set_content("\n".join(b))
    m.add_attachment(open(path, "rb").read(), maintype="application",
                     subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     filename=name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd); s.send_message(m)
    log(f"已寄送至 {to}")

# ==================== 主流程 ====================
def do_probe():
    insts = fetch_instruments()
    if not insts:
        log("！ 無法取得商品清單"); return 1
    print(f"{'代號':<11}{'instId':<20}{'tick':<13}{'一檔%':<9}{'3檔地板%':<10}{'1sK'}")
    print("-" * 70)
    for sym in SYMBOLS:
        iid = resolve(sym, insts)
        if not iid:
            print(f"{sym:<11}{'✗ 找不到'}"); continue
        tick = float(insts[iid]["tickSz"])
        d = http_get(f"/api/v5/market/candles?instId={iid}&bar=1m&limit=1")
        px = float(d[0][4]) if d else 0.0
        tp = (tick / px * 100) if px else 0.0
        bar, _ = probe_bar(iid)
        print(f"{sym:<11}{iid:<20}{tick_str(tick):<13}{tp:<9.4f}{tp*3:<10.4f}{bar or '✗'}")
    return 0

def do_run(days, nomail, only, ambush, slip_ticks):
    t0 = time.time()
    insts = fetch_instruments()
    if not insts:
        log("！ 無法取得商品清單"); return 1
    syms = [s for s in SYMBOLS if (not only or s.upper() in only)]
    res = []; bar_used = "1s"
    for n, sym in enumerate(syms, 1):
        log(f"[{n}/{len(syms)}] {sym}")
        iid = resolve(sym, insts)
        if not iid:
            log(f"  {sym} 找不到合約，略過"); continue
        bar, page = probe_bar(iid)
        if not bar:
            log(f"  {sym} 取不到K線，略過"); continue
        bar_used = bar
        arr = get_data(sym, iid, bar, page, days)
        if arr is None:
            continue
        try:
            r = analyse(sym, iid, float(insts[iid]["tickSz"]), arr, ambush, slip_ticks)
            res.append(r)
            b = r["best"]
            if b:
                log(f"  → 間距 {r['gap']:.3f}%｜緊貼 {b['F']:.4f}%({b['ticks']}檔)"
                    f"｜窗口 {b['W']}s｜淨利 {b['net']:+.4f}%｜勝率 {b['win']:.0f}%"
                    + (f"　[{r['note']}]" if r["note"] else ""))
        except Exception as e:
            log(f"  {sym} 分析失敗 {type(e).__name__}: {e}")
    if not res:
        log("！ 沒有任何幣種分析成功"); return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    day = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    name = f"OKX.noise.v3.{bar_used}.{days}d.amb{ambush}.{day}.xlsx"
    path = os.path.join(OUT_DIR, name)
    write_xlsx(res, path, days, bar_used, ambush, slip_ticks)
    log(f"已產生 {path}")
    if not nomail:
        try:
            send_mail(path, name, res, days, bar_used, ambush)
        except Exception as e:
            log(f"！ 寄信失敗 {type(e).__name__}: {e}｜檔案仍在 {path}")
    log(f"完成，耗時 {(time.time()-t0)/60:.1f} 分鐘")
    return 0

def main():
    a = sys.argv[1:]
    mode = a[0] if a else "probe"
    days = 3; nomail = False; only = set(); amb = AMBUSH_PCT; slip = SLIP_TICKS
    for i, x in enumerate(a):
        if x == "--days" and i + 1 < len(a):
            days = max(1, min(30, int(a[i + 1])))
        elif x == "--no-mail":
            nomail = True
        elif x == "--only" and i + 1 < len(a):
            only = {s.strip().upper() for s in a[i + 1].split(",") if s.strip()}
        elif x == "--ambush" and i + 1 < len(a):
            amb = max(0.05, min(5.0, float(a[i + 1])))
        elif x == "--slip-ticks" and i + 1 < len(a):
            slip = max(0, min(50, int(a[i + 1])))
        elif x == "--rate" and i + 1 < len(a):
            globals()["REQ_PER_SEC"] = max(1.0, min(9.0, float(a[i + 1])))
    if mode == "probe":
        return do_probe()
    if mode == "run":
        return do_run(days, nomail, only, amb, slip)
    print(__doc__); return 1

if __name__ == "__main__":
    sys.exit(main())

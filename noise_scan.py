#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
noise_scan.py —— 1秒K 雜訊掃描（獨立腳本）

【這支腳本不碰 run_bot.py，不碰任何交易邏輯，不下任何單。】
它只做三件事：抓 1秒K → 算雜訊統計 → 產 Excel 並寄信。

用法
  python3 noise_scan.py probe                 先探測（約 1 分鐘，不下載大量資料）
  python3 noise_scan.py run                   下載並分析（預設 3 天）
  python3 noise_scan.py run --days 7          改天數
  python3 noise_scan.py run --no-mail         只產檔不寄信
  python3 noise_scan.py run --only WIFUSDT,WLDUSDT    只跑指定幣種

寄信沿用 /srv/1111bot/.env 裡既有的 GMAIL_USER / GMAIL_APP_PASSWORD / REPORT_TO。
下載結果會快取在 CACHE_DIR，重跑不會重抓。
"""

import os, sys, time, json, math, smtplib, datetime
import urllib.request, urllib.error
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

WINDOW_SEC = 120          # 往前看幾秒（對應你的持倉時間 14~217 秒）
HUG_GRID   = [0.05, 0.075, 0.10, 0.125, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40,
              0.50, 0.60, 0.70, 0.85, 1.00]      # 緊貼候選 %
ENTRY_STEP = 5            # 每幾秒取一個模擬進場點
REQ_PER_SEC = 8.0         # 送出速率（OKX 限 20次/2秒，留一半餘裕）

# ==================== 小工具 ====================
def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)

def load_env(path):
    d = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return d

_last_req = [0.0]
def http_get(path, tries=4):
    """GET OKX 公開端點，內建速率控制與重試。"""
    for i in range(tries):
        gap = 1.0 / REQ_PER_SEC - (time.time() - _last_req[0])
        if gap > 0:
            time.sleep(gap)
        _last_req[0] = time.time()
        try:
            req = urllib.request.Request(OKX + path, headers={"User-Agent": "noise-scan/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                j = json.loads(r.read().decode("utf-8"))
            if str(j.get("code")) == "0":
                return j.get("data") or []
            if str(j.get("code")) == "50011":      # 速率超限
                time.sleep(1.5 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(1.0 * (i + 1))
    return None

def candidates(sym):
    """一個使用者寫法可能對應哪些 OKX instId。"""
    s = sym.upper()
    for suf in ("USDT", "USDC", "USD"):
        if s.endswith(suf):
            base = s[: -len(suf)]
            break
    else:
        base = s
    out = [f"{base}-USDT-SWAP", f"{base}-USD-SWAP"]
    if base == "XAU":                      # OKX 的黃金是 XAUT
        out.insert(0, "XAUT-USDT-SWAP")
    return out

# ==================== 探測 ====================
def fetch_instruments():
    data = http_get("/api/v5/public/instruments?instType=SWAP")
    if not data:
        return {}
    return {d["instId"]: d for d in data}

def resolve(sym, insts):
    for c in candidates(sym):
        if c in insts:
            return c
    return None

def probe_bar(iid):
    """測 1s 是否可用；回 (bar, page_limit)。"""
    for bar, limit in (("1s", 300), ("1s", 100)):
        d = http_get(f"/api/v5/market/candles?instId={iid}&bar={bar}&limit={limit}")
        if d and len(d) > 1:
            return bar, (300 if len(d) > 100 else 100)
    for bar, limit in (("1m", 300), ("1m", 100)):
        d = http_get(f"/api/v5/market/candles?instId={iid}&bar={bar}&limit={limit}")
        if d and len(d) > 1:
            return bar, (300 if len(d) > 100 else 100)
    return None, 0

def do_probe():
    log("讀取 OKX 商品清單 …")
    insts = fetch_instruments()
    if not insts:
        log("！ 無法取得商品清單，檢查網路或 OKX 是否可達"); return 1
    log(f"共 {len(insts)} 個永續合約\n")

    print(f"{'你寫的':<12}{'OKX instId':<22}{'tick':<14}{'一檔%':<9}{'1sK':<6}{'可回溯'}")
    print("-" * 78)
    bad = []
    for sym in SYMBOLS:
        iid = resolve(sym, insts)
        if not iid:
            print(f"{sym:<12}{'✗ 找不到':<22}{'-':<14}{'-':<9}{'-':<6}-")
            bad.append(sym); continue
        tick = float(insts[iid]["tickSz"])
        d = http_get(f"/api/v5/market/candles?instId={iid}&bar=1m&limit=1")
        px = float(d[0][4]) if d else 0.0
        tp = (tick / px * 100) if px else 0.0
        bar, lim = probe_bar(iid)
        # 回溯深度：往回翻 3 頁看時間跨度
        depth = "-"
        if bar:
            old = None; n = 0
            for _ in range(3):
                q = f"/api/v5/market/history-candles?instId={iid}&bar={bar}&limit={lim}"
                if old:
                    q += f"&after={old}"
                dd = http_get(q)
                if not dd:
                    break
                n += len(dd); old = dd[-1][0]
            if old:
                age = (time.time() * 1000 - float(old)) / 3600000.0
                depth = f"{n} 根 / {age:.1f} 小時"
        flag = "✓" if bar == "1s" else ("1m only" if bar else "✗")
        warn = "  ⚠ tick粗" if tp > 0.05 else ""
        print(f"{sym:<12}{iid:<22}{tick:<14.10g}{tp:<9.4f}{flag:<6}{depth}{warn}")
    print()
    if bad:
        log("以下代號在 OKX 找不到對應永續合約，run 時會自動略過：" + "、".join(bad))
    log("探測完成。沒問題的話執行：python3 noise_scan.py run --days 3")
    return 0

# ==================== 下載 ====================
def fetch_series(iid, bar, page, seconds_back):
    """往回抓到指定秒數，回 (ts, o, h, l, c) 五個 np.array（時間由舊到新）。"""
    need_ms = seconds_back * 1000
    newest = None; oldest = None
    rows = []
    while True:
        q = f"/api/v5/market/history-candles?instId={iid}&bar={bar}&limit={page}"
        if oldest:
            q += f"&after={oldest}"
        d = http_get(q)
        if not d:
            break
        rows.extend(d)
        if newest is None:
            newest = float(d[0][0])
        oldest = d[-1][0]
        if newest - float(oldest) >= need_ms:
            break
        if len(rows) > 2_000_000:
            break
    if not rows:
        return None
    arr = np.array([[float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])]
                    for r in rows], dtype=np.float64)
    arr = arr[np.argsort(arr[:, 0])]
    _, keep = np.unique(arr[:, 0], return_index=True)
    arr = arr[np.sort(keep)]
    return arr

def get_data(sym, iid, bar, page, days):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cf = os.path.join(CACHE_DIR, f"{sym}_{bar}_{days}d.npy")
    if os.path.exists(cf):
        try:
            a = np.load(cf)
            log(f"  {sym} 使用快取 {len(a)} 根")
            return a
        except Exception:
            pass
    log(f"  {sym} 下載 {bar} 約 {days} 天 …")
    a = fetch_series(iid, bar, page, days * 86400)
    if a is None or len(a) < 5000:
        log(f"  {sym} 資料不足（{0 if a is None else len(a)} 根），略過")
        return None
    np.save(cf, a)
    log(f"  {sym} 完成 {len(a)} 根")
    return a

# ==================== 統計 ====================
def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")

def adverse_depth(h, l, c, W):
    """逆行深度：在『這一單最後會賺』的情況下，中途往虧損方向最多走多少 %。
    多空各算一次再合併。回 np.array(%)。"""
    n = len(c) - W - 1
    if n <= 100:
        return np.array([])
    fmax = sliding_window_view(h[1:], W).max(axis=1)[:n]
    fmin = sliding_window_view(l[1:], W).min(axis=1)[:n]
    c0 = c[:n]
    up = (fmax - c0) / c0 * 100.0      # 往上走多少
    dn = (c0 - fmin) / c0 * 100.0      # 往下走多少
    # 做空進場：往上是虧(MAE)、往下是賺(MFE)；只取最後會賺的樣本
    s = up[dn > up]
    # 做多進場：往下是虧、往上是賺
    L = dn[up > dn]
    return np.concatenate([s, L])

def trail_scan(h, l, c, W, F_pct, step):
    """棘輪緊貼模擬（落單者情境）。回 (平均毛利%, 中位毛利%, 被停損比例%, 平均持倉秒)。"""
    F = F_pct / 100.0
    idx = np.arange(0, len(c) - W - 2, step)
    if len(idx) < 200:
        return (float("nan"),) * 4
    res, held, stopped = [], [], []
    for d in ("L", "S"):
        entry = c[idx].copy()
        ext = entry.copy()                      # 有利方向的極值
        stop = entry * (1 - F) if d == "L" else entry * (1 + F)
        alive = np.ones(len(idx), bool)
        out = np.full(len(idx), np.nan)
        hold = np.full(len(idx), float(W))
        for k in range(1, W + 1):
            j = idx + k
            hi, lo = h[j], l[j]
            hit = alive & (lo <= stop) if d == "L" else alive & (hi >= stop)
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
        res.append(out); held.append(hold)
        stopped.append(~alive)
    r = np.concatenate(res); hd = np.concatenate(held); st = np.concatenate(stopped)
    return (float(r.mean()), float(np.median(r)),
            float(st.mean() * 100.0), float(hd.mean()))

def analyse(sym, iid, tick, arr, W, step):
    ts, h, l, c = arr[:, 0], arr[:, 2], arr[:, 3], arr[:, 4]
    px = float(np.median(c))
    tickp = tick / px * 100.0
    rng = (h - l) / c * 100.0
    adv = adverse_depth(h, l, c, W)
    scan = []
    for F in HUG_GRID:
        m, md, sp, hd = trail_scan(h, l, c, W, F, step)
        scan.append({"F": F, "mean": m, "med": md, "stop": sp, "hold": hd})
    ok = [s for s in scan if not math.isnan(s["mean"])]
    best = max(ok, key=lambda s: s["mean"]) if ok else None
    span_d = (ts[-1] - ts[0]) / 86400000.0

    gap = pct(adv, 70)
    hug = best["F"] if best else float("nan")
    slip = 2.0 * tickp                       # 滑價緩衝：保守取 2 檔
    minsl = gap + hug + slip if not math.isnan(hug) else float("nan")
    note = []
    if tickp > 0.05:
        note.append("tick粗")
    if not math.isnan(hug) and tickp * 3 > hug:
        note.append("緊貼會被3檔地板頂高")
    if not math.isnan(best["mean"] if best else float("nan")) and best["mean"] <= 0:
        note.append("最佳緊貼仍為負期望")
    return {
        "sym": sym, "iid": iid, "px": px, "tick": tick, "tickp": tickp,
        "bars": len(c), "days": span_d,
        "r50": pct(rng, 50), "r75": pct(rng, 75), "r90": pct(rng, 90),
        "a60": pct(adv, 60), "a70": pct(adv, 70), "a80": pct(adv, 80),
        "gap": gap, "hug": hug,
        "hug_mean": best["mean"] if best else float("nan"),
        "hug_stop": best["stop"] if best else float("nan"),
        "slip": slip, "minsl": minsl,
        "note": "／".join(note), "scan": scan,
    }

# ==================== Excel ====================
HEAD = PatternFill("solid", fgColor="1F3864")
WARN = PatternFill("solid", fgColor="FFF2CC")
BAD  = PatternFill("solid", fgColor="FCE4E4")

def write_xlsx(rows, path, days, bar):
    wb = Workbook()
    ws = wb.active; ws.title = "雜訊總表"
    cols = [
        ("幣種", 12), ("OKX instId", 20), ("最新價", 12), ("tick", 13),
        ("一檔%", 9), ("天數", 7), ("根數", 10),
        ("秒波動P50%", 11), ("秒波動P75%", 11), ("秒波動P90%", 11),
        ("逆行P60%", 10), ("逆行P70%", 10), ("逆行P80%", 10),
        ("▶建議間距%", 12), ("▶建議緊貼%", 12), ("該緊貼平均毛利%", 15),
        ("被停損%", 10), ("滑價緩衝%", 11), ("▶最低可行SL%", 14), ("備註", 26),
    ]
    ws.append([c[0] for c in cols])
    for i, (t, w) in enumerate(cols, 1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True, color="FFFFFF"); cell.fill = HEAD
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        ws.column_dimensions[chr(64 + i) if i <= 26 else "A"].width = w
    ws.freeze_panes = "A2"

    for r in sorted(rows, key=lambda x: x["tickp"]):
        ws.append([
            r["sym"], r["iid"], round(r["px"], 8), r["tick"], round(r["tickp"], 4),
            round(r["days"], 2), r["bars"],
            round(r["r50"], 4), round(r["r75"], 4), round(r["r90"], 4),
            round(r["a60"], 4), round(r["a70"], 4), round(r["a80"], 4),
            round(r["gap"], 3), round(r["hug"], 3), round(r["hug_mean"], 4),
            round(r["hug_stop"], 1), round(r["slip"], 4), round(r["minsl"], 3),
            r["note"],
        ])
        i = ws.max_row
        ws.cell(row=i, column=19).font = Font(bold=True)
        ws.cell(row=i, column=14).font = Font(bold=True)
        ws.cell(row=i, column=15).font = Font(bold=True)
        if r["note"]:
            fill = BAD if "tick粗" in r["note"] else WARN
            for cc in range(1, len(cols) + 1):
                ws.cell(row=i, column=cc).fill = fill

    ws2 = wb.create_sheet("緊貼掃描")
    ws2.append(["幣種", "緊貼F%", "平均毛利%", "中位毛利%", "被停損%", "平均持倉秒"])
    for i in range(1, 7):
        cell = ws2.cell(row=1, column=i)
        cell.font = Font(bold=True, color="FFFFFF"); cell.fill = HEAD
    ws2.freeze_panes = "A2"
    for r in rows:
        bestF = r["hug"]
        for s in r["scan"]:
            ws2.append([r["sym"], s["F"], round(s["mean"], 4), round(s["med"], 4),
                        round(s["stop"], 1), round(s["hold"], 1)])
            if s["F"] == bestF:
                for cc in range(1, 7):
                    ws2.cell(row=ws2.max_row, column=cc).font = Font(bold=True)

    ws3 = wb.create_sheet("怎麼看")
    for line in [
        f"資料：OKX {bar} K線，約 {days} 天，窗口 {WINDOW_SEC} 秒",
        "",
        "【逆行深度】從任一點進場，在『這一單最後會賺』的前提下，中途往虧損方向最多走多少。",
        "　→ 決定【兩單間距】。設在 P70，代表 70% 的『本來會賺』的場合 B 不會被雜訊觸發進來。",
        "",
        "【緊貼掃描】用棘輪追蹤停損模擬落單者，掃各種緊貼寬度，看哪個平均毛利最高。",
        "　→ 決定【緊貼%】。這是實測期望值，不是猜的。",
        "",
        "【最低可行SL】= 建議間距 + 建議緊貼 + 滑價緩衝。",
        "　★ SL 必須大於這個值，否則 B 觸發時 A 的緊貼落點會等於或超過靜態SL，",
        "　　 A 一步都動不了，直接吃滿 SL 加滑價。2026-09-22 WIF 那場就是這樣虧 1.244% 的。",
        "",
        "【一檔%】tick 佔價格的比例。緊貼有 3 檔地板，所以一檔% × 3 若大於建議緊貼，",
        "　實際緊貼會被頂高，該幣種不適合這個級距的戰術。",
        "",
        "【滑價緩衝】此處保守取 2 檔。真實值要用 /check data 的滑價中位數取代。",
        "",
        "注意：毛利未扣手續費。A單來回 0.070%，B單來回 0.100%。",
    ]:
        ws3.append([line])
    ws3.column_dimensions["A"].width = 100
    wb.save(path)

def send_mail(path, name, rows, days, bar):
    env = load_env(os.path.join(BASE, ".env"))
    user = env.get("GMAIL_USER")
    pwd = (env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = env.get("REPORT_TO") or env.get("REPORT_EMAIL_TO") or user
    if not user or not pwd:
        log("！ .env 未設定 GMAIL_USER / GMAIL_APP_PASSWORD，略過寄送")
        log(f"  檔案在：{path}")
        return False
    m = EmailMessage()
    m["Subject"] = f"OKX 雜訊掃描 {bar} {days}天（{len(rows)} 個幣種）"
    m["From"] = user; m["To"] = to
    body = ["每個幣種的建議參數在「雜訊總表」，欄位說明在「怎麼看」分頁。", "",
            f"{'幣種':<10}{'建議間距%':<11}{'建議緊貼%':<11}{'最低可行SL%'}"]
    for r in sorted(rows, key=lambda x: x["tickp"]):
        body.append(f"{r['sym']:<10}{r['gap']:<11.3f}{r['hug']:<11.3f}{r['minsl']:.3f}")
    m.set_content("\n".join(body))
    m.add_attachment(open(path, "rb").read(), maintype="application",
                     subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     filename=name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd); s.send_message(m)
    log(f"已寄送至 {to}")
    return True

# ==================== 主流程 ====================
def do_run(days, nomail, only):
    t0 = time.time()
    log("讀取 OKX 商品清單 …")
    insts = fetch_instruments()
    if not insts:
        log("！ 無法取得商品清單"); return 1
    syms = [s for s in SYMBOLS if (not only or s.upper() in only)]
    rows = []
    bar_used = "1s"
    for n, sym in enumerate(syms, 1):
        log(f"[{n}/{len(syms)}] {sym}")
        iid = resolve(sym, insts)
        if not iid:
            log(f"  {sym} 找不到對應合約，略過"); continue
        bar, page = probe_bar(iid)
        if not bar:
            log(f"  {sym} 取不到K線，略過"); continue
        bar_used = bar
        arr = get_data(sym, iid, bar, page, days)
        if arr is None:
            continue
        tick = float(insts[iid]["tickSz"])
        W = WINDOW_SEC if bar == "1s" else max(3, WINDOW_SEC // 60)
        step = ENTRY_STEP if bar == "1s" else 1
        try:
            rows.append(analyse(sym, iid, tick, arr, W, step))
            r = rows[-1]
            log(f"  → 間距 {r['gap']:.3f}%　緊貼 {r['hug']:.3f}%　最低SL {r['minsl']:.3f}%"
                + (f"　[{r['note']}]" if r["note"] else ""))
        except Exception as e:
            log(f"  {sym} 分析失敗 {type(e).__name__}: {e}")
    if not rows:
        log("！ 沒有任何幣種分析成功"); return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    day = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    name = f"OKX.noise.{bar_used}.{days}d.{day}.xlsx"
    path = os.path.join(OUT_DIR, name)
    write_xlsx(rows, path, days, bar_used)
    log(f"已產生 {path}")
    if not nomail:
        try:
            send_mail(path, name, rows, days, bar_used)
        except Exception as e:
            log(f"！ 寄信失敗 {type(e).__name__}: {e}")
            log(f"  檔案仍在：{path}")
    log(f"全部完成，耗時 {(time.time()-t0)/60:.1f} 分鐘")
    return 0

def main():
    a = sys.argv[1:]
    mode = a[0] if a else "probe"
    days = 3; nomail = False; only = set()
    for i, x in enumerate(a):
        if x == "--days" and i + 1 < len(a):
            days = max(1, min(30, int(a[i + 1])))
        elif x == "--no-mail":
            nomail = True
        elif x == "--only" and i + 1 < len(a):
            only = {s.strip().upper() for s in a[i + 1].split(",") if s.strip()}
    if mode == "probe":
        return do_probe()
    if mode == "run":
        return do_run(days, nomail, only)
    print(__doc__)
    return 1

if __name__ == "__main__":
    sys.exit(main())

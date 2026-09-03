#!/usr/bin/env python3
"""均K 回溯分析：抓 OKX 歷史 K 線，重算 HA 燈號 / ATR14，並試算不同回吐門檻。
用法：改下面 CONFIG 區塊再執行。時間一律 UTC+8，用 K 線開盤時間。"""
import json, sys, unicodedata
from decimal import Decimal
from datetime import datetime, timezone, timedelta
try:
    import httpx
except ImportError:
    sys.exit("需要 httpx：/srv/1111bot/.venv/bin/python -m pip install httpx")

# ─────────── CONFIG ───────────
SYM      = "BTCUSDT"
TF       = "5m"
FROM     = "2026-09-02 23:10"    # 顯示起點（K線開盤時間，UTC+8）
TO       = "2026-09-03 01:20"    # 顯示終點
ENTRY_AT = "2026-09-02 23:25"    # 進場後第一根的開盤時間
ENTRY_PX = 76950.1               # 實際成交均價
DIR      = "L"                   # L / S
TRIALS   = [-0.1, -0.2, -0.3, -0.5, -0.8, -1.2]   # 想試算的回吐門檻(%)
PNL_ON   = "raw"                 # 損益用哪個價：raw=原始K線收盤（與實際下單一致）/ ha=均K收盤
DEBUG    = True                  # 印出每次請求的結果
# ──────────────────────────────

TZ8 = timezone(timedelta(hours=8))
BAR = {"5m":"5m","10m":"5m","15m":"15m","30m":"30m","60m":"1H",
       "120m":"2H","240m":"4H","480m":"4H","720m":"12H","1440m":"1D"}[TF]
SEC = {"5m":300,"10m":600,"15m":900,"30m":1800,"60m":3600,
       "120m":7200,"240m":14400,"480m":28800,"720m":43200,"1440m":86400}[TF]
IID = SYM.replace("USDT","") + "-USDT-SWAP"
UA  = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

def dw(t):
    """顯示寬度：中文/全形/emoji 算 2 格，其餘 1 格。"""
    n = 0
    for ch in str(t):
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n

def R(t, n):
    """靠右對齊到 n 格（依顯示寬度）"""
    return " " * max(0, n - dw(t)) + str(t)

def L(t, n):
    """靠左對齊到 n 格（依顯示寬度）"""
    return str(t) + " " * max(0, n - dw(t))

def ms(s): return int(datetime.strptime(s,"%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp()*1000)
def hm(t): return datetime.fromtimestamp(t/1000,TZ8).strftime("%m/%d %H:%M")

CLI = httpx.Client(timeout=20, headers=UA, follow_redirects=True)

def fetch(ep, after=None, limit=100):
    u = f"https://www.okx.com/api/v5/market/{ep}?instId={IID}&bar={BAR}&limit={limit}"
    if after: u += f"&after={after}"
    try:
        r = CLI.get(u)
    except Exception as e:
        if DEBUG: print(f"  [{ep}] 連線失敗 {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        if DEBUG: print(f"  [{ep}] HTTP {r.status_code}: {r.text[:150]}")
        return None
    try:
        j = r.json()
    except Exception as e:
        if DEBUG: print(f"  [{ep}] 回應非 JSON: {r.text[:150]}")
        return None
    if j.get("code") != "0":
        if DEBUG: print(f"  [{ep}] OKX code={j.get('code')} msg={j.get('msg')}")
        return None
    n = len(j.get("data") or [])
    if DEBUG:
        rng = ""
        if n: rng = f"｜{hm(int(j['data'][-1][0]))} ~ {hm(int(j['data'][0][0]))}"
        print(f"  [{ep}] after={after} -> {n} 根{rng}")
    return j

start, end = ms(FROM), ms(TO)
need_from = start - 40*SEC*1000
print(f"目標區間：{hm(start)} ~ {hm(end)}（含 40 根暖身，往回抓到 {hm(need_from)}）")

got = {}
after = end + SEC*1000
ep = "candles"
for page in range(15):
    j = fetch(ep, after)
    if j is None or not j.get("data"):
        if ep == "candles":
            print("  改用 history-candles 續抓")
            ep = "history-candles"; continue
        break
    before = len(got)
    for c in j["data"]:
        if len(c) >= 9 and str(c[8]) != "1": continue
        t = int(c[0])
        if t not in got:
            got[t] = {"ts":t,"o":Decimal(c[1]),"h":Decimal(c[2]),
                      "l":Decimal(c[3]),"c":Decimal(c[4])}
    after = int(j["data"][-1][0])
    if len(got) == before:
        if ep == "candles":
            ep = "history-candles"; continue
        break
    if got and min(got) <= need_from: break

kl = [got[t] for t in sorted(got)]
if not kl:
    sys.exit("抓不到 K 線（上面有每次請求的結果，請貼給我）")
print(f"\n取得 {len(kl)} 根 {SYM} {TF}｜{hm(kl[0]['ts'])} ~ {hm(kl[-1]['ts'])}")
inwin = [k for k in kl if start <= k["ts"] <= end]
print(f"其中落在目標區間：{len(inwin)} 根")
if not inwin:
    sys.exit("目標區間內沒有資料，請確認 FROM / TO 設定")

# HA
ha=[]
for i,k in enumerate(kl):
    hc=(k["o"]+k["h"]+k["l"]+k["c"])/4
    ho=(k["o"]+k["c"])/2 if i==0 else (ha[i-1]["ho"]+ha[i-1]["hc"])/2
    ha.append({"ts":k["ts"],"ho":ho,"hc":hc,
               "hh":max(k["h"],ho,hc),"hl":min(k["l"],ho,hc),
               "color":"G" if hc>=ho else "R"})
# ATR14 (Wilder, 原始K線)
trs=[]
for i,k in enumerate(kl):
    trs.append(k["h"]-k["l"] if i==0 else
               max(k["h"]-k["l"], abs(k["h"]-kl[i-1]["c"]), abs(k["l"]-kl[i-1]["c"])))
atr=[None]*len(kl)
if len(kl)>=14:
    p=sum(trs[:14],Decimal(0))/14; atr[13]=p
    for i in range(14,len(kl)):
        p=(p*13+trs[i])/14; atr[i]=p

ent_ts = ms(ENTRY_AT)
print(f"\n進場 {ENTRY_AT}｜{ENTRY_PX}｜{'做多' if DIR=='L' else '做空'}")
print("="*70)
W  = [7, 12, 7, 12, 4, 11, 10, 12, 12, 12]
HD = ["開盤", "均K開", "收盤", "均K收", "燈",
      "漲跌幅", "ATR14r", "本根損益", "累計損益", "變動損益"]
print("".join(R(HD[i], W[i]) for i in range(len(HD))))
print("-" * sum(W))

peak = 0.0; table = []; prev_px = None; prev_pnl = None
for i, k in enumerate(kl):
    if k["ts"] < start or k["ts"] > end: continue
    x = ha[i]
    body = float((x["hc"] - x["ho"]) / x["ho"] * 100) if x["ho"] else 0.0
    ar = float(atr[i] / k["c"] * 100) if atr[i] is not None and k["c"] else None
    lg = "\U0001F7E9" if x["color"] == "G" else "\U0001F7E5"
    px = float(x["hc"]) if PNL_ON == "ha" else float(k["c"])   # 損益基準價
    t_open = datetime.fromtimestamp(k["ts"] / 1000, TZ8).strftime("%H:%M")
    t_close = datetime.fromtimestamp(k["ts"] / 1000 + SEC, TZ8).strftime("%H:%M")
    if k["ts"] >= ent_ts:
        pnl = (px - ENTRY_PX) / ENTRY_PX * 100 if DIR == "L" else (ENTRY_PX - px) / ENTRY_PX * 100
        # 本根損益：與前一根基準價相比（第一根與進場價相比）
        base = prev_px if prev_px is not None else ENTRY_PX
        one = (px - base) / base * 100 if DIR == "L" else (base - px) / base * 100
        # 變動損益：本根累計損益 − 上一根累計損益（第一根無前值，留空）
        chg = None if prev_pnl is None else pnl - prev_pnl
        peak = max(peak, pnl)
        table.append({"ts": k["ts"], "pnl": pnl, "peak": peak, "dd": pnl - peak,
                      "c": px, "one": one, "chg": chg})
        prev_px = px; prev_pnl = pnl
        s1, s2 = f"{one:+.3f}%", f"{pnl:+.3f}%"
        s3 = "-" if chg is None else f"{chg:+.3f}%"
    else:
        s1 = s2 = s3 = "-"
    print(R(t_open, W[0]) + R(f"{float(x['ho']):.1f}", W[1])
          + R(t_close, W[2]) + R(f"{float(x['hc']):.1f}", W[3])
          + L(" " + lg, W[4])
          + R(f"{body:+.4f}%", W[5]) + R(f"{ar:.4f}%" if ar is not None else "-", W[6])
          + R(s1, W[7]) + R(s2, W[8]) + R(s3, W[9]))

print("-" * sum(W))
print(f"損益基準：{'均K收盤價' if PNL_ON == 'ha' else '原始K線收盤價（與實際下單一致）'}"
      f"｜進場價 {ENTRY_PX}｜{'做多' if DIR == 'L' else '做空'}")
print("本根損益＝與前一根基準價相比；累計損益＝與進場價相比")
print("變動損益＝本根累計損益 − 上一根累計損益（第一根無前值故留空）")

# 各回吐門檻試算
print("\n" + "="*78)
print("各回吐門檻試算（以收線價計，不含手續費與滑價）")
print("-"*70)
TW = [9, 15, 12, 12, 12, 11]
TH = ["門檻", "出場時間", "出場價", "觸發回吐", "出場利潤", "持有根數"]
print("".join(R(TH[i], TW[i]) for i in range(6)))
print("-" * sum(TW))
for th in TRIALS:
    hit=None
    for j,r in enumerate(table):
        if r["dd"]<=th: hit=(j,r); break
    if hit:
        j,r=hit
        print(R(f"{th:.1f}%", TW[0]) + R(hm(r["ts"]), TW[1]) + R(f"{r['c']:.1f}", TW[2])
              + R(f"{r['dd']:+.3f}%", TW[3]) + R(f"{r['pnl']:+.3f}%", TW[4]) + R(j+1, TW[5]))
    elif table:
        last=table[-1]
        print(R(f"{th:.1f}%", TW[0]) + R("未觸發", TW[1]) + R(f"{last['c']:.1f}", TW[2])
              + R(f"{last['dd']:+.3f}%", TW[3]) + R(f"{last['pnl']:+.3f}%", TW[4])
              + R(len(table), TW[5]) + "  ← 區間結束仍持有")
print("-" * sum(TW))

if table:
    best=max(table,key=lambda r:r["pnl"])
    worst=min(table,key=lambda r:r["dd"])
    print(f"區間內最高利潤　{best['pnl']:+.3f}%　於 {hm(best['ts'])}（收盤 {best['c']:.1f}）")
    print(f"區間內最大回吐　{worst['dd']:+.3f}%　於 {hm(worst['ts'])}"
          f"（當時利潤 {worst['pnl']:+.3f}%，最高 {worst['peak']:+.3f}%）")
    print(f"區間結束時利潤　{table[-1]['pnl']:+.3f}%（回吐 {table[-1]['dd']:+.3f}%）")
    print("-"*70)
    print("解讀：門檻要設在「大於區間內最大回吐」才不會被洗掉。")
    print("　　　例如最大回吐 -0.25%，門檻設 -0.1% 會提早出場，設 -0.3% 才抱得住。")

    # 逐根列出「若此刻門檻剛好等於當下回吐」會拿到多少
    print("\n" + "="*78)
    print("回吐分佈（每根的回吐值排序，幫你看門檻該壓在哪）")
    print("-"*70)
    dds=sorted((r["dd"] for r in table))
    n=len(dds)
    for q,lab in ((0,"最深"),(n//10,"前10%"),(n//4,"前25%"),(n//2,"中位")):
        print(f"  {lab:>6}回吐 {dds[min(q,n-1)]:+.3f}%")

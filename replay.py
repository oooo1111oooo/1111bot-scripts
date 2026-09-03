#!/usr/bin/env python3
"""均K 回溯分析：抓 OKX 歷史 K 線，重算 HA 燈號 / ATR14，並試算不同回吐門檻。
用法：改下面 CONFIG 區塊再執行。時間一律 UTC+8，用 K 線開盤時間。"""
import json, sys
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
TO       = "2026-09-03 00:35"    # 顯示終點
ENTRY_AT = "2026-09-02 23:25"    # 進場後第一根的開盤時間
ENTRY_PX = 76950.1               # 實際成交均價
DIR      = "L"                   # L / S
TRIALS   = [-0.1, -0.2, -0.3, -0.5, -0.8, -1.2]   # 想試算的回吐門檻(%)
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
print("="*78)
print(f"{'開盤時間':<12}{'燈':<3}{'收盤':>10}{'漲跌幅':>10}{'ATR14r':>9}{'利潤':>9}{'最高':>9}{'回吐':>9}")
print("-"*78)
peak=0.0; table=[]
for i,k in enumerate(kl):
    if k["ts"]<start or k["ts"]>end: continue
    x=ha[i]
    body=float((x["hc"]-x["ho"])/x["ho"]*100) if x["ho"] else 0.0
    ar=float(atr[i]/k["c"]*100) if atr[i] is not None and k["c"] else None
    lg="🟩" if x["color"]=="G" else "🟥"
    c=float(k["c"])
    if k["ts"]>=ent_ts:
        pnl=(c-ENTRY_PX)/ENTRY_PX*100 if DIR=="L" else (ENTRY_PX-c)/ENTRY_PX*100
        peak=max(peak,pnl); dd=pnl-peak
        table.append({"ts":k["ts"],"pnl":pnl,"peak":peak,"dd":dd,"c":c})
        ps=f"{pnl:>+8.3f}%"; qs=f"{peak:>+8.3f}%"; ds=f"{dd:>+8.3f}%"
    else:
        ps=qs=ds="        -"
    print(f"{hm(k['ts']):<12}{lg:<3}{c:>10.1f}{body:>+9.4f}%"
          f"{(f'{ar:.4f}%' if ar is not None else '-'):>9}{ps}{qs}{ds}")

# 各回吐門檻試算
print("\n" + "="*78)
print("各回吐門檻試算（以收線價計，不含手續費與滑價）")
print("-"*78)
print(f"{'門檻':>8}{'出場時間':>14}{'出場價':>11}{'出場利潤':>11}{'持有根數':>10}")
print("-"*78)
for th in TRIALS:
    hit=None
    for j,r in enumerate(table):
        if r["dd"]<=th: hit=(j,r); break
    if hit:
        j,r=hit
        print(f"{th:>7.1f}%{hm(r['ts']):>14}{r['c']:>11.1f}{r['pnl']:>+10.3f}%{j+1:>10}")
    else:
        last=table[-1] if table else None
        if last:
            print(f"{th:>7.1f}%{'未觸發':>14}{last['c']:>11.1f}{last['pnl']:>+10.3f}%{len(table):>10}"
                  f"   ← 到區間結束仍持有")
if table:
    best=max(table,key=lambda r:r["pnl"])
    print("-"*78)
    print(f"區間內最高利潤 {best['pnl']:+.3f}% 出現在 {hm(best['ts'])}（收盤 {best['c']:.1f}）")
    print(f"區間結束時利潤 {table[-1]['pnl']:+.3f}%")

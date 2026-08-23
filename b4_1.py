#!/usr/bin/env python3
"""B4-1 HA 計算驗證：抓 ETH 5m 普通K，兩種起始法算 HA，供比對 OKX 均K 圖。"""
from decimal import Decimal, getcontext
from datetime import datetime, timezone, timedelta
import httpx

getcontext().prec = 20
BASE = "https://www.okx.com"
INST = "ETH-USDT-SWAP"
TF = "5m"
TZ8 = timezone(timedelta(hours=8))

def fetch_klines(n=60):
    # OKX candles：最新在前，需反轉成舊→新
    r = httpx.get(BASE+f"/api/v5/market/candles?instId={INST}&bar={TF}&limit={n}",timeout=15).json()
    rows = list(reversed(r["data"]))
    out = []
    for k in rows:
        out.append({
            "ts": int(k[0]),
            "o": Decimal(k[1]), "h": Decimal(k[2]),
            "l": Decimal(k[3]), "c": Decimal(k[4]),
        })
    return out

def calc_ha(kl, start_mode):
    """start_mode: 'a' = (o+c)/2 ; 'b' = o"""
    ha = []
    for i, k in enumerate(kl):
        ha_close = (k["o"]+k["h"]+k["l"]+k["c"]) / 4
        if i == 0:
            ha_open = (k["o"]+k["c"])/2 if start_mode=="a" else k["o"]
        else:
            ha_open = (ha[i-1]["ho"] + ha[i-1]["hc"]) / 2
        ha_high = max(k["h"], ha_open, ha_close)
        ha_low  = min(k["l"], ha_open, ha_close)
        color = "🟩" if ha_close >= ha_open else "🟥"
        amp = (ha_high - ha_low) / k["c"] * 100  # 單根振幅%
        ha.append({"ho":ha_open,"hh":ha_high,"hl":ha_low,"hc":ha_close,
                   "color":color,"amp":amp})
    return ha

def fmt(d, q="0.01"):
    return str(d.quantize(Decimal(q)))

def show(kl, ha, label, last=15):
    print(f"\n{'='*70}")
    print(f"起始法 {label}  （顯示最近 {last} 根，時間 UTC+8）")
    print(f"{'='*70}")
    print(f"{'時間':<8}{'普通K 開/高/低/收':<34}{'HA收':<10}{'燈':<4}{'振幅%'}")
    print("-"*70)
    s = len(kl)-last
    for i in range(s, len(kl)):
        k=kl[i]; h=ha[i]
        t = datetime.fromtimestamp(k["ts"]/1000, TZ8).strftime("%H:%M")
        ohlc = f"{fmt(k['o'])}/{fmt(k['h'])}/{fmt(k['l'])}/{fmt(k['c'])}"
        print(f"{t:<8}{ohlc:<34}{fmt(h['hc']):<10}{h['color']:<3}{fmt(h['amp'],'0.0001')}")

def main():
    print("抓取 ETH-USDT-SWAP 5m 普通K線 60 根...")
    kl = fetch_klines(60)
    print(f"取得 {len(kl)} 根，最新一根收盤時間: "
          f"{datetime.fromtimestamp(kl[-1]['ts']/1000, TZ8).strftime('%Y-%m-%d %H:%M')} (UTC+8)")

    ha_a = calc_ha(kl, "a")
    ha_b = calc_ha(kl, "b")
    show(kl, ha_a, "a：第一根HA開=(開+收)/2")
    show(kl, ha_b, "b：第一根HA開=開盤價")

    print(f"\n{'='*70}")
    print("最近 15 根燈號序列（拿去比對 OKX App 的 ETH 5m 均K圖）：")
    print("  起始法 a:", " ".join(h["color"] for h in ha_a[-15:]))
    print("  起始法 b:", " ".join(h["color"] for h in ha_b[-15:]))
    diff = sum(1 for x,y in zip(ha_a[-15:],ha_b[-15:]) if x["color"]!=y["color"])
    print(f"  兩種起始法在最近15根的燈號差異: {diff} 根",
          "（0 = 起始誤差已消失，用哪種都一樣）" if diff==0 else "（仍有差異，需靠OKX比對決定）")

if __name__=="__main__":
    main()

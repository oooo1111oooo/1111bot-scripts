#!/usr/bin/env python3
"""B4-3 普K訊號判定 + TG預覽畫面。純計算，不下單。"""
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR, ROUND_CEILING
from datetime import datetime, timezone, timedelta
import httpx

BASE = "https://www.okx.com"
TZ8 = timezone(timedelta(hours=8))
TF_SEC = {"3m":180, "5m":300, "10m":600, "15m":900}

def inst_id(sym): return sym.replace("USDT","")+"-USDT-SWAP"

def get_spec(sym):
    iid=inst_id(sym)
    d=httpx.get(BASE+f"/api/v5/public/instruments?instType=SWAP&instId={iid}",timeout=15).json()["data"][0]
    t=httpx.get(BASE+f"/api/v5/market/ticker?instId={iid}",timeout=15).json()["data"][0]
    return {"iid":iid,"tick":Decimal(d["tickSz"]),"lot":Decimal(d["lotSz"]),
            "minsz":Decimal(d["minSz"]),"ctval":Decimal(d["ctVal"]),
            "maxlev":Decimal(d["lever"]),"last":Decimal(t["last"])}

def next_open_time(now, tf):
    """算下一根K棒開盤時間"""
    sec=TF_SEC[tf]
    epoch=int(now.timestamp())
    nxt=((epoch//sec)+1)*sec
    return datetime.fromtimestamp(nxt, TZ8)

def align(price, tick, direction):
    if direction=="L":
        return (price/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
    return (price/tick).to_integral_value(rounding=ROUND_CEILING)*tick

def calc_size(margin, lev, price, spec):
    contracts=(margin*lev/price)/spec["ctval"]
    return (contracts/spec["lot"]).to_integral_value(rounding=ROUND_DOWN)*spec["lot"]

def tg_preview(sym, direction, tf, lev, margin, offset, tp, sl, te):
    """產生 TG /run 預覽畫面"""
    spec=get_spec(sym)
    now=datetime.now(TZ8)
    nopen=next_open_time(now, tf)
    # 假設用現價當開盤價估算（實際會等真正開盤）
    op=spec["last"]
    if direction=="L":
        ambush=align(op*(1-offset/100), spec["tick"], "L")
        tp_px=align(ambush*(1+tp/100), spec["tick"], "S")
        sl_px=align(ambush*(1-sl/100), spec["tick"], "L")
    else:
        ambush=align(op*(1+offset/100), spec["tick"], "S")
        tp_px=align(ambush*(1-tp/100), spec["tick"], "L")
        sl_px=align(ambush*(1+sl/100), spec["tick"], "S")
    size=calc_size(margin, Decimal(lev), ambush, spec)
    dir_emoji="✨" if direction=="L" else "🅾️"
    dir_word="Long" if direction=="L" else "Short"
    te_ok = 1<=te<=900

    print("┌"+"─"*36)
    print(f"│ 💛 OKXLive普K｜o3333o")
    print(f"│ 事件：交易參數預覽")
    print(f"│ 商　　品：{dir_emoji} {sym} {dir_word} {lev}x")
    print(f"│ 週　　期：{tf}")
    print(f"│ 進場模式：直接埋伏 Maker")
    print(f"│ 開盤時間：{nopen.strftime('%H:%M:%S')}（下一根）")
    print(f"│ 開盤估價：{op}")
    print(f"│ 埋伏距離：{offset}%")
    print(f"│ 埋伏價格：{ambush}")
    print(f"│ 止盈 TP：{tp}% → {tp_px}")
    print(f"│ 止損 SL：{sl}% → {sl_px}")
    print(f"│ 持倉 TE：{te}s {'✓' if te_ok else '✗超出1~900'}")
    print(f"│ 未成交清場：TF邊界前3秒撤單重掛")
    print(f"│ 保 證 金：{margin} USDT")
    print(f"│ 下單張數：{size}")
    print(f"│ 名目價值：{(size*spec['ctval']*ambush):.4f} USDT")
    print(f"│ 最大槓桿：{spec['maxlev']}x")
    print(f"│ 確認期限：60秒內 /confirm")
    print(f"│ 時　　間：{now.strftime('%Y-%m-%d %H:%M:%S')} UTC+8")
    print("└"+"─"*36)

def main():
    print("普K /run 預覽畫面測試（TG 顯示格式）\n")
    print("測試1：/run ETHUSDT L 5m 1x 100 0.5 0.7 0.7 180")
    tg_preview("ETHUSDT","L","5m",1,Decimal(100),Decimal("0.5"),Decimal("0.7"),Decimal("0.7"),180)
    print("\n測試2：/run BTCUSDT S 15m 1x 50 0.3 0.3 0.9 300")
    tg_preview("BTCUSDT","S","15m",1,Decimal(50),Decimal("0.3"),Decimal("0.3"),Decimal("0.9"),300)
    print("\n測試3：TE超範圍 /run SUIUSDT L 5m 1x 20 0.5 0.5 0.5 999")
    tg_preview("SUIUSDT","L","5m",1,Decimal(20),Decimal("0.5"),Decimal("0.5"),Decimal("0.5"),999)
    print("\n✓ B4-3 完成（純計算，未下任何單）")

if __name__=="__main__":
    main()

#!/usr/bin/env python3
"""B2-3 精度對齊 + /leverage 引擎。純計算，不下單。"""
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_FLOOR, ROUND_CEILING
import httpx

BASE = "https://www.okx.com"

def inst_id(sym):
    return sym.replace("USDT","") + "-USDT-SWAP"

def get_spec(sym):
    """即時抓合約規格 + 現價"""
    iid = inst_id(sym)
    r = httpx.get(BASE+f"/api/v5/public/instruments?instType=SWAP&instId={iid}",timeout=15).json()
    d = r["data"][0]
    t = httpx.get(BASE+f"/api/v5/market/ticker?instId={iid}",timeout=15).json()
    px = Decimal(t["data"][0]["last"])
    return {
        "instId": iid,
        "tickSz": Decimal(d["tickSz"]),   # 價格最小跳動
        "lotSz":  Decimal(d["lotSz"]),    # 張數最小增量
        "minSz":  Decimal(d["minSz"]),    # 最小張數
        "ctVal":  Decimal(d["ctVal"]),    # 每張面值（幣）
        "maxLever": Decimal(d["lever"]),  # 最大槓桿
        "last": px,
    }

def align_price(price, tick, side):
    """埋伏價對齊 tickSz：Long 向下、Short 向上（保守，不縮短 OFFSET）"""
    if side == "L":
        return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    else:
        return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick

def calc_size(margin, leverage, price, spec):
    """由保證金反推張數，無條件捨去到 lotSz"""
    notional = margin * leverage          # 名目價值 USDT
    coin_qty = notional / price           # 可買幣量
    contracts = coin_qty / spec["ctVal"]  # 換算張數
    aligned = (contracts / spec["lotSz"]).to_integral_value(rounding=ROUND_DOWN) * spec["lotSz"]
    return aligned

def min_order_usdt(spec, leverage):
    """/leverage 核心：此刻最低委託金額（保證金）"""
    min_notional = spec["minSz"] * spec["ctVal"] * spec["last"]  # 最小張的名目價值
    return min_notional / leverage

def main():
    print("="*54)
    print("[1] 埋伏價對齊測試（普K OFFSET）")
    print("-"*54)
    for sym in ["BTCUSDT","ETHUSDT","XRPUSDT"]:
        s = get_spec(sym)
        px = s["last"]; offset = Decimal("0.5")  # 0.5%
        raw_long  = px * (1 - offset/100)
        raw_short = px * (1 + offset/100)
        al = align_price(raw_long, s["tickSz"], "L")
        ash= align_price(raw_short,s["tickSz"], "S")
        print(f"  {sym} 現價={px} tickSz={s['tickSz']}")
        print(f"    Long  理論={raw_long:.6f} → 對齊={al}  (距現價 {(px-al)/px*100:.4f}%)")
        print(f"    Short 理論={raw_short:.6f} → 對齊={ash} (距現價 {(ash-px)/px*100:.4f}%)")

    print("\n[2] 張數計算測試（保證金→張數）")
    print("-"*54)
    for sym, marg, lev in [("BTCUSDT",100,1),("SOLUSDT",50,2),("DOGEUSDT",20,1)]:
        s = get_spec(sym)
        size = calc_size(Decimal(marg), Decimal(lev), s["last"], s)
        actual_notional = size * s["ctVal"] * s["last"]
        print(f"  {sym} 保證金={marg} 槓桿={lev}x 現價={s['last']}")
        print(f"    → 下 {size} 張 (最小張={s['minSz']} lotSz={s['lotSz']})")
        print(f"    → 實際名目={actual_notional:.4f} USDT")
        if size < s["minSz"]:
            print(f"    ⚠ 低於最小張！此金額無法下單")

    print("\n[3] /leverage 查詢模擬（此刻最低委託金額 + 最大槓桿）")
    print("-"*54)
    for sym in ["BTCUSDT","ETHUSDT","HYPEUSDT","SUIUSDT","XAUUSDT","ADAUSDT"]:
        s = get_spec(sym)
        min1x = min_order_usdt(s, Decimal(1))
        print(f"  {sym:<10} 現價={str(s['last']):<12} 最小張={s['minSz']} "
              f"最低金額(1x)≈{min1x:.4f} USDT  最大槓桿={s['maxLever']}x")

    print("\n"+"="*54)
    print("✓ B2-3 精度引擎測試完成（純計算，未下任何單）")

if __name__=="__main__":
    main()

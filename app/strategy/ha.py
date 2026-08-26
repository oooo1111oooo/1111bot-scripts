"""HA（Heikin-Ashi）計算（B4-1 驗證，與 OKX 一致）。"""
from decimal import Decimal

def calc_ha(klines):
    """klines: [{o,h,l,c}...] 舊→新。回傳每根 HA + 燈號 + 單根振幅%。"""
    ha = []
    for i, k in enumerate(klines):
        hc = (k["o"] + k["h"] + k["l"] + k["c"]) / 4
        ho = (k["o"] + k["c"]) / 2 if i == 0 else (ha[i-1]["ho"] + ha[i-1]["hc"]) / 2
        hh = max(k["h"], ho, hc)
        hl = min(k["l"], ho, hc)
        color = "G" if hc >= ho else "R"
        amp = (hh - hl) / k["c"] * 100
        ha.append({"ts": k.get("ts"), "ho": ho, "hh": hh, "hl": hl, "hc": hc,
                   "color": color, "amp": amp})
    return ha

def merge_5m_to_10m(kl5):
    """兩根 5m 合成一根 10m，強制對齊 10 分鐘邊界。
    不論呼叫端有沒有先對齊，本函式都會自行處理：
      1. 丟掉開頭未落在 10m 邊界的 K 線
      2. 只有 (邊界, 邊界+5m) 這種緊鄰成對的才合成
      3. 尾端落單（10m 桶尚未收完）者丟棄
    """
    out = []
    i = 0
    n = len(kl5)
    while i < n:
        a = kl5[i]
        try:
            ts_a = int(a["ts"])
        except Exception:
            i += 1; continue
        if ts_a % 600000 != 0:
            i += 1; continue
        if i + 1 >= n:
            break
        b = kl5[i + 1]
        try:
            ts_b = int(b["ts"])
        except Exception:
            i += 1; continue
        if ts_b - ts_a != 300000:
            i += 1; continue
        out.append({"ts": ts_a, "o": a["o"],
                    "h": max(a["h"], b["h"]), "l": min(a["l"], b["l"]),
                    "c": b["c"]})
        i += 2
    return out

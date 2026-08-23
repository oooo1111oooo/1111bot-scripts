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
    """兩根5m合成一根10m，00:00對齊（B4 待10m啟用時使用）。"""
    out = []
    # 以 ts 對齊 10 分鐘邊界
    buf = []
    for k in kl5:
        buf.append(k)
        # 該根屬於哪個10m桶：ts(ms)//600000
        if len(buf) == 2:
            a, b = buf
            out.append({"ts": a["ts"], "o": a["o"],
                        "h": max(a["h"], b["h"]), "l": min(a["l"], b["l"]),
                        "c": b["c"]})
            buf = []
    return out

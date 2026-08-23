#!/usr/bin/env python3
"""B4-4 純函式封裝：把 B4 驗證過的邏輯寫進 app/ 正式模組。不下單。"""
import os

ROOT="/srv/1111bot"

FILES = {
"app/core/emoji.py": '''"""Emoji 字典（D3-A 凍結 + B4-3 微調定案）。所有模組發訊息一律引用此處。"""
BOT      = "\U0001F49B"   # 💛 bot 識別
LONG     = "\u2733\uFE0F" # ✳️ Long（綠色星芒）
SHORT    = "\U0001F17E\uFE0F" # 🅾️ Short
ENTRY    = "\U0001F514"   # 🔔 進場成交
WIN      = "\U0001F7E2"   # 🟢 出場獲利 / 淨損益正
LOSS     = "\U0001F534"   # 🔴 出場虧損 / 淨損益負
EVEN     = "\u26AA"       # ⚪ 打平
HOLD     = "\U0001F4CC"   # 📌 持倉中
HA_RED   = "\U0001F7E5"   # 🟥 HA 紅棒
HA_GREEN = "\U0001F7E9"   # 🟩 HA 綠棒

def dir_emoji(d): return LONG if d == "L" else SHORT
def dir_word(d):  return "Long" if d == "L" else "Short"
def pnl_emoji(v):
    if v > 0: return WIN
    if v < 0: return LOSS
    return EVEN
def ha_emoji(color): return HA_GREEN if color == "G" else HA_RED
''',

"app/core/precision.py": '''"""精度對齊（B2-3 驗證）。全用 Decimal。"""
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR, ROUND_CEILING

def align_price(price, tick, direction):
    """埋伏價對齊 tickSz：L 向下、S 向上（保守，不縮短 OFFSET）"""
    if direction == "L":
        return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick

def calc_size(margin, leverage, price, ct_val, lot_sz):
    """保證金→張數，無條件捨去到 lotSz"""
    contracts = (margin * leverage / price) / ct_val
    return (contracts / lot_sz).to_integral_value(rounding=ROUND_DOWN) * lot_sz

def min_order_usdt(min_sz, ct_val, last, leverage):
    """此刻最低委託金額（/leverage 用）"""
    return (min_sz * ct_val * last) / leverage
''',

"app/strategy/ha.py": '''"""HA（Heikin-Ashi）計算（B4-1 驗證，與 OKX 一致）。"""
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
''',

"app/strategy/heikin.py": '''"""均K 訊號判定（B4-2 驗證，使用者已核對燈號正確）。
進場：PRE根反轉前色 + POST根反轉後色，POST振幅累加>=門檻。
出場：EXIT根反向色，EXIT振幅累加>=門檻。振幅只加 POST/EXIT。"""
from decimal import Decimal

def judge_entry(ha, direction, PRE, POST, amp_req, idx):
    need = PRE + POST
    if idx < need - 1:
        return None
    seg = ha[idx-need+1:idx+1]
    pre_seg, post_seg = seg[:PRE], seg[PRE:]
    if direction == "L":
        pre_ok  = all(x["color"] == "R" for x in pre_seg)
        post_ok = all(x["color"] == "G" for x in post_seg)
    else:
        pre_ok  = all(x["color"] == "G" for x in pre_seg)
        post_ok = all(x["color"] == "R" for x in post_seg)
    amp_sum = sum((x["amp"] for x in post_seg), Decimal(0))
    return {"seg": seg, "pre_ok": pre_ok, "post_ok": post_ok,
            "amp_sum": amp_sum, "amp_ok": amp_sum >= amp_req,
            "hit": pre_ok and post_ok and amp_sum >= amp_req}

def judge_exit(ha, direction, EXIT, amp_req, idx):
    if idx < EXIT - 1:
        return None
    seg = ha[idx-EXIT+1:idx+1]
    want = "R" if direction == "L" else "G"
    color_ok = all(x["color"] == want for x in seg)
    amp_sum = sum((x["amp"] for x in seg), Decimal(0))
    return {"seg": seg, "color_ok": color_ok,
            "amp_sum": amp_sum, "amp_ok": amp_sum >= amp_req,
            "hit": color_ok and amp_sum >= amp_req}
''',

"app/strategy/normal.py": '''"""普K 訊號判定（B4-3 驗證）。
開盤取價→OFFSET埋伏(L向下/S向上)→TP/SL/TE出場。TP/SL 以埋伏成交價計算。"""
from decimal import Decimal
from app.core.precision import align_price, calc_size

TF_SEC = {"3m": 180, "5m": 300, "10m": 600, "15m": 900}

def next_open_epoch(now_epoch, tf):
    sec = TF_SEC[tf]
    return ((now_epoch // sec) + 1) * sec

def plan_entry(open_price, direction, offset_pct, tick):
    """依開盤價算埋伏價"""
    if direction == "L":
        return align_price(open_price * (1 - offset_pct/100), tick, "L")
    return align_price(open_price * (1 + offset_pct/100), tick, "S")

def plan_exits(ambush_px, direction, tp_pct, sl_pct, tick):
    """TP/SL 以埋伏成交價為基準"""
    if direction == "L":
        tp = align_price(ambush_px * (1 + tp_pct/100), tick, "S")
        sl = align_price(ambush_px * (1 - sl_pct/100), tick, "L")
    else:
        tp = align_price(ambush_px * (1 - tp_pct/100), tick, "L")
        sl = align_price(ambush_px * (1 + sl_pct/100), tick, "S")
    return {"tp": tp, "sl": sl}

def valid_te(te_sec):
    return 1 <= te_sec <= 900
''',
}

def main():
    print("B4-4 封裝：建立 app/ 正式模組\n")
    for rel, content in FILES.items():
        path = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        print(f"  ✓ {rel} ({len(content)} bytes)")

    # 確保 app 及子目錄有 __init__.py
    for d in ["app", "app/core", "app/strategy"]:
        initp = os.path.join(ROOT, d, "__init__.py")
        if not os.path.exists(initp):
            open(initp, "w").close()
            print(f"  ✓ {d}/__init__.py")

    print("\n[import 測試]")
    import subprocess
    r = subprocess.run(
        [f"{ROOT}/.venv/bin/python", "-c",
         "import sys; sys.path.insert(0,'"+ROOT+"'); "
         "from app.core import emoji, precision; "
         "from app.strategy import ha, heikin, normal; "
         "from decimal import Decimal; "
         "print('emoji Long:', emoji.LONG, '| Short:', emoji.SHORT); "
         "print('HA test:', ha.calc_ha([{'o':Decimal(100),'h':Decimal(102),'l':Decimal(99),'c':Decimal(101)}])[0]['color']); "
         "print('ALL MODULES OK')"],
        capture_output=True, text=True)
    print(r.stdout)
    if r.stderr: print("STDERR:", r.stderr)

    print("[commit]")
    os.chdir(ROOT)
    os.system("git add app/ && git commit -q -m 'B4-4: 策略引擎封裝(emoji/precision/ha/heikin/normal)' && git log --oneline | head -8")
    print("\n✓ B4-4 完成，B4 策略引擎封裝結束")

if __name__=="__main__":
    main()

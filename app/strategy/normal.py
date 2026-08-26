"""普K 訊號判定（B4-3 驗證）。
開盤取價→OFFSET埋伏(L向下/S向上)→TP/SL/TE出場。TP/SL 以埋伏成交價計算。
註：普K 主程式 run_bot.py 自帶 10 種 TF_SEC，本檔的 TF_SEC 僅供均K 使用。"""
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

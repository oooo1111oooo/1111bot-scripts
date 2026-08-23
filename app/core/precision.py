"""精度對齊（B2-3 驗證）。全用 Decimal。"""
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

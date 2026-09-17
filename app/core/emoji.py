"""Emoji 字典（D3-A 凍結 + B4-3 微調定案）。所有模組發訊息一律引用此處。"""
BOT      = "💛"   # 💛 bot 識別
LONG     = "✳️" # ✳️ Long（綠色星芒）
SHORT    = "🅾️" # 🅾️ Short
ENTRY    = "🔔"   # 🔔 進場成交
WIN      = "🟢"   # 🟢 出場獲利 / 淨損益正
LOSS     = "🔴"   # 🔴 出場虧損 / 淨損益負
EVEN     = "⚪"       # ⚪ 打平
HOLD     = "📌"   # 📌 持倉中
WARN     = "⚠️"   # ⚠️ 警告／注意
OK       = "✅"   # ✅ 確認／成功
KLINE_UP   = "🟩"   # 🟩 K棒上漲
KLINE_DOWN = "🟥"   # 🟥 K棒下跌
CHART    = "📊"   # 📊 報表／統計
RELOAD   = "🔄"   # 🔄 重啟／重新載入
UP       = "🟩"   # 🟩 K棒漲（同 KLINE_UP）
DOWN     = "🟥"   # 🟥 K棒跌（同 KLINE_DOWN）
MOVE_PROFIT = "✅"   # ✅ 動態SL：往獲利方向移動（實際獲利 > move_pct）
MOVE_TIME   = "⏱️"   # ⏱️ 動態SL：時間到的小移動（依 move_pct 固定幅度）
MOVE_FAIL   = "🚫"   # 🚫 動態SL：OKX API 調整失敗（上次SL仍守著）

def dir_emoji(d): return LONG if d == "L" else SHORT
def dir_word(d):  return "L" if d == "L" else "S"
def pnl_emoji(v):
    if v > 0: return WIN
    if v < 0: return LOSS
    return EVEN
def kline_emoji(color): return KLINE_UP if color == "G" else KLINE_DOWN
def price_emoji(cur, prev):
    if cur > prev: return UP
    if cur < prev: return DOWN
    return EVEN

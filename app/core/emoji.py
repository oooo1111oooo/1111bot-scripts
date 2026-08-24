"""Emoji 字典（D3-A 凍結 + B4-3 微調定案）。所有模組發訊息一律引用此處。"""
BOT      = "💛"   # 💛 bot 識別
LONG     = "✳️" # ✳️ Long（綠色星芒）
SHORT    = "🅾️" # 🅾️ Short
ENTRY    = "🔔"   # 🔔 進場成交
WIN      = "🟢"   # 🟢 出場獲利 / 淨損益正
LOSS     = "🔴"   # 🔴 出場虧損 / 淨損益負
EVEN     = "⚪"       # ⚪ 打平
HOLD     = "📌"   # 📌 持倉中
HA_RED   = "🟥"   # 🟥 HA 紅棒
HA_GREEN = "🟩"   # 🟩 HA 綠棒

def dir_emoji(d): return LONG if d == "L" else SHORT
def dir_word(d):  return "L" if d == "L" else "S"
def pnl_emoji(v):
    if v > 0: return WIN
    if v < 0: return LOSS
    return EVEN
def ha_emoji(color): return HA_GREEN if color == "G" else HA_RED

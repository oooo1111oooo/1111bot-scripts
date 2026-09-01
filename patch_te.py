# -*- coding: utf-8 -*-
import io, sys, os

p = os.environ.get("TARGET", "/srv/1111bot/run_bot.py")
s = io.open(p, encoding="utf-8").read()
fails = []

def rep(old, new, label):
    global s
    n = s.count(old)
    if n != 1:
        fails.append("FAIL: %s (found %d)" % (label, n))
        return
    s = s.replace(old, new)
    print("OK: " + label)

CMD = 'async def cmd_te(u, c):\n    """設定固定持倉秒數。用法：/te 120（30~300）"""\n    global HOLD_SEC\n    if not c.args:\n        await reply(u, f"{E.BOT} 目前持倉秒數 TE：{HOLD_SEC} 秒\\n"\n                       f"可設 30~300\\n變更：/te 90\\n"\n                       f"（出場後再掛的門檻固定 {ENTRY_CUTOFF} 秒，不受此設定影響）")\n        return\n    try:\n        v = int(c.args[0])\n    except Exception:\n        await reply(u, f"{E.BOT} 請輸入整數秒數，例如 /te 90"); return\n    if not 30 <= v <= 300:\n        await reply(u, f"{E.BOT} 秒數須介於 30~300"); return\n    old = HOLD_SEC\n    HOLD_SEC = v\n    save_state()\n    await reply(u, f"{E.BOT} ✅ 持倉秒數 TE：{old} → {v} 秒\\n"\n                   f"立即對所有策略生效（含已在持倉中的）\\n時間：{hhmmss()}")\n\n'

rep('HOLD_SEC = 120       # 固定持倉秒數（TE）：進場後最多持有幾秒',
    'HOLD_SEC = 120       # 固定持倉秒數（TE）：可用 /te 30~300 動態調整，存檔保留',
    "1. HOLD_SEC 註解")

rep('        data = {"chat": CHAT_ID, "tf": ACCOUNT_TF, "stats": STATS, "strats": []}',
    '        data = {"chat": CHAT_ID, "tf": ACCOUNT_TF, "te": HOLD_SEC, "stats": STATS, "strats": []}',
    "2. 存檔寫入 TE")

rep('    global CHAT_ID, ACCOUNT_TF, STATS',
    '    global CHAT_ID, ACCOUNT_TF, STATS, HOLD_SEC',
    "3. startup_recover global")

rep('    CHAT_ID = data.get("chat"); ACCOUNT_TF = data.get("tf", "5m"); STATS = data.get("stats", {})',
    '    CHAT_ID = data.get("chat"); ACCOUNT_TF = data.get("tf", "5m"); STATS = data.get("stats", {})\n    try:\n        v = int(data.get("te") or HOLD_SEC)\n        if 30 <= v <= 300: HOLD_SEC = v\n    except Exception: pass',
    "4. 啟動還原 TE")

rep('         f"撤單/持倉秒數：{ENTRY_CUTOFF}秒",',
    '         f"持倉秒數 TE：{HOLD_SEC}秒｜再掛門檻：{ENTRY_CUTOFF}秒",',
    "5. /status 顯示 TE")

rep("async def cmd_timeframe(u, c):", CMD + "async def cmd_timeframe(u, c):", "6. 新增 cmd_te")

rep('("timeframe", cmd_timeframe), ("coins", cmd_coins)]:',
    '("timeframe", cmd_timeframe), ("te", cmd_te), ("coins", cmd_coins)]:',
    "7. 註冊 /te")

rep('            BotCommand("timeframe", "週期"),',
    '            BotCommand("timeframe", "週期"),\n            BotCommand("te", "持倉秒數 30~300"),',
    "8. Menu 加 /te")

rep('        "/timeframe 查看/設定週期\\n/coins 幣種\\n"',
    '        "/timeframe 查看/設定週期\\n/te 持倉秒數（30~300）\\n/coins 幣種\\n"',
    "9. /menu 加 /te")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

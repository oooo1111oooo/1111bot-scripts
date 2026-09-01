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

rep('            tf_end = oe + tf_sec\n            deadline = tf_end - ENTRY_CUTOFF   # 剩 60 秒就不再等成交',
    '            tf_end = oe + tf_sec\n            deadline = tf_end                  # 未成交等滿整個 TF 才撤，撤完立刻重掛',
    "1. 撤單改在 TF 結束")

rep('            if S.get("catchup") and cur != S.get("last_open") and room >= ENTRY_CUTOFF:',
    '            # 撤單後或出場後：只要本 TF 剩餘 >= ENTRY_CUTOFF 就立刻掛，\n            # 允許同一個 TF 內再掛一輪（出場後若時間夠）。\n            if S.get("catchup") and room >= ENTRY_CUTOFF:',
    "2. 出場後同 TF 可再掛")

rep('        if not reason and (time.time() - ee >= HOLD_SEC or time.time() >= tf_end - CLOSE_LEAD):\n            reason = "Time_Exit"',
    '        if not reason and time.time() - ee >= HOLD_SEC:\n            reason = "Time_Exit"',
    "3. 持倉滿 HOLD_SEC 才出場（可跨 TF）")

rep('            # 輪詢成交，直到 TF 剩餘不足 ENTRY_CUTOFF 秒',
    '            # 輪詢成交，直到本 TF 結束',
    "4. 註解")

rep('    """固定持倉 HOLD_SEC 秒即平倉（TE）；tf_end 僅作為不跨輪的安全上限。"""',
    '    """固定持倉 HOLD_SEC 秒即平倉（TE），允許跨 TF。"""',
    "5. monitor 說明")

rep('        f"未成交且剩餘不足 {ENTRY_CUTOFF}s → 撤單放棄本輪\\n"\n        f"已進場未觸發 TP/SL → 持倉滿 {HOLD_SEC}s 平倉（TE）\\n"',
    '        f"未成交 → TF 結束時撤單，立刻重掛下一輪\\n"\n        f"已進場未觸發 TP/SL → 持倉滿 {HOLD_SEC}s 平倉（TE）\\n"\n        f"出場後本 TF 剩餘 >= {ENTRY_CUTOFF}s 即再掛一輪\\n"',
    "6. /menu 說明")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

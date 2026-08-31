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

rep('TF_SEC = {"3m": 180, "4m": 240, "5m": 300, "6m": 360, "10m": 600,\n          "12m": 720, "15m": 900, "20m": 1200, "30m": 1800, "60m": 3600}',
    'TF_SEC = {"3m": 180, "4m": 240, "5m": 300, "6m": 360, "10m": 600}',
    "1. TF 縮為 5 種")

rep('ENTRY_CUTOFF = 60    # TF 剩餘不足幾秒就放棄進場（撤掉未成交單、也不補掛）\nCLOSE_LEAD = 2       # TF 結束前幾秒強制平倉（TE）',
    'HOLD_SEC = 90        # 固定持倉秒數（TE）：進場後最多持有幾秒\nENTRY_CUTOFF = 90    # TF 剩餘不足幾秒就放棄進場（＝HOLD_SEC，確保持倉能跑滿）\nCLOSE_LEAD = 2       # 安全上限：無論如何不晚於 TF 結束前幾秒平倉',
    "2. HOLD_SEC 90 + ENTRY_CUTOFF 90")

rep('    """tf_end：本 TF 的結束時刻（epoch）。到 tf_end-CLOSE_LEAD 秒一律平倉。"""',
    '    """固定持倉 HOLD_SEC 秒即平倉（TE）；tf_end 僅作為不跨輪的安全上限。"""',
    "3. monitor 說明")

rep('        if not reason and time.time() >= tf_end - CLOSE_LEAD: reason = "Time_Exit"',
    '        if not reason and (time.time() - ee >= HOLD_SEC or time.time() >= tf_end - CLOSE_LEAD):\n            reason = "Time_Exit"',
    "4. 出場改固定 90 秒")

OLDNB = 'NATIVE_BARS = {"3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "1H"}'
NEWNB = 'NATIVE_BARS = {"3m": "3m", "5m": "5m"}   # 4m/6m 無原生 K 線；10m 由兩根 5m 合成'
cnt = s.count(OLDNB)
s = s.replace(OLDNB, NEWNB)
print("OK: 5. NATIVE_BARS（%d 處）" % cnt)

rep('    supported = "3m/5m/10m/15m/30m/60m"',
    '    supported = "3m/5m/10m"',
    "6. /amp 支援週期")

rep('        f"未成交且剩餘不足 {ENTRY_CUTOFF}s → 撤單放棄本輪\\n"\n        f"已進場未觸發 TP/SL → TF 結束前 {CLOSE_LEAD}s 平倉（TE）\\n"',
    '        f"未成交且剩餘不足 {ENTRY_CUTOFF}s → 撤單放棄本輪\\n"\n        f"已進場未觸發 TP/SL → 持倉滿 {HOLD_SEC}s 平倉（TE）\\n"',
    "7. /menu 說明")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

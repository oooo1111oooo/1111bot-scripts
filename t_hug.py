#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.3 緊貼度參數化 —— 離線驗證（載入真檔案 run_bot.py）"""
import sys
from decimal import Decimal as D
sys.path.insert(0, ".")
import run_bot as R

TICK = D("0.0001")                    # WIF
SPEC = {"tick": TICK, "iid": "WIF-USDT-SWAP"}
ok = bad = 0

def chk(name, got, want):
    global ok, bad
    good = (got == want)
    ok, bad = ok + good, bad + (not good)
    print(f"  {'✅' if good else '🔴'} {name}: {got}" + ("" if good else f"  ← 應為 {want}"))

def S(hug=None, front="0.2604", back="0.2617"):
    d = {"spec": SPEC, "front_px": front, "back_px": back}
    if hug is not None:
        d["hug"] = D(hug)
    return d

print("=" * 66)
print("【1】hug_pct 是否改由 S['hug'] 決定")
chk("指定 0.25%", R.hug_pct(S("0.0025"), "front"), D("0.0025"))
chk("指定 0.40%", R.hug_pct(S("0.0040"), "front"), D("0.0040"))
chk("指定 0.15%", R.hug_pct(S("0.0015"), "back"), D("0.0015"))

print("\n【2】tick 地板（3檔）仍然生效，且只在該生效時生效")
floor_f = (TICK * R.MIN_HUG_TICKS) / D("0.2604")      # 0.115207%
chk("指定 0.05% → 被地板抬高", R.hug_pct(S("0.0005"), "front"), floor_f)
chk("指定 0.25% → 不動（>地板）", R.hug_pct(S("0.0025"), "front"), D("0.0025"))
print(f"     （WIF 3檔地板 = {float(floor_f*100):.4f}%）")

print("\n【3】舊存檔沒有 hug 欄位 → 回退 HUG_FIXED 預設值")
legacy = {"spec": SPEC, "front_px": "1.5000", "back_px": "1.5000"}   # 粗價，地板不綁
chk("完全沒有 hug 欄位", R.hug_pct(legacy, "front"), R.HUG_FIXED)
chk("hug 是空字串", R.hug_pct({**legacy, "hug": ""}, "front"), R.HUG_FIXED)
chk("hug 是 0", R.hug_pct({**legacy, "hug": D(0)}, "front"), R.HUG_FIXED)
chk("hug 是字串數字", R.hug_pct({**legacy, "hug": "0.003"}, "front"), D("0.003"))

print("\n【4】sl_decide 本身未被改動（規則表回歸）")
E, TKF = D("0.2604"), TICK
cases = [
    # (方向, 現價, 現SL, F, hedged, 期望action, 期望rule)
    ("S", D("0.2604"), D("0.2620"), D("0.001"), False, "hold", 1),   # 規則1 不動
    ("S", D("0.2617"), D("0.2620"), D("0.001"), False, "hold", 3),   # 模式一往SL 不動
    ("S", D("0.2602"), D("0.2620"), D("0.001"), False, "move", 2),   # 往TP超費率 緊貼
    ("S", D("0.2603"), D("0.2620"), D("0.001"), False, "hold", 3),   # 往TP未達費率
    ("S", D("0.2617"), D("0.2620"), D("0.001"), True,  "move", 4),   # 未觸地板：0.2619，有1檔改善
    ("S", D("0.2617"), D("0.2620"), D("0.0011521"), True, "hold", 0),  # 觸地板後：落在0.2620，棘輪擋下
    ("S", D("0.2606"), D("0.2620"), D("0.001"), True,  "move", 4),   # 模式二 減損
]
for d, cur, sl0, F, hed, wa, wr in cases:
    act, px, why, rule = R.sl_decide(d, E, cur, sl0, F, TKF, hed, R.FEE_A)
    tag = f"{d} 現價{cur} SL{sl0} hedged={hed}"
    chk(f"{tag} → {act}/規則{rule}", (act, rule), (wa, wr))

print("\n【5】重現 2026-09-22 WIF 那一場：舊參數 vs 新參數")
print("     A 做空 進場 0.2604｜靜態SL 0.2620（0.6%）")
for label, gap_pct, hug in (("舊 間距0.5% 緊貼0.1%", D("0.5"), D("0.001")),
                            ("新 間距0.1% 緊貼0.1%", D("0.1"), D("0.001")),
                            ("新 間距0.25% 緊貼0.15%", D("0.25"), D("0.0015"))):
    b_trig = R.align(E * (1 + gap_pct / 100), TICK, "L")
    s2 = S(str(hug))
    F = R.hug_pct(s2, "front")
    act, px, why, rule = R.sl_decide("S", E, b_trig, D("0.2620"), F, TICK, True, R.FEE_A)
    room = D("0.6") - gap_pct - F * 100
    print(f"  {label}")
    print(f"     B觸發 {b_trig}｜實際緊貼 {float(F*100):.4f}%｜減損空間 {float(room):+.3f}%")
    print(f"     → B觸發瞬間 A 的規則4：{act}"
          + (f" 到 {px}（改善 {float((D('0.2620')-px)/E*100):.3f}%）" if act == "move"
             else f"（{why}）← A 動不了"))

print("\n【6】/run 護欄邏輯（照 cmd_run 的判斷式重算）")
def guard(gap, hug, sl):
    if hug <= 0: return "拒絕：緊貼度必須大於 0"
    if hug >= sl: return "拒絕：緊貼度 ≥ SL"
    if gap >= sl: return "拒絕：間距 ≥ SL"
    if gap + hug >= sl: return f"⚠️ 警告：減損空間 {float(sl-gap-hug):+.3f}%"
    return f"✅ 通過：減損空間 {float(sl-gap-hug):+.3f}%"
for g, h, s_ in ((D("0.5"), D("0.1"), D("0.6")), (D("0.5"), D("0.115"), D("0.6")),
                 (D("0.1"), D("0.1"), D("0.6")), (D("0.25"), D("0.15"), D("0.8")),
                 (D("0.2"), D("0.2"), D("0.4")), (D("0.7"), D("0.1"), D("0.6")),
                 (D("0.2"), D("0"), D("0.6")), (D("0.2"), D("0.6"), D("0.6"))):
    print(f"  間距{g}% 緊貼{h}% SL{s_}%  →  {guard(g, h, s_)}")

print("\n" + "=" * 66)
print(f"結果：通過 {ok}｜失敗 {bad}")
sys.exit(1 if bad else 0)

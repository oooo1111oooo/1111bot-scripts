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

HELPERS = '# ---------- K 線 / 振幅（/amp 用） ----------\nNATIVE_BARS = {"3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "1H"}\n\nasync def get_klines(iid, bar, limit=300):\n    """只取已收線（confirm=1）的 K 線，回傳舊->新。"""\n    r = await pub(f"/api/v5/market/candles?instId={iid}&bar={bar}&limit={min(300, limit)}")\n    if r.get("code") != "0":\n        return []\n    out = []\n    for c in (r.get("data") or []):\n        try:\n            if len(c) >= 9 and str(c[8]) != "1":\n                continue\n            out.append({"ts": int(c[0]), "o": Decimal(c[1]), "h": Decimal(c[2]),\n                        "l": Decimal(c[3]), "c": Decimal(c[4])})\n        except Exception:\n            continue\n    out.reverse()\n    return out\n\ndef _merge2(kl5):\n    """兩根 5m 合成一根 10m，強制對齊 10 分鐘邊界。"""\n    out = []\n    i = 0\n    n = len(kl5)\n    while i < n:\n        a = kl5[i]\n        ts_a = int(a["ts"])\n        if ts_a % 600000 != 0:\n            i += 1; continue\n        if i + 1 >= n:\n            break\n        b = kl5[i + 1]\n        if int(b["ts"]) - ts_a != 300000:\n            i += 1; continue\n        out.append({"ts": ts_a, "o": a["o"], "h": max(a["h"], b["h"]),\n                    "l": min(a["l"], b["l"]), "c": b["c"]})\n        i += 2\n    return out\n\nasync def klines_for_tf(iid, tf, want=300):\n    """依 TF 取原始 K 線。10m 由兩根 5m 合成；其餘須為 OKX 原生週期。"""\n    if tf == "10m":\n        return _merge2(await get_klines(iid, "5m", 300))\n    bar = NATIVE_BARS.get(tf)\n    if not bar:\n        return None\n    return await get_klines(iid, bar, want)\n\ndef calc_amp(kl):\n    """每根回傳 (傳統振幅%, 本根收盤為分母的振幅%)。"""\n    out = []\n    for i, k in enumerate(kl):\n        rng = k["h"] - k["l"]\n        prev_c = kl[i-1]["c"] if i > 0 else k["c"]\n        a1 = (rng / prev_c * 100) if prev_c else Decimal(0)\n        a2 = (rng / k["c"] * 100) if k["c"] else Decimal(0)\n        out.append((a1, a2))\n    return out\n\nasync def _reply_long(u, head, lines, tail):\n    """長清單拆多則送出（Telegram 單則上限 4096 字元）。"""\n    LIM = 3500\n    buf = list(head); msgs = []\n    for ln in lines:\n        if sum(len(x) + 1 for x in buf) + len(ln) + 1 > LIM and len(buf) > len(head):\n            msgs.append("\\n".join(buf)); buf = list(head)\n        buf.append(ln)\n    buf += tail\n    msgs.append("\\n".join(buf))\n    for i, m in enumerate(msgs):\n        if len(msgs) > 1:\n            m = m.replace("事件：振幅檢視", "事件：振幅檢視（%d/%d）" % (i+1, len(msgs)), 1)\n        await reply(u, m)\n\n'

CMD = 'async def cmd_amp(u, c):\n    """原K 振幅檢視。用法：/amp SOLUSDT [根數 3~300]"""\n    supported = "3m/5m/10m/15m/30m/60m"\n    if not c.args:\n        await reply(u, f"{E.BOT} 用法：/amp SOLUSDT 30\\n"\n                       f"根數 3~300（預設 20）\\n"\n                       f"欄位：漲跌｜振幅%（前收為分母）｜振幅%（本收為分母）\\n"\n                       f"支援週期：{supported}\\n"\n                       f"目前週期：{ACCOUNT_TF}")\n        return\n    sym = c.args[0].upper()\n    n = 20\n    if len(c.args) >= 2:\n        try: n = max(3, min(300, int(c.args[1])))\n        except Exception: pass\n    if ACCOUNT_TF != "10m" and ACCOUNT_TF not in NATIVE_BARS:\n        await reply(u, f"{E.BOT} 目前週期 {ACCOUNT_TF} 無原生 K 線，無法查振幅\\n"\n                       f"可查週期：{supported}\\n請先 /timeframe 切換")\n        return\n    try:\n        spec = await get_spec(sym)\n    except Exception:\n        await reply(u, f"{E.LOSS} 找不到商品 {sym}"); return\n    kl = await klines_for_tf(spec["iid"], ACCOUNT_TF, want=min(300, n + 5))\n    if not kl:\n        await reply(u, f"{E.BOT} {sym} K 線取得失敗"); return\n    amps = calc_amp(kl)\n    st = max(0, len(kl) - n)\n    head = [f"{E.BOT} OKX原K｜{ACCT}",\n            f"事件：振幅檢視 {sym} {ACCOUNT_TF}",\n            f"共 {len(kl) - st} 根（舊→新）｜振幅%（前收）｜振幅%（本收）",\n            "━" * 10]\n    lines = []\n    for i in range(st, len(kl)):\n        k = kl[i]\n        tt = datetime.fromtimestamp(int(k["ts"]) / 1000, TZ8).strftime("%m/%d %H:%M")\n        lg = "🟩" if k["c"] >= k["o"] else "🟥"\n        a1, a2 = amps[i]\n        lines.append(f"{tt} {lg} {a1:.4f}% | {a2:.4f}%")\n    seg = [a for a, _ in amps[st:]]\n    ss = sorted(seg); m = len(ss)\n    med = ss[m // 2] if m % 2 else (ss[m // 2 - 1] + ss[m // 2]) / 2\n    avg = sum(seg, Decimal(0)) / m\n    tail = ["━" * 10,\n            f"{m}根統計（前收為分母）",\n            f"平均 {avg:.4f}% | 中位 {med:.4f}%",\n            f"最大 {max(seg):.4f}% | 最小 {min(seg):.4f}%",\n            f"時間：{hhmmss()}"]\n    await _reply_long(u, head, lines, tail)\n\n'

A = "# ---------- TG \u6307\u4ee4 ----------\n"
rep(A, A + HELPERS, "1. helpers")

B = "async def cmd_coins(u, c):"
rep(B, CMD + B, "2. cmd_amp")

rep('("summary", cmd_summary), ("timeframe", cmd_timeframe), ("coins", cmd_coins)]:',
    '("summary", cmd_summary), ("amp", cmd_amp),\n                    ("timeframe", cmd_timeframe), ("coins", cmd_coins)]:',
    "3. handler")

rep('            BotCommand("coins", "\u5e63\u7a2e"),\n            BotCommand("stopall", "\u505c\u5168\u90e8"),',
    '            BotCommand("coins", "\u5e63\u7a2e"),\n            BotCommand("amp", "\u632f\u5e45\u6aa2\u8996 3~300\u6839"),\n            BotCommand("stopall", "\u505c\u5168\u90e8"),',
    "4. menu button")

rep('"/status \u6240\u6709\u7b56\u7565\u73fe\u6cc1\\n/summary \u7576\u65e5\u6230\u5831\\n/timeframe \u67e5\u770b/\u8a2d\u5b9a\u9031\u671f\\n/coins \u5e63\u7a2e\\n"',
    '"/status \u6240\u6709\u7b56\u7565\u73fe\u6cc1\\n/summary \u7576\u65e5\u6230\u5831\\n"\n        "/amp \u5546\u54c1 \u6839\u6578  \u632f\u5e45\u6aa2\u8996\uff083~300\u6839\uff09\\n"\n        "/timeframe \u67e5\u770b/\u8a2d\u5b9a\u9031\u671f\\n/coins \u5e63\u7a2e\\n"',
    "5. menu text")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

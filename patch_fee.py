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

OD = 'async def order_detail(iid, oid, tries=10):\n    """查訂單成交明細（成交後即時可得）。回傳 {avgPx, fee, sz} 或 None。"""\n    if not oid:\n        return None\n    for _ in range(tries):\n        st = await api("GET", f"/api/v5/trade/order?instId={iid}&ordId={oid}")\n        if st.get("code") == "0" and st.get("data"):\n            dd = st["data"][0]\n            if dd.get("state") == "filled" and dd.get("avgPx"):\n                return {"avgPx": Decimal(dd["avgPx"]),\n                        "fee": Decimal(dd.get("fee") or "0"),\n                        "sz": Decimal(dd.get("accFillSz") or dd.get("sz") or "0")}\n        await asyncio.sleep(1)\n    return None\n'
PX = '    # 損益取值優先序：\n    #   1. 訂單成交明細（進場單 + 出場單）— 成交後即時可得，手續費為 OKX 實收\n    #   2. positions-history — 官方口徑，但落帳延遲可達數分鐘，僅機會性嘗試\n    #   3. 估算 — 兩者皆失敗時的最後手段，會在訊息上明確標示\n    xoid = (xr.get("data") or [{}])[0].get("ordId")\n    eoid = S.get("pos_oid")\n    od_in = await order_detail(iid, eoid, tries=3) if eoid else None\n    od_out = await order_detail(iid, xoid, tries=10)\n    src = None\n    if od_out:\n        xpx = od_out["avgPx"]\n        fee = od_out["fee"] + (od_in["fee"] if od_in else Decimal(0))\n        opx = od_in["avgPx"] if od_in else fpx\n        g = (xpx - opx) * size * spec["ctval"] if d == "L" else (opx - xpx) * size * spec["ctval"]\n        net = g + fee\n        nv = opx * size * spec["ctval"]\n        src = "訂單" if od_in else "訂單(出)"\n    ph = await close_record(iid, pos, t0, tries=3)\n    if ph:\n        g = Decimal(ph.get("pnl") or "0")\n        fee = Decimal(ph.get("fee") or "0") + Decimal(ph.get("fundingFee") or "0")\n        net = Decimal(ph.get("realizedPnl") or "0")\n        xpx = Decimal(ph.get("closeAvgPx") or "0") or xpx if src else await get_last(iid)\n        nv = Decimal(ph.get("openAvgPx") or fpx) * Decimal(ph.get("closeTotalPos") or size) * spec["ctval"]\n        src = "OKX"\n    if src is None:\n        src = "估算"\n        xpx = await get_last(iid)\n        g = (xpx - fpx) * size * spec["ctval"] if d == "L" else (fpx - xpx) * size * spec["ctval"]\n        fee = Decimal(0); net = g\n        nv = fpx * size * spec["ctval"]\n'
AUDIT = 'async def cmd_audit(u, c):\n    """對帳：本地紀錄 vs OKX positions-history。用法：/audit [YYYYMMDD]"""\n    day = c.args[0] if c.args else now8().strftime("%Y%m%d")\n    t = day[:4] + "-" + day[4:6] + "-" + day[6:8]\n    recs = load_trades(t)\n    if not recs:\n        await reply(u, f"{E.BOT} {t} 無本地交易紀錄"); return\n    await reply(u, f"{E.BOT} 對帳中：{t}，本地 {len(recs)} 筆…")\n    s8 = now8().replace(hour=0, minute=0, second=0, microsecond=0)\n    try:\n        s8 = s8.replace(year=int(day[:4]), month=int(day[4:6]), day=int(day[6:8]))\n    except Exception:\n        pass\n    sms = int(s8.timestamp() * 1000)\n    ph = []; after = ""; pages = 0\n    while pages < 20:\n        q = "/api/v5/account/positions-history?instType=SWAP&limit=100"\n        if after:\n            q += "&after=" + after\n        r = await api("GET", q)\n        if r.get("code") != "0":\n            break\n        batch = r.get("data") or []\n        if not batch:\n            break\n        pages += 1\n        stop = False\n        for p in batch:\n            ut = int(p.get("uTime") or 0)\n            if ut >= sms and ut < sms + 86400000:\n                ph.append(p)\n            elif ut < sms:\n                stop = True\n        if stop or len(batch) < 100:\n            break\n        after = batch[-1].get("posId") or ""\n        if not after:\n            break\n    ln = sum(Decimal(str(x.get("net") or "0")) for x in recs)\n    lf = sum(Decimal(str(x.get("fee") or "0")) for x in recs)\n    on = sum(Decimal(p.get("realizedPnl") or "0") for p in ph)\n    of = sum(Decimal(p.get("fee") or "0") + Decimal(p.get("fundingFee") or "0") for p in ph)\n    from collections import Counter\n    srcs = Counter(x.get("src") for x in recs)\n    L = [f"{E.BOT} OKX原K｜{ACCT}", f"📋 對帳 {t}", "━" * 10,\n         f"本地筆數：{len(recs)}｜OKX：{len(ph)}",\n         "來源分佈：" + "、".join(f"{k}×{v}" for k, v in srcs.items()),\n         "━" * 10,\n         f"本地淨損益：{ln:+.6f}",\n         f"OKX 淨損益：{on:+.6f}",\n         f"差異：{ln - on:+.6f}",\n         "━" * 10,\n         f"本地手續費：{lf:+.6f}",\n         f"OKX 手續費：{of:+.6f}",\n         f"差異：{lf - of:+.6f}",\n         "━" * 10]\n    if len(recs) != len(ph):\n        L.append(f"{E.LOSS} 筆數不符，OKX 可能尚未完全落帳")\n    elif abs(ln - on) < Decimal("0.000001") and abs(lf - of) < Decimal("0.000001"):\n        L.append("✅ 完全一致")\n    else:\n        L.append(f"{E.LOSS} 有差異，請人工核對")\n    zero = [x for x in recs if Decimal(str(x.get("fee") or "0")) == 0]\n    if zero:\n        L.append(f"⚠ 手續費為 0 的紀錄：{len(zero)} 筆")\n        for x in zero[:5]:\n            L.append(f"\u3000{x[\'ts\']} {x[\'sym\']} {x[\'dir\']} src={x.get(\'src\')}")\n    L.append(f"時間：{hhmmss()}")\n    await reply(u, "\\n".join(L))\n\n'
OLD = '    ph = await close_record(iid, pos, t0)\n    src = "OKX"\n    if ph:\n        g = Decimal(ph.get("pnl") or "0")\n        fee = Decimal(ph.get("fee") or "0") + Decimal(ph.get("fundingFee") or "0")\n        net = Decimal(ph.get("realizedPnl") or "0")\n        xpx = Decimal(ph.get("closeAvgPx") or "0") or await get_last(iid)\n        nv = Decimal(ph.get("openAvgPx") or fpx) * Decimal(ph.get("closeTotalPos") or size) * spec["ctval"]\n    else:\n        src = "估算"\n        xpx = await get_last(iid)\n        g = (xpx - fpx) * size * spec["ctval"] if d == "L" else (fpx - xpx) * size * spec["ctval"]\n        fee = Decimal(0); net = g\n        nv = fpx * size * spec["ctval"]\n'
OLDXOID = '    xoid = (xr.get("data") or [{}])[0].get("ordId")\n'

rep("async def close_record(iid, ps, after_ms, tries=10):",
    OD + "\nasync def close_record(iid, ps, after_ms, tries=3):",
    "1. order_detail + close_record 改 3 次")

rep(OLD, PX, "3. 取價改用訂單成交明細")

rep('f"淨損益：{net:+.6f} ({npv:+.3f}%) {E.pnl_emoji(net)}\\n時間：{hhmmss()}")',
    'f"淨損益：{net:+.6f} ({npv:+.3f}%) {E.pnl_emoji(net)}\\n"\n        + ("" if src == "OKX" else f"⚠ 來源：{src}\\n")\n        + f"時間：{hhmmss()}")',
    "4. 訊息標示資料來源")

rep('            S["pos_tp"] = str(tp); S["pos_sl"] = str(sl); S["pos_ee"] = ee; S["pos_pt"] = pt',
    '            S["pos_tp"] = str(tp); S["pos_sl"] = str(sl); S["pos_ee"] = ee; S["pos_pt"] = pt\n            S["pos_oid"] = oid',
    "5. 記錄進場單 ordId")

rep('"pos_open","pos_px","pos_tp","pos_sl","pos_ee","pos_pt","last_open","catchup")',
    '"pos_open","pos_px","pos_tp","pos_sl","pos_ee","pos_pt","pos_oid","last_open","catchup")',
    "6. SAVE_FIELDS")

rep('for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt", "last_open", "catchup"):',
    'for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt", "pos_oid", "last_open", "catchup"):',
    "7. rebuild 還原欄位")

CLR = '("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt")'
NCLR = '("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt", "pos_oid")'
cnt = s.count(CLR)
s = s.replace(CLR, NCLR)
print("OK: 8. 清除欄位加 pos_oid（%d 處）" % cnt)

rep("async def cmd_coins(u, c):", AUDIT + "async def cmd_coins(u, c):", "9. 新增 cmd_audit")
rep('("summary", cmd_summary), ("amp", cmd_amp),',
    '("summary", cmd_summary), ("amp", cmd_amp), ("audit", cmd_audit),',
    "10. 註冊 /audit")
rep('            BotCommand("amp", "振幅報表 Excel"),',
    '            BotCommand("amp", "振幅報表 Excel"),\n            BotCommand("audit", "對帳 OKX"),',
    "11. Menu 加 /audit")
rep('"/amp 商品 根數  振幅報表 Excel 寄信（3~2000根）\\n"',
    '"/amp 商品 根數  振幅報表 Excel 寄信（3~2000根）\\n"\n        "/audit [YYYYMMDD]  與 OKX 對帳\\n"',
    "12. /menu 加 /audit")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

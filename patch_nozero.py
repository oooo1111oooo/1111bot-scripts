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

OLDPX = '    # 損益取值優先序：\n    #   1. 訂單成交明細（進場單 + 出場單）— 成交後即時可得，手續費為 OKX 實收\n    #   2. positions-history — 官方口徑，但落帳延遲可達數分鐘，僅機會性嘗試\n    #   3. 估算 — 兩者皆失敗時的最後手段，會在訊息上明確標示\n    xoid = (xr.get("data") or [{}])[0].get("ordId")\n    eoid = S.get("pos_oid")\n    od_in = await order_detail(iid, eoid, tries=3) if eoid else None\n    od_out = await order_detail(iid, xoid, tries=10)\n    src = None\n    if od_out:\n        xpx = od_out["avgPx"]\n        fee = od_out["fee"] + (od_in["fee"] if od_in else Decimal(0))\n        opx = od_in["avgPx"] if od_in else fpx\n        g = (xpx - opx) * size * spec["ctval"] if d == "L" else (opx - xpx) * size * spec["ctval"]\n        net = g + fee\n        nv = opx * size * spec["ctval"]\n        src = "訂單" if od_in else "訂單(出)"\n    ph = await close_record(iid, pos, t0, tries=3)\n    if ph:\n        g = Decimal(ph.get("pnl") or "0")\n        fee = Decimal(ph.get("fee") or "0") + Decimal(ph.get("fundingFee") or "0")\n        net = Decimal(ph.get("realizedPnl") or "0")\n        xpx = Decimal(ph.get("closeAvgPx") or "0") or xpx if src else await get_last(iid)\n        nv = Decimal(ph.get("openAvgPx") or fpx) * Decimal(ph.get("closeTotalPos") or size) * spec["ctval"]\n        src = "OKX"\n    if src is None:\n        src = "估算"\n        xpx = await get_last(iid)\n        g = (xpx - fpx) * size * spec["ctval"] if d == "L" else (fpx - xpx) * size * spec["ctval"]\n        fee = Decimal(0); net = g\n        nv = fpx * size * spec["ctval"]\n    gp = (g / nv * 100) if nv else Decimal(0)\n    fp = (fee / nv * 100) if nv else Decimal(0)\n    npv = (net / nv * 100) if nv else Decimal(0)\n    hs = int(time.time() - ee)\n    amb_s = f"{round(ee - pt)}s" if pt else "-"\n    log_trade({"date": today8(), "sym": S["sym"], "dir": d, "reason": reason,\n               "ambush_s": round(ee - pt) if pt else 0, "hold_s": hs,\n               "gross": str(g), "fee": str(fee), "net": str(net), "nv": str(nv),\n               "src": src, "ts": hhmmss(),\n               "in_ts": datetime.fromtimestamp(ee, TZ8).strftime("%H:%M:%S"),\n               "tf": S["tf"], "margin": str(S["margin"]),\n               "in_px": str(fpx), "out_px": str(xpx)})\n'
NEWPX = '    # 財務數字一律以 OKX 為準，絕不估算。\n    #   1. positions-history（官方口徑，含資金費）\n    #   2. 訂單成交明細（OKX 實收手續費與成交均價）\n    #   3. 兩者皆取不到 -> 寫入 null 並標記 pending，由背景補帳任務事後回填\n    xoid = (xr.get("data") or [{}])[0].get("ordId")\n    eoid = S.get("pos_oid")\n    g = fee = net = nv = xpx = None\n    src = "PENDING"\n    ph = await close_record(iid, pos, t0, tries=3)\n    if ph:\n        g, fee, net, nv, xpx = _from_ph(ph, size, spec["ctval"], fpx)\n        src = "OKX"\n    else:\n        od_out = await order_detail(iid, xoid, tries=8)\n        od_in = await order_detail(iid, eoid, tries=2) if eoid else None\n        if od_out and od_in:\n            g, fee, net, nv, xpx = _from_orders(od_in, od_out, d, size, spec["ctval"])\n            src = "訂單"\n    hs = int(time.time() - ee)\n    amb_s = f"{round(ee - pt)}s" if pt else "-"\n    rec = {"date": today8(), "sym": S["sym"], "dir": d, "reason": reason,\n           "ambush_s": round(ee - pt) if pt else 0, "hold_s": hs,\n           "gross": None if g is None else str(g),\n           "fee": None if fee is None else str(fee),\n           "net": None if net is None else str(net),\n           "nv": None if nv is None else str(nv),\n           "src": src, "ts": hhmmss(),\n           "in_ts": datetime.fromtimestamp(ee, TZ8).strftime("%H:%M:%S"),\n           "tf": S["tf"], "margin": str(S["margin"]),\n           "in_px": str(fpx), "out_px": None if xpx is None else str(xpx),\n           "iid": iid, "pos": pos, "t0": t0, "eoid": eoid, "xoid": xoid}\n    log_trade(rec)\n    if src == "PENDING":\n        await notify(app, S["chat"],\n            f"{E.BOT} {E.LOSS} {S[\'sym\']} {E.dir_word(d)} 已平倉，但 OKX 損益尚未回報\\n"\n            f"出場原因：{reason}｜持倉 {hs}s\\n"\n            f"已標記待補帳，背景任務會自動回填（可用 /audit 追蹤）\\n時間：{hhmmss()}")\n        for a in ("pos_open", "pos_px", "pos_tp", "pos_sl", "pos_ee", "pos_pt", "pos_oid"):\n            S.pop(a, None)\n        save_state()\n        return True\n    gp = (g / nv * 100) if nv else Decimal(0)\n    fp = (fee / nv * 100) if nv else Decimal(0)\n    npv = (net / nv * 100) if nv else Decimal(0)\n'
HELPERS = 'def _from_ph(ph, size, ctval, fpx):\n    """由 positions-history 取值（OKX 官方口徑）。"""\n    g = Decimal(ph.get("pnl") or "0")\n    fee = Decimal(ph.get("fee") or "0") + Decimal(ph.get("fundingFee") or "0")\n    net = Decimal(ph.get("realizedPnl") or "0")\n    xpx = Decimal(ph.get("closeAvgPx") or "0")\n    opx = Decimal(ph.get("openAvgPx") or "0") or fpx\n    csz = Decimal(ph.get("closeTotalPos") or "0") or size\n    nv = opx * csz * ctval\n    return g, fee, net, nv, xpx\n\ndef _from_orders(od_in, od_out, d, size, ctval):\n    """由進出場訂單成交明細取值（OKX 實收手續費與成交均價）。"""\n    opx = od_in["avgPx"]; xpx = od_out["avgPx"]\n    fee = od_in["fee"] + od_out["fee"]\n    g = (xpx - opx) * size * ctval if d == "L" else (opx - xpx) * size * ctval\n    return g, fee, g + fee, opx * size * ctval, xpx\n\nasync def reconcile_pending(app, days=3):\n    """背景補帳：掃描近幾日紀錄檔，把 PENDING 的財務數字向 OKX 補齊。"""\n    filled = 0\n    for back in range(days):\n        t = (now8() - timedelta(days=back)).strftime("%Y-%m-%d")\n        fp = trade_file(t)\n        try:\n            arr = json.load(open(fp))\n        except Exception:\n            continue\n        changed = False\n        for rec in arr:\n            if rec.get("src") != "PENDING":\n                continue\n            iid = rec.get("iid"); pos = rec.get("pos")\n            if not iid or not pos:\n                continue\n            fpx = Decimal(rec.get("in_px") or "0")\n            size = Decimal("0")\n            g = fee = net = nv = xpx = None; src = None\n            ph = await close_record(iid, pos, int(rec.get("t0") or 0), tries=1)\n            if ph:\n                g, fee, net, nv, xpx = _from_ph(ph, size, Decimal("1"), fpx)\n                src = "OKX"\n            else:\n                od_out = await order_detail(iid, rec.get("xoid"), tries=1)\n                od_in = await order_detail(iid, rec.get("eoid"), tries=1)\n                if od_out and od_in:\n                    sz = od_out.get("sz") or Decimal("0")\n                    try:\n                        sp = await get_spec(rec["sym"])\n                        ctv = sp["ctval"]\n                    except Exception:\n                        ctv = Decimal("1")\n                    g, fee, net, nv, xpx = _from_orders(od_in, od_out, rec["dir"], sz, ctv)\n                    src = "訂單"\n            if src:\n                rec["gross"] = str(g); rec["fee"] = str(fee)\n                rec["net"] = str(net); rec["nv"] = str(nv)\n                rec["out_px"] = str(xpx); rec["src"] = src\n                rec["filled_at"] = hhmmss()\n                changed = True; filled += 1\n            await asyncio.sleep(0.2)\n        if changed:\n            try:\n                tmp = fp + ".tmp"\n                with open(tmp, "w") as f:\n                    json.dump(arr, f, default=str); f.flush(); os.fsync(f.fileno())\n                os.replace(tmp, fp)\n            except Exception as e:\n                print("reconcile write fail", e)\n    return filled\n\nasync def reconcile_watch(app):\n    """每 5 分鐘嘗試補帳一次；補到就通知。"""\n    while True:\n        await asyncio.sleep(300)\n        try:\n            n = await reconcile_pending(app)\n            if n and CHAT_ID:\n                await notify(app, CHAT_ID, f"{E.BOT} ✅ 補帳完成：已向 OKX 回填 {n} 筆待補紀錄")\n        except Exception as e:\n            print("reconcile_watch fail", e)\n\n'
SUMOLD = 'def sum_lines(rs, placed, entered):\n    L = []\n    m = len(rs)\n    hit = (entered / placed * 100) if placed else 0\n    amb = ("%d秒" % (sum(int(r.get("ambush_s") or 0) for r in rs) / m)) if m else "-"\n    L.append("次數:%d | %d(%s) | %.2f%%" % (placed, entered, amb, hit))\n'
SUMNEW = 'def sum_lines(rs_all, placed, entered):\n    L = []\n    pend = [r for r in rs_all if r.get("src") == "PENDING" or r.get("net") is None]\n    rs = [r for r in rs_all if r not in pend]\n    m = len(rs)\n    hit = (entered / placed * 100) if placed else 0\n    amb = ("%d秒" % (sum(int(r.get("ambush_s") or 0) for r in rs_all) / len(rs_all))) if rs_all else "-"\n    L.append("次數:%d | %d(%s) | %.2f%%" % (placed, entered, amb, hit))\n    if pend:\n        L.append("%s 待補帳:%d 筆（未計入損益）" % (E.LOSS, len(pend)))\n    if not m:\n        L.append("尚無已確認損益")\n        return L\n'

# 1) timedelta 已 import？確保有
if "from datetime import datetime, timezone, timedelta" not in s:
    fails.append("FAIL: 缺少 timedelta import")
else:
    print("OK: 0. timedelta 已可用")

# 2) 插入補帳輔助函式（放在 close_record 之前）
rep("async def close_record(iid, ps, after_ms, tries=3):",
    HELPERS + "async def close_record(iid, ps, after_ms, tries=3):",
    "1. 插入補帳函式")

# 3) do_exit 取值段改為零估算
rep(OLDPX, NEWPX, "2. do_exit 移除估算，改 PENDING")

# 4) 出場訊息來源標記
rep('        + ("" if src == "OKX" else f"⚠ 來源：{src}\\n")',
    '        + ("" if src == "OKX" else f"⚠ 來源：{src}\\n")',
    "3. 訊息來源標記（已存在）")

# 5) /summary 排除待補帳
rep(SUMOLD, SUMNEW, "4. summary 排除待補帳")

# 6) 啟動時掛上補帳背景任務
rep('    await startup_recover(app)',
    '    await startup_recover(app)\n    _rc = asyncio.create_task(reconcile_watch(app))\n    _BG.add(_rc); _rc.add_done_callback(_BG.discard)\n    print("補帳背景任務已啟動（每 5 分鐘）")',
    "5. 啟動補帳任務")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

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

OLD = '    L.append(f"掛單數：{len(pdl)}")\n    L.append(f"持倉數：{len(pl)}")\n    for p in pl:\n        d = "L" if p["posSide"] == "long" else "S"\n        L.append(f"{E.dir_emoji(d)} {p[\'instId\'].replace(\'-USDT-SWAP\',\'USDT\')} {d}")\n'
NEW = '    # 掛單依 clOrdId 前綴分類：n/x=原K、h/y=均K、其餘=孤兒\n    n_own = n_ha = n_orph = 0\n    for o in pdl:\n        cid = str(o.get("clOrdId") or "")\n        if cid[:1] in ("n", "x"): n_own += 1\n        elif cid[:1] in ("h", "y"): n_ha += 1\n        else: n_orph += 1\n    seg = [f"原K {n_own}"]\n    if n_ha: seg.append(f"均K {n_ha}")\n    if n_orph: seg.append(f"{E.LOSS} 孤兒 {n_orph}")\n    L.append(f"掛單數：{len(pdl)}（" + " / ".join(seg) + "）")\n    # 持倉無 clOrdId，只能比對原K 策略清單\n    own_keys = {(s["spec"]["iid"], "long" if s["dir"] == "L" else "short") for s in alive}\n    p_own = [p for p in pl if (p["instId"], p["posSide"]) in own_keys]\n    p_other = [p for p in pl if (p["instId"], p["posSide"]) not in own_keys]\n    ps = [f"原K {len(p_own)}"]\n    if p_other: ps.append(f"{E.LOSS} 非原K {len(p_other)}")\n    L.append(f"持倉數：{len(pl)}（" + " / ".join(ps) + "）")\n    for p in p_own:\n        d = "L" if p["posSide"] == "long" else "S"\n        L.append(f"{E.dir_emoji(d)} {p[\'instId\'].replace(\'-USDT-SWAP\',\'USDT\')} {d}")\n    for p in p_other:\n        d = "L" if p["posSide"] == "long" else "S"\n        L.append(f"{E.LOSS} {p[\'instId\'].replace(\'-USDT-SWAP\',\'USDT\')} {d}（非原K）")\n'
AOLD = '    ln = sum(Decimal(str(x.get("net") or "0")) for x in recs)\n    lf = sum(Decimal(str(x.get("fee") or "0")) for x in recs)\n    on = sum(Decimal(p.get("realizedPnl") or "0") for p in ph)\n    of = sum(Decimal(p.get("fee") or "0") + Decimal(p.get("fundingFee") or "0") for p in ph)\n    from collections import Counter\n    srcs = Counter(x.get("src") for x in recs)\n    L = [f"{E.BOT} OKX原K｜{ACCT}", f"📋 對帳 {t}", "━" * 10,\n         ("%s " % E.LOSS if len(recs) != len(ph) else "") + f"DB筆數：{len(recs)}｜OKX：{len(ph)}",\n         "來源分佈：" + "、".join(f"{k}×{v}" for k, v in srcs.items()),\n         "━" * 10,\n         f"DB 淨損益：{ln:+.6f}",\n         f"OKX 淨損益：{on:+.6f}",\n         f"差異：{ln - on:+.6f}",\n         "━" * 10,\n         f"DB 手續費：{lf:+.6f}",\n         f"OKX 手續費：{of:+.6f}",\n         f"差異：{lf - of:+.6f}",\n         "━" * 10]\n    if len(recs) == len(ph) and abs(ln - on) < Decimal("0.000001") and abs(lf - of) < Decimal("0.000001"):\n        L.append("✅ 完全一致")\n'
ANEW = '    # 只比對「原K 自己的」平倉：以幣種+方向+時間（±3分）與 DB 紀錄配對，\n    # 配不上的視為非原K（均K 或手動），單獨列出不計入差異。\n    used = set(); ph_own = []; ph_other = []\n    for p in ph:\n        sym_p = p["instId"].replace("-USDT-SWAP", "USDT")\n        d_p = "L" if p.get("posSide") == "long" else "S"\n        ut = int(p.get("uTime") or 0)\n        hit = None\n        for i, x in enumerate(recs):\n            if i in used or x.get("sym") != sym_p or x.get("dir") != d_p:\n                continue\n            tx = _rec_epoch_ms(x)\n            if tx and abs(tx - ut) <= 180000:\n                hit = i; break\n        if hit is None:\n            ph_other.append(p)\n        else:\n            used.add(hit); ph_own.append(p)\n    ln = sum(Decimal(str(x.get("net") or "0")) for x in recs)\n    lf = sum(Decimal(str(x.get("fee") or "0")) for x in recs)\n    on = sum(Decimal(p.get("realizedPnl") or "0") for p in ph_own)\n    of = sum(Decimal(p.get("fee") or "0") + Decimal(p.get("fundingFee") or "0") for p in ph_own)\n    from collections import Counter\n    srcs = Counter(x.get("src") for x in recs)\n    same = len(recs) == len(ph_own)\n    L = [f"{E.BOT} OKX原K｜{ACCT}", f"📋 對帳 {t}", "━" * 10,\n         ("%s " % E.LOSS if not same else "") + f"DB筆數：{len(recs)}｜OKX原K：{len(ph_own)}",\n         "來源分佈：" + "、".join(f"{k}×{v}" for k, v in srcs.items()),\n         "━" * 10,\n         f"DB 淨損益：{ln:+.6f}",\n         f"OKX 淨損益：{on:+.6f}",\n         f"差異：{ln - on:+.6f}",\n         "━" * 10,\n         f"DB 手續費：{lf:+.6f}",\n         f"OKX 手續費：{of:+.6f}",\n         f"差異：{lf - of:+.6f}",\n         "━" * 10]\n    if same and abs(ln - on) < Decimal("0.000001") and abs(lf - of) < Decimal("0.000001"):\n        L.append("✅ 完全一致")\n    if ph_other:\n        L.append(f"（另有 {len(ph_other)} 筆非原K 平倉，未計入）")\n        for p in ph_other[:5]:\n            sy = p["instId"].replace("-USDT-SWAP", "USDT")\n            dd = "L" if p.get("posSide") == "long" else "S"\n            tt = datetime.fromtimestamp(int(p.get("uTime") or 0) / 1000, TZ8).strftime("%H:%M:%S")\n            L.append(f"\u3000{tt} {sy} {dd} {Decimal(p.get(\'realizedPnl\') or \'0\'):+.6f}")\n'

rep(OLD, NEW, "1. /status 掛單分原K/均K/孤兒、持倉分原K/非原K")
rep(AOLD, ANEW, "2. /audit 只比對原K，非原K 另列")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

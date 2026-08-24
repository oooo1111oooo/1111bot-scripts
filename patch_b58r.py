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

HELPERS = (
    'def trade_file(t):\n'
    '    return STATE_FILE.replace("strategies_", "trades_").replace(".json", "_" + str(t).replace("-", "") + ".json")\n'
    'def load_trades(t):\n'
    '    try: return json.load(open(trade_file(t)))\n'
    '    except Exception: return []\n'
    'def sum_lines(rs, placed, entered):\n'
    '    L = []\n'
    '    m = len(rs)\n'
    '    hit = (entered / placed * 100) if placed else 0\n'
    '    L.append("\u59d4\u8a17\u6b21\u6578\uff1a%d" % placed)\n'
    '    L.append("\u9032\u5834\u6578\uff1a%d | \u547d\u4e2d\u7387\uff1a%.2f%%" % (entered, hit))\n'
    '    if m:\n'
    '        L.append("\u5e73\u5747\u57cb\u4f0f\u79d2\u6578\uff1a%d\u79d2" % (sum(int(r.get("ambush_s") or 0) for r in rs) / m))\n'
    '    else:\n'
    '        L.append("\u5e73\u5747\u57cb\u4f0f\u79d2\u6578\uff1a-")\n'
    '    NAME = {"Take_Profit": "TP", "Stop_Loss": "SL", "Time_Exit": "TE"}\n'
    '    for lab, cats in (("\u7372\u5229", ("Take_Profit", "Time_Exit")), ("\u8667\u640d", ("Stop_Loss", "Time_Exit"))):\n'
    '        if lab == "\u7372\u5229": sub = [r for r in rs if Decimal(str(r.get("net") or "0")) > 0]\n'
    '        else: sub = [r for r in rs if Decimal(str(r.get("net") or "0")) < 0]\n'
    '        L.append("\u2501" * 10)\n'
    '        L.append("%s\u6578\uff1a%d" % (lab, len(sub)))\n'
    '        ps = []; ss = []\n'
    '        for cn in cats:\n'
    '            gg = [r for r in sub if r.get("reason") == cn]\n'
    '            ps.append("%s:%d" % (NAME[cn], len(gg)))\n'
    '            if gg: ss.append("%s:%d\u79d2" % (NAME[cn], sum(int(r.get("hold_s") or 0) for r in gg) / len(gg)))\n'
    '            else: ss.append("%s:-" % NAME[cn])\n'
    '        L.append("\u3000" + " | ".join(ps))\n'
    '        L.append("\u3000\u5e73\u5747\u79d2\u6578 " + " | ".join(ss))\n'
    '    L.append("\u2501" * 10)\n'
    '    tg = sum((Decimal(str(r.get("gross") or "0")) for r in rs), Decimal(0))\n'
    '    tf = sum((Decimal(str(r.get("fee") or "0")) for r in rs), Decimal(0))\n'
    '    tn = sum((Decimal(str(r.get("net") or "0")) for r in rs), Decimal(0))\n'
    '    nv = sum((Decimal(str(r.get("nv") or "0")) for r in rs), Decimal(0))\n'
    '    gp = (tg / nv * 100) if nv else Decimal(0)\n'
    '    fp = (tf / nv * 100) if nv else Decimal(0)\n'
    '    npc = (tn / nv * 100) if nv else Decimal(0)\n'
    '    L.append("\u6bdb\u640d\u76ca\uff1a%+.6f (%+.3f%%)" % (tg, gp))\n'
    '    L.append("\u624b\u7e8c\u8cbb\uff1a%+.6f (%+.3f%%)" % (tf, fp))\n'
    '    L.append("\u6de8\u640d\u76ca\uff1a%+.6f (%+.3f%%) %s" % (tn, npc, E.pnl_emoji(tn)))\n'
    '    return L\n\n'
)
rep('def rebuild_strat(app,d):', HELPERS + 'def rebuild_strat(app,d):', "1. insert summary helpers")

rep('TRADE_FILE = STATE_FILE.replace("strategies_", "trades_")\n'
    'def log_trade(rec):\n'
    '    try:\n'
    '        try: arr = json.load(open(TRADE_FILE))\n'
    '        except Exception: arr = []\n'
    '        arr.append(rec)\n'
    '        with open(TRADE_FILE, "w") as f: json.dump(arr, f, default=str)\n'
    '    except Exception as e: print("log_trade fail", e)\n',
    'def log_trade(rec):\n'
    '    try:\n'
    '        fp_ = trade_file(rec.get("date"))\n'
    '        try: arr = json.load(open(fp_))\n'
    '        except Exception: arr = []\n'
    '        arr.append(rec)\n'
    '        with open(fp_, "w") as f: json.dump(arr, f, default=str)\n'
    '    except Exception as e: print("log_trade fail", e)\n',
    "2. log_trade writes per-day file")

NEWBODY = (
    'async def cmd_summary(u,c):\n'
    '    t=today8(); recs=load_trades(t)\n'
    '    ts={k:v for k,v in STATS.items() if str(v.get("date"))==str(t)}\n'
    '    tp_=sum(v["placed"] for v in ts.values()); te_=sum(v["entered"] for v in ts.values())\n'
    '    L=[f"{E.BOT} OKXLive\u666eK\uff5c{ACCT}",f"\u4e8b\u4ef6\uff1a\u7576\u65e5\u6230\u5831 {t}","\u2501"*10]\n'
    '    L+=sum_lines(recs,tp_,te_)\n'
    '    L+=["\u2501"*10,f"\u6642\u9593\uff1a{hhmmss()}"]\n'
    '    await u.message.reply_text("\\n".join(L))\n'
    '    syms=sorted({r["sym"] for r in recs})\n'
    '    for sy in syms:\n'
    '        D=[f"{E.BOT} OKXLive\u666eK\uff5c{ACCT}",f"\u9644\u8868\uff1a{sy} {t}"]\n'
    '        for dr in ("L","S"):\n'
    '            rows=[r for r in recs if r["sym"]==sy and r["dir"]==dr]\n'
    '            st_=ts.get(skey(sy,dr)) or {"placed":0,"entered":0}\n'
    '            D.append("\u2501"*10)\n'
    '            D.append(f"{E.dir_emoji(dr)} {E.dir_word(dr)}")\n'
    '            D+=sum_lines(rows,st_["placed"],st_["entered"])\n'
    '        D+=["\u2501"*10,f"\u6642\u9593\uff1a{hhmmss()}"]\n'
    '        await u.message.reply_text("\\n".join(D))\n'
    '    return\n'
)
rep('async def cmd_summary(u,c):\n', NEWBODY, "3. new summary implementation")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

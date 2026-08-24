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

OLD = (
    '    tp_=sum(v["placed"] for v in ts.values()); te_=sum(v["entered"] for v in ts.values())\n'
    '    L=[f"{E.BOT} OKXLive\u666eK\uff5c{ACCT}",f"\u4e8b\u4ef6\uff1a\u7576\u65e5\u6230\u5831 {t}","\u2501"*10]\n'
    '    L+=sum_lines(recs,tp_,te_)\n'
    '    L+=["\u2501"*10,f"\u6642\u9593\uff1a{hhmmss()}"]\n'
)
NEW = (
    '    L=[f"{E.BOT} OKXLive\u666eK\uff5c{ACCT}",'
    'f"\U0001f4ca\U0001f4ca\U0001f4ca Summary {t}"]\n'
    '    for dr in ("L","S"):\n'
    '        rows=[r for r in recs if r["dir"]==dr]\n'
    '        pa=sum(v["placed"] for k,v in ts.items() if k.endswith("_"+dr))\n'
    '        en=sum(v["entered"] for k,v in ts.items() if k.endswith("_"+dr))\n'
    '        L.append("\u2501"*10)\n'
    '        L.append(f"{E.dir_emoji(dr)} {E.dir_word(dr)}")\n'
    '        L+=sum_lines(rows,pa,en)\n'
    '    L+=["\u2501"*10,f"\u6642\u9593\uff1a{hhmmss()}"]\n'
)
rep(OLD, NEW, "1. summary totals split by L/S")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

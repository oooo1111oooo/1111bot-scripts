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

HELPER = 'def pct(v):\n    return str(Decimal(str(v)).normalize())\n\n'

OLD1 = 'def rebuild_strat(app,d):'
rep(OLD1, HELPER + OLD1, "1. insert pct helper")

OLD2 = (
    '                f"\u5546\u3000\u3000\u54c1\uff1a{E.dir_emoji(d)} {S[\'sym\']} {E.dir_word(d)}'
    '\\n\u9032\u5834\u50f9\u683c\uff1a{fpx}\\n"\n'
    '                f"\u6b62\u76c8 TP\uff1a{tp}\\n\u6b62\u640d SL\uff1a{sl}\\n'
)
NEW2 = (
    '                f"\u5546\u3000\u3000\u54c1\uff1a{E.dir_emoji(d)} {S[\'sym\']} {E.dir_word(d)}'
    '\\n\u9032\u5834\u50f9\u683c\uff1a{fpx} ({pct(S[\'offset\'])}%)\\n"\n'
    '                f"\u6b62\u76c8 TP\uff1a{tp} ({pct(S[\'tp\'])}%)\\n'
    '\u6b62\u640d SL\uff1a{sl} ({pct(S[\'sl\'])}%)\\n'
)
rep(OLD2, NEW2, "2. add pct to entry message")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL 2 PATCHES APPLIED ===")

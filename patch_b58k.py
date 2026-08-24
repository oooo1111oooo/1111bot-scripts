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

OLD1 = '            net=g+fee\n'
NEW1 = (
    '            net=g+fee\n'
    '            nv=fpx*size*spec["ctval"]\n'
    '            gp=(g/nv*100) if nv else Decimal(0)\n'
    '            fp=(fee/nv*100) if nv else Decimal(0)\n'
    '            np_=(net/nv*100) if nv else Decimal(0)\n'
)
rep(OLD1, NEW1, "1. compute notional percentages")

OLD2 = (
    '\u6bdb\u640d\u76ca\uff1a{g:+.6f} USDT\\n'
    '\u624b\u7e8c\u8cbb\uff1a{fee:+.6f} USDT\\n'
    '\u6de8\u640d\u76ca\uff1a{net:+.6f} USDT {E.pnl_emoji(net)}'
)
NEW2 = (
    '\u6bdb\u640d\u76ca\uff1a{g:+.6f} USDT ({gp:+.3f}%)\\n'
    '\u624b\u7e8c\u8cbb\uff1a{fee:+.6f} USDT ({fp:+.3f}%)\\n'
    '\u6de8\u640d\u76ca\uff1a{net:+.6f} USDT ({np_:+.3f}%) {E.pnl_emoji(net)}'
)
rep(OLD2, NEW2, "2. add percentages to exit message")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL 2 PATCHES APPLIED ===")

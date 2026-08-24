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

OLD1 = '            oid=r["data"][0]["ordId"]; S["state"]="\u59d4\u8a17\u4e2d"; S["ordId"]=oid; bump(k,"placed")\n'
NEW1 = OLD1 + '            pt=time.time()\n'
rep(OLD1, NEW1, "1. record place time")

OLD2 = '\u6301\u5009 TE\uff1a{S[\'te\']}s\\n\u72c0\u3000\u3000\u614b\uff1a'
NEW2 = ('\u6301\u5009 TE\uff1a{S[\'te\']}s\\n'
        '\u57cb\u4f0f\u79d2\u6578\uff1a{int(time.time()-pt)}s\\n'
        '\u72c0\u3000\u3000\u614b\uff1a')
rep(OLD2, NEW2, "2. add ambush seconds line")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL 2 PATCHES APPLIED ===")

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
    '            log_trade({"date":today8(),"sym":S["sym"],"dir":d,"reason":reason,'
    '"ambush_s":int(ee-pt),"hold_s":hs,"gross":str(g),"fee":str(fee),"net":str(net),'
    '"nv":str(nv),"src":src,"ts":hhmmss()})\n'
)
NEW = (
    '            log_trade({"date":today8(),"sym":S["sym"],"dir":d,"reason":reason,'
    '"ambush_s":round(ee-pt),"hold_s":hs,"gross":str(g),"fee":str(fee),"net":str(net),'
    '"nv":str(nv),"src":src,"ts":hhmmss(),'
    '"in_ts":time.strftime("%H:%M:%S",time.localtime(ee)),'
    '"tf":S["tf"],"te":str(S["te"])+"s",'
    '"in_px":str(fpx),"out_px":str(xpx)})\n'
)
rep(OLD, NEW, "1. log entry time / params / prices")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

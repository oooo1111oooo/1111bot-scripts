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

OLD1 = (
    '            S["state"]="\u7b49\u4e0b\u8f2a"; save_state(); tf_sec=TF_SEC[S["tf"]]\n'
    '            oe=next_open_epoch(int(time.time()),S["tf"]); w=oe-time.time()\n'
    '            if w>0: await asyncio.sleep(w)\n'
    '            if not S["alive"]: break\n'
)
NEW1 = (
    '            tf_sec=TF_SEC[S["tf"]]\n'
    '            now=time.time(); cur=int(now//tf_sec)*tf_sec\n'
    '            if S.get("catchup") and cur!=S.get("last_open") and (cur+tf_sec-now)>=30:\n'
    '                oe=cur\n'
    '            else:\n'
    '                S["state"]="\u7b49\u4e0b\u8f2a"; save_state()\n'
    '                oe=next_open_epoch(int(time.time()),S["tf"]); w=oe-time.time()\n'
    '                if w>0: await asyncio.sleep(w)\n'
    '                if not S["alive"]: break\n'
    '            S["last_open"]=oe; S["catchup"]=False\n'
)
rep(OLD1, NEW1, "1. catch-up scheduling")

OLD2 = (
    '                    S["alive"]=False; break\n'
    '                continue\n'
)
NEW2 = (
    '                    S["alive"]=False; break\n'
    '                S["catchup"]=True\n'
    '                continue\n'
)
rep(OLD2, NEW2, "2. flag catch-up after cancel")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL 2 PATCHES APPLIED ===")

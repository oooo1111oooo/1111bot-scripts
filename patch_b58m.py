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
    '    r=api("GET","/api/v5/account/positions-history?instType=SWAP&limit=100")\n'
    '    s8=now8().replace(hour=0,minute=0,second=0,microsecond=0); sms=int(s8.timestamp()*1000)\n'
    '    td=[p for p in r.get("data",[]) if int(p.get("uTime") or 0)>=sms] if r.get("code")=="0" else []\n'
)
NEW1 = (
    '    s8=now8().replace(hour=0,minute=0,second=0,microsecond=0); sms=int(s8.timestamp()*1000)\n'
    '    td=[]; after=""; pages=0\n'
    '    while pages<20:\n'
    '        q="/api/v5/account/positions-history?instType=SWAP&limit=100"\n'
    '        if after: q+="&after="+after\n'
    '        r=api("GET",q)\n'
    '        if r.get("code")!="0": break\n'
    '        batch=r.get("data") or []\n'
    '        if not batch: break\n'
    '        pages+=1\n'
    '        stop=False\n'
    '        for pp in batch:\n'
    '            ut=int(pp.get("uTime") or 0)\n'
    '            if ut>=sms: td.append(pp)\n'
    '            else: stop=True\n'
    '        if stop or len(batch)<100: break\n'
    '        after=batch[-1].get("posId") or ""\n'
    '        if not after: break\n'
)
rep(OLD1, NEW1, "1. paginate positions-history")

OLD2 = (
    '        L+=[f"\u5e73\u5009\u7b46\u6578\uff1a{n}",f"\u7372\u5229/\u8667\u640d/\u6253\u5e73\uff1a{win}/{loss}/{even}",'
    'f"\u52dd\u7387\uff1a{win/n*100:.1f}%",\n'
    '            f"\u6bdb\u640d\u76ca\uff1a{tp:+.6f}",f"\u624b\u7e8c\u8cbb\uff1a{tf:+.6f}",'
    'f"\u6de8\u640d\u76ca\uff1a{tn:+.6f} {E.pnl_emoji(tn)}"]\n'
)
NEW2 = (
    '        nv=Decimal(0)\n'
    '        for pp in td:\n'
    '            try: nv+=Decimal(pp.get("openAvgPx") or "0")*Decimal(pp.get("closeTotalPos") or "0")\n'
    '            except Exception: pass\n'
    '        gp=(tp/nv*100) if nv else Decimal(0)\n'
    '        fpc=(tf/nv*100) if nv else Decimal(0)\n'
    '        npc=(tn/nv*100) if nv else Decimal(0)\n'
    '        L+=[f"\u5e73\u5009\u7b46\u6578\uff1a{n}",f"\u7372\u5229/\u8667\u640d/\u6253\u5e73\uff1a{win}/{loss}/{even}",'
    'f"\u52dd\u7387\uff1a{win/n*100:.1f}%",\n'
    '            f"\u6bdb\u640d\u76ca\uff1a{tp:+.6f} ({gp:+.3f}%)",'
    'f"\u624b\u7e8c\u8cbb\uff1a{tf:+.6f} ({fpc:+.3f}%)",'
    'f"\u6de8\u640d\u76ca\uff1a{tn:+.6f} ({npc:+.3f}%) {E.pnl_emoji(tn)}"]\n'
)
rep(OLD2, NEW2, "2. add percentages to summary")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL 2 PATCHES APPLIED ===")

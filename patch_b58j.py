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
    '    for s in alive:\n'
    '        k=skey(s["sym"],s["dir"]); placed,entered=get_stat(k)\n'
    '        L.append(f"\u3000{E.dir_emoji(s[\'dir\'])} {s[\'sym\']} {E.dir_word(s[\'dir\'])}'
    '\uff1a{s[\'state\']}(\u639b{placed}/\u9032{entered})")\n'
)
NEW = (
    '    okx_pos={(p["instId"],p["posSide"]) for p in pl}\n'
    '    okx_ord={(o["instId"],o.get("posSide")) for o in pdl}\n'
    '    for s in alive:\n'
    '        k=skey(s["sym"],s["dir"]); placed,entered=get_stat(k)\n'
    '        key=(s["spec"]["iid"],"long" if s["dir"]=="L" else "short")\n'
    '        if key in okx_pos: live="\u6301\u5009\u4e2d"\n'
    '        elif key in okx_ord: live="\u59d4\u8a17\u4e2d"\n'
    '        else: live="\u7b49\u4e0b\u8f2a"\n'
    '        L.append(f"\u3000{E.dir_emoji(s[\'dir\'])} {s[\'sym\']} {E.dir_word(s[\'dir\'])}'
    '\uff1a{live}(\u639b{placed}/\u9032{entered})")\n'
)
rep(OLD, NEW, "1. status reads from OKX, not S[state]")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL 1 PATCH APPLIED ===")

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

OLD = '    app=Application.builder().token(TOKEN).post_init(_post_init).build()\n'
NEW = (
    '    app=(Application.builder().token(TOKEN).post_init(_post_init)\n'
    '         .connect_timeout(30.0).read_timeout(30.0).write_timeout(30.0)\n'
    '         .pool_timeout(30.0).get_updates_read_timeout(40.0)\n'
    '         .get_updates_connect_timeout(30.0).build())\n'
)
rep(OLD, NEW, "1. longer telegram timeouts")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

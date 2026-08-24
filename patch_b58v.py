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
    '        def enc(o): return str(o) if isinstance(o,Decimal) else o\n'
    '        with open(STATE_FILE,"w") as f: json.dump(data,f,default=enc)\n'
    '    except Exception as e: print("save_state fail",e)\n'
)
NEW1 = (
    '        def enc(o): return str(o) if isinstance(o,Decimal) else o\n'
    '        if not data["strats"] and STRATS:\n'
    '            return\n'
    '        tmp = STATE_FILE + ".tmp"\n'
    '        with open(tmp,"w") as f:\n'
    '            json.dump(data,f,default=enc); f.flush(); os.fsync(f.fileno())\n'
    '        os.replace(tmp, STATE_FILE)\n'
    '    except Exception as e: print("save_state fail",e)\n'
)
rep(OLD1, NEW1, "1. atomic save_state")

OLD2 = (
    'async def notify(app,chat,t):\n'
    '    try: await app.bot.send_message(chat,t)\n'
    '    except Exception as e: print("notify fail",e)\n'
)
NEW2 = (
    'async def notify(app,chat,t):\n'
    '    try:\n'
    '        await asyncio.wait_for(app.bot.send_message(chat,t), timeout=12)\n'
    '    except Exception as e: print("notify fail",e)\n'
)
rep(OLD2, NEW2, "2. notify timeout guard")

OLD3 = '    app=Application.builder().token(TOKEN).post_init(_post_init).build()\n'
NEW3 = (
    '    app=(Application.builder().token(TOKEN).post_init(_post_init)\n'
    '         .connect_timeout(8).read_timeout(15).write_timeout(15).pool_timeout(5)\n'
    '         .get_updates_read_timeout(30).build())\n'
)
rep(OLD3, NEW3, "3. telegram timeouts")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

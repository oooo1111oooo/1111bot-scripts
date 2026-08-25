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
    'async def notify(app,chat,t):\n'
    '    try: await app.bot.send_message(chat,t)\n'
    '    except Exception as e: print("notify fail",e)\n'
)
NEW = (
    '_NOTIFY_TASKS = set()\n'
    'async def _send_bg(app, chat, t):\n'
    '    try:\n'
    '        await app.bot.send_message(chat, t)\n'
    '    except Exception as e:\n'
    '        print("notify fail", type(e).__name__, e)\n'
    'async def notify(app,chat,t):\n'
    '    """\u5c04\u5f8c\u4e0d\u7406\uff1a\u4e0d\u7b49 Telegram\uff0c\u4ea4\u6613\u908f\u8f2f\u7acb\u523b\u7e7c\u7e8c\u3002"""\n'
    '    try:\n'
    '        tk = asyncio.create_task(_send_bg(app, chat, t))\n'
    '        _NOTIFY_TASKS.add(tk)\n'
    '        tk.add_done_callback(_NOTIFY_TASKS.discard)\n'
    '    except Exception as e:\n'
    '        print("notify schedule fail", e)\n'
)
rep(OLD, NEW, "1. notify fire-and-forget")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

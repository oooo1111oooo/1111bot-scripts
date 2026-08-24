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

HELPERS = (
    'class _FakeMsg:\n'
    '    def __init__(self, app, chat): self._a = app; self._c = chat\n'
    '    async def reply_text(self, t):\n'
    '        try: await self._a.bot.send_message(self._c, t)\n'
    '        except Exception as e: print("auto summary send fail", e)\n'
    'class _FakeUpd:\n'
    '    def __init__(self, app, chat): self.message = _FakeMsg(app, chat)\n'
    'async def job_summary(ctx):\n'
    '    if not CHAT_ID:\n'
    '        print("auto summary: no CHAT_ID"); return\n'
    '    try: await cmd_summary(_FakeUpd(ctx.application, CHAT_ID), ctx)\n'
    '    except Exception as e: print("auto summary fail", e)\n\n'
)
rep('def rebuild_strat(app,d):', HELPERS + 'def rebuild_strat(app,d):', "1. insert auto-summary job")

OLD2 = '    print("\u5de6\u4e0b Menu \u5df2\u66f4\u65b0"); await startup_recover(app)\n'
NEW2 = (
    '    print("\u5de6\u4e0b Menu \u5df2\u66f4\u65b0")\n'
    '    try:\n'
    '        jq = app.job_queue\n'
    '        if jq:\n'
    '            t2359 = datetime.strptime("23:59", "%H:%M").time().replace(tzinfo=TZ8)\n'
    '            jq.run_daily(job_summary, time=t2359, name="daily_summary")\n'
    '            print("\u5df2\u6392\u7a0b\uff1a\u6bcf\u65e5 23:59 \u81ea\u52d5 /summary")\n'
    '        else:\n'
    '            print("\u7121 job_queue\uff0c\u81ea\u52d5 /summary \u672a\u555f\u7528\uff08\u9700 pip install '
    '\\"python-telegram-bot[job-queue]\\"\uff09")\n'
    '    except Exception as e: print("schedule summary fail", e)\n'
    '    await startup_recover(app)\n'
)
rep(OLD2, NEW2, "2. schedule daily 23:59")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

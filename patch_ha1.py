# -*- coding: utf-8 -*-
import io, sys, os

p = os.environ.get("TARGET", "/srv/1111bot/run_ha.py")
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

rep('PENDING = {}; STRATS = {}; TASKS = {}; STATS = {}\nCHAT_ID = None\nHTTP = None',
    'PENDING = {}; STRATS = {}; TASKS = {}; STATS = {}\nCHAT_ID = None\nHTTP = None\nSHUTTING_DOWN = False',
    "1. SHUTTING_DOWN 旗標")

rep('''def save_state():
    try:
        data = {"chat": CHAT_ID, "tf": ACCOUNT_TF, "stats": STATS, "strats": []}
        for k, S in STRATS.items():
            if S.get("alive"):
                data["strats"].append({a: S[a] for a in SAVE_FIELDS if a in S})
        if STRATS and not data["strats"]:
            return''',
    '''def save_state():
    if SHUTTING_DOWN:
        return
    try:
        data = {"chat": CHAT_ID, "tf": ACCOUNT_TF, "stats": STATS, "strats": []}
        for k, S in STRATS.items():
            if S.get("alive"):
                data["strats"].append({a: S[a] for a in SAVE_FIELDS if a in S})''',
    "2. save_state 關閉跳過")

rep('''    finally:
        S["state"] = "已停止"; S["alive"] = False
        STRATS.pop(k, None); TASKS.pop(k, None); save_state()
        try:
            if await okx_pos(iid, pos):
                await notify(app, chat, f"{E.BOT} ⚠ {S['sym']} {E.dir_word(d)} 已停止但仍有持倉，請至 OKX 處理")
        except Exception:
            pass''',
    '''    finally:
        if SHUTTING_DOWN:
            S["state"] = "已停止"
        else:
            S["state"] = "已停止"; S["alive"] = False
            STRATS.pop(k, None); TASKS.pop(k, None); save_state()
            try:
                if await okx_pos(iid, pos):
                    await notify(app, chat, f"{E.BOT} ⚠ {S['sym']} {E.dir_word(d)} 已停止但仍有持倉，請至 OKX 處理")
            except Exception:
                pass''',
    "3. hloop finally 關閉保護")

rep('''    PENDING[u.effective_chat.id] = {"t": time.time(), "sym": sym, "dir": dr, "tf": ACCOUNT_TF,
        "lev": lev, "margin": margin, "pre": pre, "post": post, "exitn": exitn, "amp": amp, "spec": spec}''',
    '''    PENDING[u.effective_chat.id] = {"kind": "run", "t": time.time(), "sym": sym, "dir": dr, "tf": ACCOUNT_TF,
        "lev": lev, "margin": margin, "pre": pre, "post": post, "exitn": exitn, "amp": amp, "spec": spec}''',
    "4. /run 加 kind")

rep('''        del PENDING[chat]
        await notify(app, chat, f"{E.BOT} 參數逾時已取消，請重新 /run")''',
    '''        kd = p.get("kind", "run")
        del PENDING[chat]
        await notify(app, chat, f"{E.BOT} /{kd} 逾時未確認，已取消")''',
    "5. 逾時通知")

rep('''    if not p: await reply(u, f"{E.BOT} 沒有待確認的 /run"); return
    if time.time() - p["t"] > 60:
        del PENDING[u.effective_chat.id]; await reply(u, f"{E.BOT} 確認逾時"); return
    del PENDING[u.effective_chat.id]
    k = skey(p["sym"], p["dir"])''',
    '''    if not p: await reply(u, f"{E.BOT} 沒有待確認的指令"); return
    if time.time() - p["t"] > 60:
        del PENDING[u.effective_chat.id]; await reply(u, f"{E.BOT} 確認逾時"); return
    kind = p.get("kind", "run")
    if kind == "stop":
        del PENDING[u.effective_chat.id]
        await do_stop(u, p["key"]); return
    if kind == "stopall":
        del PENDING[u.effective_chat.id]
        await do_stopall(u); return
    del PENDING[u.effective_chat.id]
    k = skey(p["sym"], p["dir"])''',
    "6. cmd_confirm 分派")

rep('''    S = STRATS[tg[0]]; d = S["dir"]; iid = S["spec"]["iid"]
    ps = "long" if d == "L" else "short"
    p = await okx_pos(iid, ps)
    S["alive"] = False
    save_state()
    if p:
        await reply(u, f"{E.BOT} /stop（持倉中）\\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\\n"
                       f"⚠ 已進場不自動平倉\\n倉位 {p['pos']} 張 均價 {p.get('avgPx','?')} 浮 {p.get('upl','?')}\\n"
                       f"請至 OKX 手動平倉")
    else:
        await reply(u, f"{E.BOT} /stop\\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\\n已停止")''',
    '''    S = STRATS[tg[0]]
    PENDING[u.effective_chat.id] = {"kind": "stop", "t": time.time(), "key": tg[0]}
    await reply(u, f"{E.BOT} 將停止 {E.dir_emoji(S['dir'])} {S['sym']} {E.dir_word(S['dir'])}\\n"
                   f"60秒內 /confirm 確認")
    asyncio.create_task(_to(c.application, u.effective_chat.id, PENDING[u.effective_chat.id]["t"]))

async def do_stop(u, key):
    S = STRATS.get(key)
    if not S or not S.get("alive"):
        await reply(u, f"{E.BOT} 策略已不存在"); return
    d = S["dir"]; iid = S["spec"]["iid"]
    ps = "long" if d == "L" else "short"
    p = await okx_pos(iid, ps)
    S["alive"] = False
    n = await sweep_h(iid, ps)
    save_state()
    tail = f"\\n⚠ 持倉 {p['pos']} 張，請至 OKX 平倉" if p else ""
    await reply(u, f"{E.BOT} 已停止 {E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}｜殘單 {n}{tail}")''',
    "7. /stop 兩段式")

rep('''async def cmd_stopall(u, c):
    alive = [k for k, s in STRATS.items() if s.get("alive")]
    held = []; done = []''',
    '''async def cmd_stopall(u, c):
    alive = [k for k, s in STRATS.items() if s.get("alive")]
    if not alive:
        await reply(u, f"{E.BOT} 目前無運行中策略"); return
    PENDING[u.effective_chat.id] = {"kind": "stopall", "t": time.time()}
    await reply(u, f"{E.BOT} ⚠ 將停止全部 {len(alive)} 個均K 策略\\n60秒內 /confirm 確認")
    asyncio.create_task(_to(c.application, u.effective_chat.id, PENDING[u.effective_chat.id]["t"]))

async def do_stopall(u):
    alive = [k for k, s in STRATS.items() if s.get("alive")]
    held = []; done = []''',
    "8. /stopall 兩段式")

rep('''    m = f"{E.BOT} /stopall（均K）\\n━━━━━━━━━━\\n"
    if done: m += f"已停止策略（{len(done)}）：\\n" + "\\n".join("・" + x for x in done) + "\\n"
    if orphan: m += f"另清除均K 殘留掛單：{orphan} 筆\\n"
    if held: m += f"⚠ 持倉需手動平倉（{len(held)}）：\\n" + "\\n".join("・" + x for x in held) + "\\n"
    if not done and not orphan and not held: m += "目前無策略、無殘單\\n"
    m += "（普K 不受影響）\\n"
    await reply(u, m + f"時間：{hhmmss()}")''',
    '''    m = f"{E.BOT} 已停止 {len(done)} 個均K 策略｜清殘單 {orphan}"
    if held: m += f"\\n⚠ 持倉需手動平倉：" + "、".join(held)
    m += "\\n（普K 不受影響）"
    await reply(u, m)''',
    "9. stopall 訊息簡化")

rep('''def main():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)''',
    '''async def _post_stop(app):
    global SHUTTING_DOWN
    save_state()
    SHUTTING_DOWN = True
    print("關閉中：已保存狀態，停止後續寫檔")

def main():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)''',
    "10. _post_stop")

rep('    app = (Application.builder().token(TOKEN).post_init(_post_init)',
    '    app = (Application.builder().token(TOKEN).post_init(_post_init).post_stop(_post_stop)',
    "11. 註冊 post_stop")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL PATCHES APPLIED ===")

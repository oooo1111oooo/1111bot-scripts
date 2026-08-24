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

HELPERS = '''def sweep_orphans(iid, pos, keep=None):
    try:
        r = api("GET", "/api/v5/trade/orders-pending")
    except Exception:
        return 0
    if r.get("code") != "0":
        return 0
    n = 0
    for o in (r.get("data") or []):
        if o.get("instId") != iid or o.get("posSide") != pos:
            continue
        if not str(o.get("clOrdId") or "").startswith("n"):
            continue
        if keep and o.get("ordId") == keep:
            continue
        api("POST", "/api/v5/trade/cancel-order", {"instId": iid, "ordId": o["ordId"]})
        n += 1
    return n

async def safe_cancel(iid, oid, tries=5):
    for i in range(tries):
        api("POST", "/api/v5/trade/cancel-order", {"instId": iid, "ordId": oid})
        await asyncio.sleep(1)
        st = api("GET", "/api/v5/trade/order?instId=%s&ordId=%s" % (iid, oid))
        if st.get("code") == "0" and st.get("data"):
            s2 = st["data"][0].get("state")
            if s2 == "canceled":
                return "canceled"
            if s2 == "filled":
                return "filled"
    return "fail"

async def order_fee(iid, oid):
    if not oid:
        return Decimal(0)
    for i in range(8):
        st = api("GET", "/api/v5/trade/order?instId=%s&ordId=%s" % (iid, oid))
        if st.get("code") == "0" and st.get("data"):
            dd = st["data"][0]
            if dd.get("state") == "filled":
                return Decimal(dd.get("fee") or "0")
        await asyncio.sleep(1)
    return Decimal(0)

'''

OLD1 = '            side="buy" if d=="L" else "sell"; pos="long" if d=="L" else "short"\n'
NEW1 = OLD1 + '            sweep_orphans(iid,pos)\n'
rep(OLD1, NEW1, "1. sweep orphans before placing")

OLD2 = (
    '            if not S["alive"]:\n'
    '                api("POST","/api/v5/trade/cancel-order",{"instId":iid,"ordId":oid}); break\n'
    '            if not filled:\n'
    '                api("POST","/api/v5/trade/cancel-order",{"instId":iid,"ordId":oid}); continue\n'
)
NEW2 = (
    '            if not S["alive"]:\n'
    '                rc=await safe_cancel(iid,oid)\n'
    '                if rc!="canceled":\n'
    '                    await notify(app,chat,f"{E.BOT} {E.LOSS} {S[\'sym\']} {E.dir_word(d)} '
    '撤單未確認({rc})，請至 OKX 手動檢查")\n'
    '                break\n'
    '            if not filled:\n'
    '                rc=await safe_cancel(iid,oid)\n'
    '                if rc!="canceled":\n'
    '                    await notify(app,chat,f"{E.BOT} {E.LOSS} {S[\'sym\']} {E.dir_word(d)} '
    '撤單未確認({rc})，本策略已停止以免重複掛單，請至 OKX 手動檢查")\n'
    '                    S["alive"]=False; break\n'
    '                continue\n'
)
rep(OLD2, NEW2, "2. verified cancel")

OLD3 = (
    '            api("POST","/api/v5/trade/order",{"instId":iid,"tdMode":"isolated","side":cs,"posSide":pos,\n'
    '                "ordType":"market","sz":str(size),"clOrdId":"x"+uuid.uuid4().hex[:14]})\n'
    '            xpx=get_last(iid)\n'
)
NEW3 = (
    '            xr=api("POST","/api/v5/trade/order",{"instId":iid,"tdMode":"isolated","side":cs,"posSide":pos,\n'
    '                "ordType":"market","sz":str(size),"clOrdId":"x"+uuid.uuid4().hex[:14]})\n'
    '            xoid=(xr.get("data") or [{}])[0].get("ordId")\n'
    '            xpx=get_last(iid)\n'
)
rep(OLD3, NEW3, "3. capture exit ordId")

OLD4 = (
    '            await notify(app,chat,f"{E.BOT} OKXLive\u666eK\uff5c{ACCT}\\n\u4e8b\u4ef6\uff1a'
    '{\'\U0001f7e2\' if g>=0 else \'\U0001f534\'} \u5df2\u51fa\u5834\\n"\n'
)
NEW4 = (
    '            fe=await order_fee(iid,oid); fx=await order_fee(iid,xoid); fee=fe+fx\n'
    '            net=g+fee\n'
    '            await notify(app,chat,f"{E.BOT} OKXLive\u666eK\uff5c{ACCT}\\n\u4e8b\u4ef6\uff1a'
    '{\'\U0001f7e2\' if net>=0 else \'\U0001f534\'} \u5df2\u51fa\u5834\\n"\n'
)
rep(OLD4, NEW4, "4. exit header uses net")

OLD5 = '\u6bdb\u640d\u76ca\uff1a{g:+.6f} USDT {E.pnl_emoji(g)}\\n\u6642\u9593\uff1a{hhmmss()}")'
NEW5 = ('\u6bdb\u640d\u76ca\uff1a{g:+.6f} USDT\\n'
        '\u624b\u7e8c\u8cbb\uff1a{fee:+.6f} USDT\\n'
        '\u6de8\u640d\u76ca\uff1a{net:+.6f} USDT {E.pnl_emoji(net)}\\n\u6642\u9593\uff1a{hhmmss()}")')
rep(OLD5, NEW5, "5. add fee + net lines")

OLD6 = 'def rebuild_strat(app,d):'
rep(OLD6, HELPERS + OLD6, "6. insert helper functions")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL 6 PATCHES APPLIED ===")

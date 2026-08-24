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
    'TRADE_FILE = STATE_FILE.replace("strategies_", "trades_")\n'
    'def log_trade(rec):\n'
    '    try:\n'
    '        try: arr = json.load(open(TRADE_FILE))\n'
    '        except Exception: arr = []\n'
    '        arr.append(rec)\n'
    '        with open(TRADE_FILE, "w") as f: json.dump(arr, f, default=str)\n'
    '    except Exception as e: print("log_trade fail", e)\n'
    'def okx_pos(iid, ps):\n'
    '    try: r = api("GET", "/api/v5/account/positions")\n'
    '    except Exception: return None\n'
    '    if r.get("code") != "0": return None\n'
    '    for pp in (r.get("data") or []):\n'
    '        if pp.get("instId") == iid and pp.get("posSide") == ps:\n'
    '            try:\n'
    '                if float(pp.get("pos") or 0) != 0: return pp\n'
    '            except Exception: pass\n'
    '    return None\n'
    'async def okx_close_rec(iid, ps, after_ms, tries=10):\n'
    '    for i in range(tries):\n'
    '        r = api("GET", "/api/v5/account/positions-history?instType=SWAP&instId=%s&limit=10" % iid)\n'
    '        if r.get("code") == "0":\n'
    '            for pp in (r.get("data") or []):\n'
    '                if pp.get("posSide") == ps and int(pp.get("uTime") or 0) >= after_ms:\n'
    '                    return pp\n'
    '        await asyncio.sleep(1)\n'
    '    return None\n\n'
)
rep('def rebuild_strat(app,d):', HELPERS + 'def rebuild_strat(app,d):', "1. insert OKX helpers")

OLD2 = (
    '            ee=time.time(); reason=None\n'
    '            while S["alive"]:\n'
    '                await asyncio.sleep(2); last=get_last(iid); held=time.time()-ee\n'
)
NEW2 = (
    '            ee=time.time(); reason=None; chk=0\n'
    '            while S["alive"]:\n'
    '                await asyncio.sleep(2); last=get_last(iid); held=time.time()-ee\n'
    '                chk+=1\n'
    '                if chk%8==0 and not okx_pos(iid,pos): reason="__gone__"; break\n'
)
rep(OLD2, NEW2, "2. reconcile position with OKX")

OLD3 = (
    '            cs="sell" if d=="L" else "buy"\n'
    '            xr=api("POST","/api/v5/trade/order",{"instId":iid,"tdMode":"isolated","side":cs,"posSide":pos,\n'
    '                "ordType":"market","sz":str(size),"clOrdId":"x"+uuid.uuid4().hex[:14]})\n'
    '            xoid=(xr.get("data") or [{}])[0].get("ordId")\n'
    '            xpx=get_last(iid)\n'
    '            g=(xpx-fpx)*size*spec["ctval"] if d=="L" else (fpx-xpx)*size*spec["ctval"]\n'
    '            fe=await order_fee(iid,oid); fx=await order_fee(iid,xoid); fee=fe+fx\n'
    '            net=g+fee\n'
    '            nv=fpx*size*spec["ctval"]\n'
)
NEW3 = (
    '            if reason=="__gone__":\n'
    '                await notify(app,chat,f"{E.BOT} {S[\'sym\']} {E.dir_word(d)} OKX \u5df2\u7121\u6301\u5009'
    '\uff08\u53ef\u80fd\u5df2\u624b\u52d5\u5e73\u5009\uff09\uff0c\u672c\u8f2a\u7d50\u675f")\n'
    '                continue\n'
    '            cs="sell" if d=="L" else "buy"\n'
    '            t0=int(time.time()*1000)\n'
    '            xr=api("POST","/api/v5/trade/order",{"instId":iid,"tdMode":"isolated","side":cs,"posSide":pos,\n'
    '                "ordType":"market","sz":str(size),"clOrdId":"x"+uuid.uuid4().hex[:14]})\n'
    '            if xr.get("code")!="0":\n'
    '                em=(xr.get("data") or [{}])[0].get("sMsg") or xr.get("msg")\n'
    '                await notify(app,chat,f"{E.BOT} {E.LOSS} {S[\'sym\']} {E.dir_word(d)} '
    '\u5e73\u5009\u5931\u6557\uff1a{em}\\n\u26a0 \u5009\u4f4d\u53ef\u80fd\u4ecd\u5728\uff0c\u8acb\u81f3 OKX \u624b\u52d5\u8655\u7406")\n'
    '                S["alive"]=False; break\n'
    '            xoid=(xr.get("data") or [{}])[0].get("ordId")\n'
    '            ph=await okx_close_rec(iid,pos,t0)\n'
    '            src="OKX"\n'
    '            if ph:\n'
    '                g=Decimal(ph.get("pnl") or "0")\n'
    '                fee=Decimal(ph.get("fee") or "0")+Decimal(ph.get("fundingFee") or "0")\n'
    '                net=Decimal(ph.get("realizedPnl") or "0")\n'
    '                xpx=Decimal(ph.get("closeAvgPx") or "0") or get_last(iid)\n'
    '                nv=Decimal(ph.get("openAvgPx") or fpx)*Decimal(ph.get("closeTotalPos") or size)*spec["ctval"]\n'
    '            else:\n'
    '                src="\u4f30\u7b97"\n'
    '                xpx=get_last(iid)\n'
    '                g=(xpx-fpx)*size*spec["ctval"] if d=="L" else (fpx-xpx)*size*spec["ctval"]\n'
    '                fe=await order_fee(iid,oid); fx=await order_fee(iid,xoid); fee=fe+fx\n'
    '                net=g+fee\n'
    '                nv=fpx*size*spec["ctval"]\n'
)
rep(OLD3, NEW3, "3. verified close + real OKX pnl")

OLD4 = (
    '            await notify(app,chat,f"{E.BOT} OKXLive\u666eK\uff5c{ACCT}\\n\u4e8b\u4ef6\uff1a'
    '{\'\U0001f7e2\' if net>=0 else \'\U0001f534\'} \u5df2\u51fa\u5834\\n"\n'
)
NEW4 = (
    '            hs=int(time.time()-ee)\n'
    '            log_trade({"date":today8(),"sym":S["sym"],"dir":d,"reason":reason,'
    '"ambush_s":int(ee-pt),"hold_s":hs,"gross":str(g),"fee":str(fee),"net":str(net),'
    '"nv":str(nv),"src":src,"ts":hhmmss()})\n'
    '            await notify(app,chat,f"{E.BOT} OKXLive\u666eK\uff5c{ACCT}\\n\u4e8b\u4ef6\uff1a'
    '{\'\U0001f7e2\' if net>=0 else \'\U0001f534\'} \u5df2\u51fa\u5834\\n"\n'
)
rep(OLD4, NEW4, "4. log trade record")

OLD5 = (
    '\u6bdb\u640d\u76ca\uff1a{g:+.6f} USDT ({gp:+.3f}%)\\n'
    '\u624b\u7e8c\u8cbb\uff1a{fee:+.6f} USDT ({fp:+.3f}%)\\n'
    '\u6de8\u640d\u76ca\uff1a{net:+.6f} USDT ({np_:+.3f}%)'
)
NEW5 = (
    '\u6bdb\u640d\u76ca\uff1a{g:+.6f} ({gp:+.3f}%)\\n'
    '\u624b\u7e8c\u8cbb\uff1a{fee:+.6f} ({fp:+.3f}%)\\n'
    '\u6de8\u640d\u76ca\uff1a{net:+.6f} ({np_:+.3f}%)'
)
rep(OLD5, NEW5, "5. drop USDT wording")

OLD6 = (
    '    S=STRATS[tg[0]]; st=S["state"]; d=S["dir"]\n'
    '    if st=="\u6301\u5009\u4e2d":\n'
)
NEW6 = (
    '    S=STRATS[tg[0]]; st=S["state"]; d=S["dir"]\n'
    '    if okx_pos(S["spec"]["iid"],"long" if d=="L" else "short"):\n'
)
rep(OLD6, NEW6, "6. /stop checks OKX position")

OLD7 = '        if S["state"]=="\u6301\u5009\u4e2d": S["alive"]=False; held.append(f"{S[\'sym\']} {S[\'dir\']}")\n'
NEW7 = ('        if okx_pos(S["spec"]["iid"],"long" if S["dir"]=="L" else "short"): '
        'S["alive"]=False; held.append(f"{S[\'sym\']} {S[\'dir\']}")\n')
rep(OLD7, NEW7, "7. /stopall checks OKX position")

if fails:
    print("\n".join(fails))
    print("=== ABORTED, file NOT modified ===")
    sys.exit(1)

io.open(p, "w", encoding="utf-8").write(s)
print("=== ALL 7 PATCHES APPLIED ===")

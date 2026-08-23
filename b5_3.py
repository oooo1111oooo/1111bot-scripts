#!/usr/bin/env python3
"""B5-5 普K：/TF可變、/run去掉TF參數、減噪(不通知掛單/撤單)、等下輪(N)輪次、/summary當日戰報。"""
import sys, hmac, base64, hashlib, json, time, asyncio, uuid
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN
from datetime import datetime, timezone, timedelta
import httpx
from telegram import BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters
sys.path.insert(0,"/srv/1111bot")
from app.core import emoji as E
from app.strategy.normal import next_open_epoch, TF_SEC
BASE="https://www.okx.com"; ACCT="o3333o"; TZ8=timezone(timedelta(hours=8))
ACCOUNT_TF="5m"   # 帳戶預設週期，可用 /timeframe 變更
def load_env(p):
    d={}
    for line in open(p):
        line=line.strip()
        if "=" in line and not line.startswith("#"):
            k,v=line.split("=",1); d[k]=v
    return d
ACC=load_env("/srv/1111bot/config/accounts.env"); BOTS=load_env("/srv/1111bot/config/bots.env")
TOKEN=BOTS["BOT_o3333o_NORMAL"]; SYMS=json.load(open("/srv/1111bot/config/symbols.json"))["symbols"]
PENDING={}; STRATS={}; TASKS={}
def skey(s,d): return f"{s}_{d}"
def inst_id(s): return s.replace("USDT","")+"-USDT-SWAP"
def now8(): return datetime.now(TZ8)
def hhmmss(): return now8().strftime("%H:%M:%S")
def ts_now():
    n=datetime.now(timezone.utc); return n.strftime("%Y-%m-%dT%H:%M:%S.")+f"{n.microsecond//1000:03d}Z"
def sign(sec,ts,m,p,b=""):
    return base64.b64encode(hmac.new(sec.encode(),f"{ts}{m}{p}{b}".encode(),hashlib.sha256).digest()).decode()
def api(method,path,body=None):
    b=json.dumps(body) if body else ""; ts=ts_now()
    h={"OK-ACCESS-KEY":ACC[f"OKX_{ACCT}_API_KEY"],"OK-ACCESS-SIGN":sign(ACC[f"OKX_{ACCT}_SECRET"],ts,method,path,b),
       "OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":ACC[f"OKX_{ACCT}_PASSPHRASE"],"Content-Type":"application/json"}
    return httpx.request(method,BASE+path,headers=h,content=b,timeout=15).json()
def pub(path): return httpx.get(BASE+path,timeout=15).json()
def get_spec(s):
    iid=inst_id(s); d=pub(f"/api/v5/public/instruments?instType=SWAP&instId={iid}")["data"][0]
    return {"iid":iid,"tick":Decimal(d["tickSz"]),"lot":Decimal(d["lotSz"]),"minsz":Decimal(d["minSz"]),
            "ctval":Decimal(d["ctVal"]),"maxlev":Decimal(d["lever"]),"ctvalccy":d["ctValCcy"]}
def get_last(iid): return Decimal(pub(f"/api/v5/market/ticker?instId={iid}")["data"][0]["last"])
def align(px,tick,d):
    return (px/tick).to_integral_value(rounding=ROUND_FLOOR if d=="L" else ROUND_CEILING)*tick
def csize(m,lev,px,cv,lot): return ((m*lev/px)/cv/lot).to_integral_value(rounding=ROUND_DOWN)*lot
async def notify(app,chat,t):
    try: await app.bot.send_message(chat,t)
    except Exception as e: print("notify fail",e)

async def loop(app,chat,S):
    spec=S["spec"]; iid=spec["iid"]; d=S["dir"]; k=skey(S["sym"],d)
    await notify(app,chat,f"{E.BOT} OKXLive普K｜{ACCT}\n事件：🟢 策略啟動\n"
        f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)} {S['lev']}x\n週　　期：{S['tf']}\n"
        f"狀　　態：等下輪開盤\n時間：{hhmmss()}")
    try:
        while S["alive"]:
            S["state"]="等下輪"; tf_sec=TF_SEC[S["tf"]]
            oe=next_open_epoch(int(time.time()),S["tf"]); w=oe-time.time()
            if w>0: await asyncio.sleep(w)
            if not S["alive"]: break
            op=get_last(iid)
            amb=align(op*(1-S["offset"]/100) if d=="L" else op*(1+S["offset"]/100),spec["tick"],d)
            size=csize(S["margin"],Decimal(S["lev"]),amb,spec["ctval"],spec["lot"])
            if size<spec["minsz"]:
                await notify(app,chat,f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 保證金不足，循環停止"); break
            side="buy" if d=="L" else "sell"; pos="long" if d=="L" else "short"
            r=api("POST","/api/v5/trade/order",{"instId":iid,"tdMode":"isolated","side":side,"posSide":pos,
                "ordType":"limit","px":str(amb),"sz":str(size),"clOrdId":"n"+uuid.uuid4().hex[:14]})
            if r.get("code")!="0":
                await notify(app,chat,f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 掛單失敗：{r.get('data',[{}])[0].get('sMsg',r.get('msg'))}")
                await asyncio.sleep(5); continue
            oid=r["data"][0]["ordId"]; S["state"]="委託中"; S["ordId"]=oid  # 不通知(減噪)
            nb=oe+tf_sec; filled=False; fpx=None
            while S["alive"] and time.time()<nb-3:
                await asyncio.sleep(2)
                st=api("GET",f"/api/v5/trade/order?instId={iid}&ordId={oid}")
                if st.get("code")=="0" and st["data"]:
                    s2=st["data"][0]["state"]
                    if s2=="filled": filled=True; fpx=Decimal(st["data"][0]["avgPx"]); break
                    if s2=="canceled": break
            if not S["alive"]:
                api("POST","/api/v5/trade/cancel-order",{"instId":iid,"ordId":oid}); break
            if not filled:
                api("POST","/api/v5/trade/cancel-order",{"instId":iid,"ordId":oid})
                S["round"]+=1  # 輪空+1，不通知(減噪)
                continue
            S["round"]=0; S["state"]="持倉中"; S["entry_px"]=fpx
            if d=="L":
                tp=align(fpx*(1+S["tp"]/100),spec["tick"],"S"); sl=align(fpx*(1-S["sl"]/100),spec["tick"],"L")
            else:
                tp=align(fpx*(1-S["tp"]/100),spec["tick"],"L"); sl=align(fpx*(1+S["sl"]/100),spec["tick"],"S")
            await notify(app,chat,f"{E.BOT} OKXLive普K｜{ACCT}\n事件：🔔 已進場成交\n"
                f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n進場價格：{fpx}\n"
                f"止盈 TP：{tp}\n止損 SL：{sl}\n持倉 TE：{S['te']}s\n狀　　態：📌 持倉中\n時間：{hhmmss()}")
            ee=time.time(); reason=None
            while S["alive"]:
                await asyncio.sleep(2); last=get_last(iid); held=time.time()-ee
                if d=="L":
                    if last>=tp: reason="Take_Profit"; break
                    if last<=sl: reason="Stop_Loss"; break
                else:
                    if last<=tp: reason="Take_Profit"; break
                    if last>=sl: reason="Stop_Loss"; break
                if held>=S["te"]: reason="Time_Exit"; break
            if not S["alive"]:
                await notify(app,chat,f"{E.BOT} {S['sym']} {E.dir_word(d)} 循環停止但仍有持倉，請至 OKX 確認"); break
            cs="sell" if d=="L" else "buy"
            api("POST","/api/v5/trade/order",{"instId":iid,"tdMode":"isolated","side":cs,"posSide":pos,
                "ordType":"market","sz":str(size),"clOrdId":"x"+uuid.uuid4().hex[:14]})
            xpx=get_last(iid)
            g=(xpx-fpx)*size*spec["ctval"] if d=="L" else (fpx-xpx)*size*spec["ctval"]
            await notify(app,chat,f"{E.BOT} OKXLive普K｜{ACCT}\n事件：{'🟢' if g>=0 else '🔴'} 已出場\n"
                f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n出場原因：{reason}\n進場價：{fpx}\n出場價：{xpx}\n"
                f"持倉秒數：{int(time.time()-ee)}s\n毛損益：{g:+.6f} USDT {E.pnl_emoji(g)}\n時間：{hhmmss()}")
    except Exception as e:
        await notify(app,chat,f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 循環錯誤：{type(e).__name__}: {e}")
    finally:
        S["state"]="已停止"; S["alive"]=False; STRATS.pop(k,None); TASKS.pop(k,None)

async def cmd_run(u,c):
    a=c.args
    fmt=f"用法：/run 商品 方向 槓桿 保證金 埋伏 TP SL TE\n例：/run ETHUSDT L 1x 3 0.5 0.5 0.5 180\n（週期依 /timeframe，目前 {ACCOUNT_TF}）"
    if len(a)!=8: await u.message.reply_text(f"{E.BOT} 參數數量錯誤（需8個，週期已移除）\n{fmt}"); return
    try:
        sym=a[0].upper();dr=a[1].upper();lev=int(a[2].replace("x",""));margin=Decimal(a[3])
        offset=Decimal(a[4]);tp=Decimal(a[5]);sl=Decimal(a[6]);te=int(a[7])
    except: await u.message.reply_text(f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if dr not in("L","S"): await u.message.reply_text(f"{E.BOT} 方向須 L 或 S"); return
    if not 1<=te<=900: await u.message.reply_text(f"{E.BOT} TE 須 1~900 秒"); return
    tf=ACCOUNT_TF; k=skey(sym,dr)
    if k in STRATS and STRATS[k].get("alive"):
        await u.message.reply_text(f"{E.BOT} {sym} {E.dir_word(dr)} 已在運行，同幣同向不可重複"); return
    try: spec=get_spec(sym)
    except: await u.message.reply_text(f"{E.LOSS} 找不到商品 {sym}"); return
    op=get_last(spec["iid"])
    amb=align(op*(1-offset/100) if dr=="L" else op*(1+offset/100),spec["tick"],dr)
    size=csize(margin,Decimal(lev),amb,spec["ctval"],spec["lot"])
    if size<spec["minsz"]: await u.message.reply_text(f"{E.BOT} {E.LOSS} 保證金不足，算出 {size} 張 < 最小 {spec['minsz']}"); return
    PENDING[u.effective_chat.id]={"t":time.time(),"sym":sym,"dir":dr,"tf":tf,"lev":lev,"margin":margin,
        "offset":offset,"tp":tp,"sl":sl,"te":te,"spec":spec}
    await u.message.reply_text(f"{E.BOT} OKXLive普K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
        f"商　　品：{E.dir_emoji(dr)} {sym} {E.dir_word(dr)} {lev}x\n週　　期：{tf}（帳戶設定）\n"
        f"進場模式：直接埋伏 Maker\n開盤估價：{op}\n埋伏距離：{offset}%\n埋伏價格：{amb}\n"
        f"止盈 TP：{tp}%\n止損 SL：{sl}%\n持倉 TE：{te}s\n保 證 金：{margin} USDT\n下單張數：{size}\n"
        f"━━━━━━━━━━\n⚠ 確認後真實循環交易\n下一步：60秒內 /confirm\n時間：{hhmmss()}")
    asyncio.create_task(_to(c.application,u.effective_chat.id,PENDING[u.effective_chat.id]["t"]))
async def _to(app,chat,stamp):
    await asyncio.sleep(61); p=PENDING.get(chat)
    if p and p["t"]==stamp: del PENDING[chat]; await notify(app,chat,f"{E.BOT} 參數逾時已取消，請重新 /run")
async def cmd_confirm(u,c):
    p=PENDING.get(u.effective_chat.id)
    if not p: await u.message.reply_text(f"{E.BOT} 沒有待確認的 /run"); return
    if time.time()-p["t"]>60: del PENDING[u.effective_chat.id]; await u.message.reply_text(f"{E.BOT} 確認逾時"); return
    del PENDING[u.effective_chat.id]; k=skey(p["sym"],p["dir"])
    S={**p,"alive":True,"state":"等下輪","round":0,"chat":u.effective_chat.id}
    STRATS[k]=S; TASKS[k]=asyncio.create_task(loop(c.application,u.effective_chat.id,S))
    cnt=sum(1 for s in STRATS.values() if s.get("alive"))
    await u.message.reply_text(f"{E.BOT} ✅ 已確認，{p['sym']} {E.dir_word(p['dir'])} 啟動\n運行中策略：{cnt} 個")
async def cmd_stop(u,c):
    a=c.args; alive=[k for k,s in STRATS.items() if s.get("alive")]
    if not alive: await u.message.reply_text(f"{E.BOT} 目前無運行中策略"); return
    if not a:
        lst="\n".join(f"・/stop {STRATS[k]['sym']} {STRATS[k]['dir']}（{STRATS[k]['state']}）" for k in alive)
        await u.message.reply_text(f"{E.BOT} 請指定：\n{lst}\n或 /stopall"); return
    sym=a[0].upper()
    tg=[skey(sym,a[1].upper())] if len(a)>=2 and skey(sym,a[1].upper()) in alive else [k for k in alive if STRATS[k]["sym"]==sym]
    if not tg: await u.message.reply_text(f"{E.BOT} 找不到運行中的 {sym}"); return
    if len(tg)>1: await u.message.reply_text(f"{E.BOT} {sym} 有多方向，請指定 /stop {sym} L 或 /stop {sym} S"); return
    S=STRATS[tg[0]]; st=S["state"]; d=S["dir"]
    if st=="持倉中":
        S["alive"]=False; pi=""
        pr=api("GET","/api/v5/account/positions")
        if pr.get("code")=="0":
            for pp in pr["data"]:
                if pp["instId"]==S["spec"]["iid"] and pp.get("posSide")==("long" if d=="L" else "short") and float(pp.get("pos","0"))!=0:
                    pi=f"倉位 {pp['pos']} 張 均價 {pp.get('avgPx','?')} 浮 {pp.get('upl','?')}"
        await u.message.reply_text(f"{E.BOT} /stop（持倉中）\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n⚠ 已進場不自動平倉\n{pi}\n請至 OKX 手動平倉")
    else:
        if S.get("ordId") and st=="委託中": api("POST","/api/v5/trade/cancel-order",{"instId":S["spec"]["iid"],"ordId":S["ordId"]})
        S["alive"]=False
        await u.message.reply_text(f"{E.BOT} /stop\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n原狀態：{st}\n動作：{'已撤銷委託' if st=='委託中' else '已移除'}")
async def cmd_stopall(u,c):
    alive=[k for k,s in STRATS.items() if s.get("alive")]
    if not alive: await u.message.reply_text(f"{E.BOT} 目前無運行中策略"); return
    held=[];done=[]
    for k in list(alive):
        S=STRATS[k]
        if S["state"]=="持倉中": S["alive"]=False; held.append(f"{S['sym']} {S['dir']}")
        else:
            if S.get("ordId") and S["state"]=="委託中": api("POST","/api/v5/trade/cancel-order",{"instId":S["spec"]["iid"],"ordId":S["ordId"]})
            S["alive"]=False; done.append(f"{S['sym']} {S['dir']}")
    m=f"{E.BOT} /stopall\n━━━━━━━━━━\n"
    if done: m+=f"已停止（{len(done)}）：\n"+"\n".join("・"+x for x in done)+"\n"
    if held: m+=f"⚠ 持倉需手動平倉（{len(held)}）：\n"+"\n".join("・"+x for x in held)+"\n請至 OKX 平倉\n"
    await u.message.reply_text(m+f"時間：{hhmmss()}")
async def cmd_status(u,c):
    bal=api("GET","/api/v5/account/balance");posr=api("GET","/api/v5/account/positions");pe=api("GET","/api/v5/trade/orders-pending")
    eq=av="?"
    if bal.get("code")=="0":
        x=next((d for d in bal["data"][0].get("details",[]) if d["ccy"]=="USDT"),None)
        if x: eq=f"{Decimal(x.get('eq','0')):.4f}"; av=f"{Decimal(x.get('availEq') or x.get('availBal') or '0'):.4f}"
    pl=[p for p in posr.get("data",[]) if float(p.get("pos","0"))!=0] if posr.get("code")=="0" else []
    pdl=pe.get("data",[]) if pe.get("code")=="0" else []
    alive=[s for s in STRATS.values() if s.get("alive")]
    L=[f"{E.BOT} OKXLive普K｜{ACCT}","事件：現況（即時查 OKX）","━━━━━━━━━━",
       f"USDT權益：{eq}",f"可用餘額：{av}",f"帳戶週期：{ACCOUNT_TF}",f"運行中策略：{len(alive)} 個"]
    for s in alive:
        st=f"等下輪({s['round']})" if s["state"]=="等下輪" else s["state"]
        L.append(f"　{E.dir_emoji(s['dir'])} {s['sym']} {E.dir_word(s['dir'])}：{st}")
    L+=[f"持倉數：{len(pl)}",f"掛單數：{len(pdl)}"]
    for p in pl:
        s=E.LONG if p["posSide"]=="long" else E.SHORT; upl=Decimal(p.get('upl','0'))
        L.append(f"{s} {p['instId']} 張{p['pos']} 浮{upl:+.4f}{E.pnl_emoji(upl)}")
    L+=["━━━━━━━━━━",f"時間：{hhmmss()} UTC+8"]
    await u.message.reply_text("\n".join(L))
async def cmd_summary(u,c):
    r=api("GET","/api/v5/account/positions-history?instType=SWAP&limit=100")
    if r.get("code")!="0": await u.message.reply_text(f"{E.LOSS} 查詢失敗"); return
    s8=now8().replace(hour=0,minute=0,second=0,microsecond=0); sms=int(s8.timestamp()*1000)
    td=[p for p in r["data"] if int(p.get("uTime") or 0)>=sms]; n=len(td)
    if n==0: await u.message.reply_text(f"{E.BOT} OKXLive普K｜{ACCT}\n事件：當日戰報 {s8.strftime('%m-%d')}\n今日尚無已平倉交易"); return
    tp=sum((Decimal(p.get('pnl') or '0') for p in td),Decimal(0))
    tf=sum((Decimal(p.get('fee') or '0')+Decimal(p.get('fundingFee') or '0') for p in td),Decimal(0))
    tn=sum((Decimal(p.get('realizedPnl') or '0') for p in td),Decimal(0))
    win=sum(1 for p in td if Decimal(p.get('realizedPnl') or '0')>0)
    loss=sum(1 for p in td if Decimal(p.get('realizedPnl') or '0')<0); even=n-win-loss
    await u.message.reply_text(f"{E.BOT} OKXLive普K｜{ACCT}\n事件：當日戰報 {s8.strftime('%m-%d')}\n━━━━━━━━━━\n"
        f"成交筆數：{n}\n獲利：{win} 筆\n虧損：{loss} 筆\n打平：{even} 筆\n勝率：{win/n*100:.1f}%\n"
        f"毛損益：{tp:+.6f}\n手續費：{tf:+.6f}\n淨損益：{tn:+.6f} {E.pnl_emoji(tn)}\n━━━━━━━━━━\n時間：{hhmmss()}")
async def cmd_leverage(u,c):
    if not c.args: await u.message.reply_text(f"{E.BOT} 用法：/leverage 商品 [槓桿]"); return
    sym=c.args[0].upper(); lev=Decimal(c.args[1].replace("x","")) if len(c.args)>1 else Decimal(1)
    try:
        sp=get_spec(sym); last=get_last(sp["iid"]); m=sp["minsz"]*sp["ctval"]*last/lev
        await u.message.reply_text(f"{E.BOT} 最低下單金額\n商品：{sym}\n槓桿：{lev}x\n標記價：{last}\n"
            f"最小張：{sp['minsz']}\n面值：{sp['ctval']}{sp['ctvalccy']}\n最低金額：{m:.4f} USDT\n最大槓桿：{sp['maxlev']}x")
    except: await u.message.reply_text(f"{E.LOSS} 查詢失敗")
async def cmd_coins(u,c):
    on=[s["symbol"] for s in SYMS if s["enabled"]]
    await u.message.reply_text(f"{E.BOT} 幣種清單\n啟用（{len(on)}）：\n"+"\n".join("　"+s for s in on))
async def cmd_timeframe(u,c):
    global ACCOUNT_TF
    if not c.args:
        await u.message.reply_text(f"{E.BOT} 目前週期：{ACCOUNT_TF}\n可選 3m/5m/10m/15m\n變更：/timeframe 10m"); return
    tf=c.args[0]
    if tf not in TF_SEC: await u.message.reply_text(f"{E.BOT} 週期須 3m/5m/10m/15m"); return
    ACCOUNT_TF=tf
    await u.message.reply_text(f"{E.BOT} ✅ 帳戶週期已設為 {tf}\n（僅影響之後新建立的策略；運行中維持原週期）")
async def cmd_menu(u,c):
    await u.message.reply_text(f"{E.BOT} OKXLive普K｜{ACCT}\n使用說明\n━━━━━━━━━━\n"
        "/run 商品 方向 槓桿 保證金 埋伏 TP SL TE\n"
        f"例：/run ETHUSDT L 1x 3 0.5 0.5 0.5 180\n　週期依 /timeframe（目前 {ACCOUNT_TF}）\n"
        "　同幣可雙向、多幣可並行\n/confirm 確認啟動\n/stop 商品 方向\n/stopall 停全部\n"
        "/status 所有策略現況\n/summary 當日戰報\n/leverage 最低額\n/timeframe 查看/設定週期\n/coins 幣種\n"
        "━━━━━━━━━━\n⚠ 真實下單，循環交易")
async def cmd_unknown(u,c): await u.message.reply_text(f"{E.BOT} 指令無法辨識：{u.message.text}\n請用 /menu")
async def _menu(app):
    await app.bot.delete_my_commands()
    await app.bot.set_my_commands([BotCommand("run","建立策略"),BotCommand("confirm","確認啟動"),
        BotCommand("stop","停指定"),BotCommand("stopall","停全部"),BotCommand("status","現況"),
        BotCommand("summary","當日戰報"),BotCommand("leverage","最低額"),BotCommand("timeframe","週期"),
        BotCommand("coins","幣種"),BotCommand("menu","說明")])
    print("左下 Menu 已更新")
def main():
    print(f"啟動 o3333o 普K B5-5（token ...{TOKEN[-6:]}）")
    app=Application.builder().token(TOKEN).post_init(_menu).build()
    for cmd,fn in [(["menu","start"],cmd_menu),("run",cmd_run),("confirm",cmd_confirm),("stop",cmd_stop),
        ("stopall",cmd_stopall),("status",cmd_status),("summary",cmd_summary),("leverage",cmd_leverage),
        ("timeframe",cmd_timeframe),("coins",cmd_coins)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(MessageHandler(filters.COMMAND,cmd_unknown))
    app.run_polling()
if __name__=="__main__": main()

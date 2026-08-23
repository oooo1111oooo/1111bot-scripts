#!/usr/bin/env python3
"""B5-4 多策略版：同幣可雙向並存、多幣並行。/run /confirm /stop /stopall /status。o3333o真實下單。"""
import sys, hmac, base64, hashlib, json, time, asyncio, uuid
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN
from datetime import datetime, timezone, timedelta
import httpx
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

sys.path.insert(0,"/srv/1111bot")
from app.core import emoji as E
from app.strategy.normal import next_open_epoch, TF_SEC

BASE="https://www.okx.com"; ACCT="o3333o"; TZ8=timezone(timedelta(hours=8))

def load_env(p):
    d={}
    with open(p) as f:
        for line in f:
            line=line.strip()
            if "=" in line and not line.startswith("#"):
                k,v=line.split("=",1); d[k]=v
    return d
ACC=load_env("/srv/1111bot/config/accounts.env")
BOTS=load_env("/srv/1111bot/config/bots.env")
TOKEN=BOTS["BOT_o3333o_NORMAL"]
SYMS=json.load(open("/srv/1111bot/config/symbols.json"))["symbols"]

PENDING={}       # chat_id -> 待confirm參數
STRATS={}        # key "SYM_DIR" -> 策略dict
TASKS={}         # key -> asyncio task

def skey(sym,d): return f"{sym}_{d}"
def inst_id(s): return s.replace("USDT","")+"-USDT-SWAP"
def now8(): return datetime.now(TZ8)
def hhmmss(): return now8().strftime("%H:%M:%S")
def ts_now():
    n=datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.")+f"{n.microsecond//1000:03d}Z"
def sign(sec,ts,m,p,b=""):
    return base64.b64encode(hmac.new(sec.encode(),f"{ts}{m}{p}{b}".encode(),hashlib.sha256).digest()).decode()
def api(method,path,body_obj=None):
    body=json.dumps(body_obj) if body_obj else ""
    ts=ts_now()
    h={"OK-ACCESS-KEY":ACC[f"OKX_{ACCT}_API_KEY"],
       "OK-ACCESS-SIGN":sign(ACC[f"OKX_{ACCT}_SECRET"],ts,method,path,body),
       "OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":ACC[f"OKX_{ACCT}_PASSPHRASE"],
       "Content-Type":"application/json"}
    return httpx.request(method,BASE+path,headers=h,content=body,timeout=15).json()
def pub(path): return httpx.get(BASE+path,timeout=15).json()
def get_spec(sym):
    iid=inst_id(sym)
    d=pub(f"/api/v5/public/instruments?instType=SWAP&instId={iid}")["data"][0]
    return {"iid":iid,"tick":Decimal(d["tickSz"]),"lot":Decimal(d["lotSz"]),
            "minsz":Decimal(d["minSz"]),"ctval":Decimal(d["ctVal"]),
            "maxlev":Decimal(d["lever"]),"ctvalccy":d["ctValCcy"]}
def get_last(iid): return Decimal(pub(f"/api/v5/market/ticker?instId={iid}")["data"][0]["last"])
def align(price,tick,d):
    r=ROUND_FLOOR if d=="L" else ROUND_CEILING
    return (price/tick).to_integral_value(rounding=r)*tick
def calc_size(margin,lev,price,ctval,lot):
    return ((margin*lev/price)/ctval/lot).to_integral_value(rounding=ROUND_DOWN)*lot
async def notify(app,chat,txt):
    try: await app.bot.send_message(chat,txt)
    except Exception as e: print("notify fail",e)

# ========== 背景策略循環 ==========
async def strategy_loop(app, chat, S):
    spec=S["spec"]; iid=spec["iid"]; d=S["dir"]; k=skey(S["sym"],d)
    await notify(app,chat,
        f"{E.BOT} OKXLive普K｜{ACCT}\n事件：🟢 策略啟動\n"
        f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)} {S['lev']}x\n"
        f"週　　期：{S['tf']}\n狀　　態：等下輪開盤\n時間：{hhmmss()}")
    try:
        while S["alive"]:
            S["state"]="等下輪"
            tf_sec=TF_SEC[S["tf"]]
            open_epoch=next_open_epoch(int(time.time()),S["tf"])
            wait=open_epoch-time.time()
            if wait>0: await asyncio.sleep(wait)
            if not S["alive"]: break
            op=get_last(iid)
            ambush=align(op*(1-S["offset"]/100) if d=="L" else op*(1+S["offset"]/100),spec["tick"],d)
            size=calc_size(S["margin"],Decimal(S["lev"]),ambush,spec["ctval"],spec["lot"])
            if size<spec["minsz"]:
                await notify(app,chat,f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 保證金不足，循環停止"); break
            cl="n"+uuid.uuid4().hex[:14]
            side="buy" if d=="L" else "sell"
            posSide="long" if d=="L" else "short"
            r=api("POST","/api/v5/trade/order",{"instId":iid,"tdMode":"isolated",
                "side":side,"posSide":posSide,"ordType":"limit","px":str(ambush),
                "sz":str(size),"clOrdId":cl})
            if r.get("code")!="0":
                await notify(app,chat,f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 掛單失敗：{r.get('data',[{}])[0].get('sMsg',r.get('msg'))}\n下一輪重試")
                await asyncio.sleep(5); continue
            ordId=r["data"][0]["ordId"]
            S["state"]="委託中"; S["ordId"]=ordId; S["ambush"]=ambush
            await notify(app,chat,
                f"{E.BOT} OKXLive普K｜{ACCT}\n事件：🔔 已掛埋伏單\n"
                f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                f"埋伏價格：{ambush}\n下單張數：{size}\n開盤估價：{op}\n"
                f"狀　　態：等待成交\n時間：{hhmmss()}")
            next_boundary=open_epoch+tf_sec
            filled=False; fillpx=None
            while S["alive"] and time.time()<next_boundary-3:
                await asyncio.sleep(2)
                st=api("GET",f"/api/v5/trade/order?instId={iid}&ordId={ordId}")
                if st.get("code")=="0" and st["data"]:
                    ostate=st["data"][0]["state"]
                    if ostate=="filled":
                        filled=True; fillpx=Decimal(st["data"][0]["avgPx"]); break
                    elif ostate=="canceled": break
            if not S["alive"]:
                api("POST","/api/v5/trade/cancel-order",{"instId":iid,"ordId":ordId}); break
            if not filled:
                api("POST","/api/v5/trade/cancel-order",{"instId":iid,"ordId":ordId})
                await notify(app,chat,
                    f"{E.BOT} OKXLive普K｜{ACCT}\n事件：未成交撤單\n"
                    f"{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)} 埋伏 {ambush} 未觸及\n"
                    f"下一步：下一根開盤重新埋伏\n時間：{hhmmss()}")
                continue
            S["state"]="持倉中"; S["entry_px"]=fillpx
            if d=="L":
                tp_px=align(fillpx*(1+S["tp"]/100),spec["tick"],"S")
                sl_px=align(fillpx*(1-S["sl"]/100),spec["tick"],"L")
            else:
                tp_px=align(fillpx*(1-S["tp"]/100),spec["tick"],"L")
                sl_px=align(fillpx*(1+S["sl"]/100),spec["tick"],"S")
            await notify(app,chat,
                f"{E.BOT} OKXLive普K｜{ACCT}\n事件：🔔 已進場成交\n"
                f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                f"進場價格：{fillpx}\n止盈 TP：{tp_px}\n止損 SL：{sl_px}\n"
                f"持倉 TE：{S['te']}s\n狀　　態：📌 持倉中\n時間：{hhmmss()}")
            entry_epoch=time.time(); exit_reason=None
            while S["alive"]:
                await asyncio.sleep(2)
                last=get_last(iid); held=time.time()-entry_epoch
                if d=="L":
                    if last>=tp_px: exit_reason="Take_Profit"; break
                    if last<=sl_px: exit_reason="Stop_Loss"; break
                else:
                    if last<=tp_px: exit_reason="Take_Profit"; break
                    if last>=sl_px: exit_reason="Stop_Loss"; break
                if held>=S["te"]: exit_reason="Time_Exit"; break
            if not S["alive"]:
                await notify(app,chat,f"{E.BOT} {S['sym']} {E.dir_word(d)} 循環停止但仍有持倉，請至 OKX 確認"); break
            close_side="sell" if d=="L" else "buy"
            api("POST","/api/v5/trade/order",{"instId":iid,"tdMode":"isolated",
                "side":close_side,"posSide":posSide,"ordType":"market",
                "sz":str(size),"clOrdId":"x"+uuid.uuid4().hex[:14]})
            exit_px=get_last(iid)
            gross=(exit_px-fillpx)*size*spec["ctval"] if d=="L" else (fillpx-exit_px)*size*spec["ctval"]
            await notify(app,chat,
                f"{E.BOT} OKXLive普K｜{ACCT}\n事件：{'🟢' if gross>=0 else '🔴'} 已出場\n"
                f"商　　品：{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n"
                f"出場原因：{exit_reason}\n進場價：{fillpx}\n出場價：{exit_px}\n"
                f"持倉秒數：{int(time.time()-entry_epoch)}s\n毛損益：{gross:+.6f} USDT {E.pnl_emoji(gross)}\n"
                f"下一步：下一根開盤續掛\n時間：{hhmmss()}")
    except Exception as e:
        await notify(app,chat,f"{E.BOT} {E.LOSS} {S['sym']} {E.dir_word(d)} 循環錯誤：{type(e).__name__}: {e}")
    finally:
        S["state"]="已停止"; S["alive"]=False
        STRATS.pop(k,None); TASKS.pop(k,None)

# ========== 指令 ==========
async def cmd_run(u,c):
    args=c.args
    fmt="用法：/run 商品 方向 週期 槓桿 保證金 埋伏 TP SL TE\n例：/run ETHUSDT L 5m 1x 3 0.5 0.5 0.5 180"
    if len(args)!=9:
        await u.message.reply_text(f"{E.BOT} 參數數量錯誤（需9個）\n{fmt}"); return
    try:
        sym=args[0].upper();direction=args[1].upper();tf=args[2]
        lev=int(args[3].replace("x",""));margin=Decimal(args[4]);offset=Decimal(args[5])
        tp=Decimal(args[6]);sl=Decimal(args[7]);te=int(args[8])
    except Exception:
        await u.message.reply_text(f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    if direction not in("L","S"): await u.message.reply_text(f"{E.BOT} 方向須 L 或 S"); return
    if tf not in TF_SEC: await u.message.reply_text(f"{E.BOT} 週期須 3m/5m/10m/15m"); return
    if not 1<=te<=900: await u.message.reply_text(f"{E.BOT} TE 須 1~900 秒"); return
    k=skey(sym,direction)
    if k in STRATS and STRATS[k].get("alive"):
        await u.message.reply_text(f"{E.BOT} {sym} {E.dir_word(direction)} 已在運行（{STRATS[k]['state']}）\n同幣同向不可重複，請先 /stop {sym} {direction}"); return
    try: spec=get_spec(sym)
    except: await u.message.reply_text(f"{E.LOSS} 找不到商品 {sym}"); return
    op=get_last(spec["iid"])
    ambush=align(op*(1-offset/100) if direction=="L" else op*(1+offset/100),spec["tick"],direction)
    size=calc_size(margin,Decimal(lev),ambush,spec["ctval"],spec["lot"])
    if size<spec["minsz"]:
        await u.message.reply_text(f"{E.BOT} {E.LOSS} 保證金不足，算出 {size} 張 < 最小 {spec['minsz']}"); return
    de=E.dir_emoji(direction);dw=E.dir_word(direction)
    PENDING[u.effective_chat.id]={"t":time.time(),"sym":sym,"dir":direction,"tf":tf,
        "lev":lev,"margin":margin,"offset":offset,"tp":tp,"sl":sl,"te":te,"spec":spec}
    await u.message.reply_text(
        f"{E.BOT} OKXLive普K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
        f"商　　品：{de} {sym} {dw} {lev}x\n週　　期：{tf}\n進場模式：直接埋伏 Maker\n"
        f"開盤估價：{op}\n埋伏距離：{offset}%\n埋伏價格：{ambush}\n"
        f"止盈 TP：{tp}%\n止損 SL：{sl}%\n持倉 TE：{te}s\n"
        f"保 證 金：{margin} USDT\n下單張數：{size}\n"
        f"━━━━━━━━━━\n⚠ 確認後真實循環交易\n下一步：60秒內 /confirm\n時間：{hhmmss()}")
    asyncio.create_task(_timeout(c.application,u.effective_chat.id,PENDING[u.effective_chat.id]["t"]))

async def _timeout(app,chat,stamp):
    await asyncio.sleep(61)
    p=PENDING.get(chat)
    if p and p["t"]==stamp:
        del PENDING[chat]
        await notify(app,chat,f"{E.BOT} 事件：參數輸入逾時\n結果：已取消\n請重新 /run\n時間：{hhmmss()}")

async def cmd_confirm(u,c):
    p=PENDING.get(u.effective_chat.id)
    if not p:
        await u.message.reply_text(f"{E.BOT} 沒有待確認的 /run"); return
    if time.time()-p["t"]>60:
        del PENDING[u.effective_chat.id]
        await u.message.reply_text(f"{E.BOT} 確認逾時，請重新 /run"); return
    del PENDING[u.effective_chat.id]
    k=skey(p["sym"],p["dir"])
    S={**p,"alive":True,"state":"等下輪","chat":u.effective_chat.id}
    STRATS[k]=S
    TASKS[k]=asyncio.create_task(strategy_loop(c.application,u.effective_chat.id,S))
    cnt=sum(1 for s in STRATS.values() if s.get("alive"))
    await u.message.reply_text(f"{E.BOT} ✅ 已確認，{p['sym']} {E.dir_word(p['dir'])} 策略啟動\n目前運行中策略：{cnt} 個\n即將於下一根開盤掛單")

async def cmd_stop(u,c):
    args=c.args
    alive=[k for k,s in STRATS.items() if s.get("alive")]
    if not alive:
        await u.message.reply_text(f"{E.BOT} 目前無運行中策略"); return
    if len(args)==0:
        lst="\n".join(f"・/stop {STRATS[k]['sym']} {STRATS[k]['dir']}（{STRATS[k]['state']}）" for k in alive)
        await u.message.reply_text(f"{E.BOT} 請指定要停的策略：\n{lst}\n或 /stopall 停全部"); return
    sym=args[0].upper()
    if len(args)>=2:
        d=args[1].upper(); targets=[skey(sym,d)] if skey(sym,d) in alive else []
    else:
        targets=[k for k in alive if STRATS[k]["sym"]==sym]
    if not targets:
        await u.message.reply_text(f"{E.BOT} 找不到運行中的 {sym} 策略"); return
    if len(targets)>1:
        await u.message.reply_text(f"{E.BOT} {sym} 有多個方向在跑，請指定：\n・/stop {sym} L\n・/stop {sym} S"); return
    await _do_stop(u,targets[0])

async def _do_stop(u,k):
    S=STRATS.get(k)
    if not S: 
        await u.message.reply_text(f"{E.BOT} 策略已不存在"); return
    state=S["state"]; sym=S["sym"]; d=S["dir"]
    if state=="持倉中":
        S["alive"]=False
        pos=api("GET","/api/v5/account/positions"); posinfo=""
        if pos.get("code")=="0":
            for pp in pos["data"]:
                if pp["instId"]==S["spec"]["iid"] and pp.get("posSide")==("long" if d=="L" else "short") and float(pp.get("pos","0"))!=0:
                    posinfo=f"倉位 {pp['pos']} 張 均價 {pp.get('avgPx','?')} 浮 {pp.get('upl','?')}"
        await u.message.reply_text(
            f"{E.BOT} OKXLive普K｜{ACCT}\n事件：/stop（持倉中）\n"
            f"{E.dir_emoji(d)} {sym} {E.dir_word(d)}\n"
            f"⚠ 此單已進場，系統不自動平倉\n{posinfo}\n"
            f"請至 OKX 手動平倉\n循環已停止，不再續掛\n時間：{hhmmss()}")
    else:
        if S.get("ordId") and state=="委託中":
            api("POST","/api/v5/trade/cancel-order",{"instId":S["spec"]["iid"],"ordId":S["ordId"]})
        S["alive"]=False
        await u.message.reply_text(
            f"{E.BOT} OKXLive普K｜{ACCT}\n事件：/stop\n"
            f"{E.dir_emoji(d)} {sym} {E.dir_word(d)}\n"
            f"原狀態：{state}\n動作：{'已撤銷委託' if state=='委託中' else '已移除策略'}\n時間：{hhmmss()}")

async def cmd_stopall(u,c):
    alive=[k for k,s in STRATS.items() if s.get("alive")]
    if not alive:
        await u.message.reply_text(f"{E.BOT} 目前無運行中策略"); return
    held=[]; stopped=[]
    for k in list(alive):
        S=STRATS[k]
        if S["state"]=="持倉中":
            S["alive"]=False; held.append(f"{S['sym']} {S['dir']}")
        else:
            if S.get("ordId") and S["state"]=="委託中":
                api("POST","/api/v5/trade/cancel-order",{"instId":S["spec"]["iid"],"ordId":S["ordId"]})
            S["alive"]=False; stopped.append(f"{S['sym']} {S['dir']}")
    msg=f"{E.BOT} OKXLive普K｜{ACCT}\n事件：/stopall\n━━━━━━━━━━\n"
    if stopped: msg+=f"已停止（{len(stopped)}）：\n"+"\n".join("・"+x for x in stopped)+"\n"
    if held: msg+=f"⚠ 持倉中需手動平倉（{len(held)}）：\n"+"\n".join("・"+x for x in held)+"\n請至 OKX 平倉\n"
    msg+=f"時間：{hhmmss()}"
    await u.message.reply_text(msg)

async def cmd_status(u,c):
    bal=api("GET","/api/v5/account/balance")
    pos=api("GET","/api/v5/account/positions")
    pend=api("GET","/api/v5/trade/orders-pending")
    eq=avail="?"
    if bal.get("code")=="0":
        det=bal["data"][0].get("details",[])
        x=next((d for d in det if d["ccy"]=="USDT"),None)
        if x: eq=f"{Decimal(x.get('eq','0')):.4f}";avail=f"{Decimal(x.get('availEq') or x.get('availBal') or '0'):.4f}"
    pl=[p for p in pos.get("data",[]) if float(p.get("pos","0"))!=0] if pos.get("code")=="0" else []
    pdl=pend.get("data",[]) if pend.get("code")=="0" else []
    alive=[s for s in STRATS.values() if s.get("alive")]
    L=[f"{E.BOT} OKXLive普K｜{ACCT}","事件：現況（即時查 OKX）","━━━━━━━━━━",
       f"USDT權益：{eq}",f"可用餘額：{avail}",f"運行中策略：{len(alive)} 個"]
    for s in alive:
        L.append(f"　{E.dir_emoji(s['dir'])} {s['sym']} {E.dir_word(s['dir'])}：{s['state']}")
    L+=[f"持倉數：{len(pl)}",f"掛單數：{len(pdl)}"]
    for p in pl:
        s=E.LONG if p["posSide"]=="long" else E.SHORT;upl=Decimal(p.get('upl','0'))
        L.append(f"{s} {p['instId']} 張{p['pos']} 浮{upl:+.4f}{E.pnl_emoji(upl)}")
    L+=["━━━━━━━━━━",f"時間：{hhmmss()} UTC+8"]
    await u.message.reply_text("\n".join(L))

async def cmd_leverage(u,c):
    if len(c.args)<1: await u.message.reply_text(f"{E.BOT} 用法：/leverage 商品 [槓桿]"); return
    sym=c.args[0].upper();lev=Decimal(c.args[1].replace("x","")) if len(c.args)>1 else Decimal(1)
    try:
        spec=get_spec(sym);last=get_last(spec["iid"])
        m=spec["minsz"]*spec["ctval"]*last/lev
        await u.message.reply_text(
            f"{E.BOT} 最低下單金額查詢\n商品：{sym}\n槓桿：{lev}x\n標記價：{last}\n"
            f"最小張：{spec['minsz']}\n面值：{spec['ctval']}{spec['ctvalccy']}\n"
            f"最低金額：{m:.4f} USDT\n最大槓桿：{spec['maxlev']}x")
    except: await u.message.reply_text(f"{E.LOSS} 查詢失敗")

async def cmd_coins(u,c):
    on=[s["symbol"] for s in SYMS if s["enabled"]]
    await u.message.reply_text(f"{E.BOT} 幣種清單\n啟用（{len(on)}）：\n"+"\n".join("　"+s for s in on))

async def cmd_timeframe(u,c):
    await u.message.reply_text(f"{E.BOT} 目前週期 5m\n可選 3m/5m/10m/15m（設定於 B5-5 開放）")

async def cmd_menu(u,c):
    await u.message.reply_text(
        f"{E.BOT} OKXLive普K｜{ACCT}\n使用說明\n━━━━━━━━━━\n"
        "/run 商品 方向 週期 槓桿 保證金 埋伏 TP SL TE\n"
        "例：/run ETHUSDT L 5m 1x 3 0.5 0.5 0.5 180\n"
        "　同幣可雙向並存、多幣可並行\n"
        "/confirm 確認並啟動\n"
        "/stop 商品 方向 停指定策略（例 /stop ETHUSDT L）\n"
        "/stopall 停全部策略\n"
        "/status 所有運行中策略現況\n"
        "/leverage 最低下單額\n/coins 幣種\n/timeframe 週期\n"
        "━━━━━━━━━━\n⚠ 真實下單，會循環交易")

async def cmd_unknown(u,c):
    await u.message.reply_text(f"{E.BOT} 指令無法辨識：{u.message.text}\n請用 /menu")

async def _set_menu(app):
    await app.bot.delete_my_commands()
    await app.bot.set_my_commands([
        BotCommand("run","建立策略(可多幣多向)"),BotCommand("confirm","確認啟動"),
        BotCommand("stop","停指定策略"),BotCommand("stopall","停全部"),
        BotCommand("status","所有策略現況"),BotCommand("leverage","最低下單額"),
        BotCommand("timeframe","週期"),BotCommand("coins","幣種清單"),
        BotCommand("menu","使用說明")])
    print("左下 Menu 已更新")

def main():
    print(f"啟動 o3333o 普K bot B5-4 多策略版（token ...{TOKEN[-6:]}）")
    app=Application.builder().token(TOKEN).post_init(_set_menu).build()
    app.add_handler(CommandHandler(["menu","start"],cmd_menu))
    app.add_handler(CommandHandler("run",cmd_run))
    app.add_handler(CommandHandler("confirm",cmd_confirm))
    app.add_handler(CommandHandler("stop",cmd_stop))
    app.add_handler(CommandHandler("stopall",cmd_stopall))
    app.add_handler(CommandHandler("status",cmd_status))
    app.add_handler(CommandHandler("leverage",cmd_leverage))
    app.add_handler(CommandHandler("coins",cmd_coins))
    app.add_handler(CommandHandler("timeframe",cmd_timeframe))
    app.add_handler(MessageHandler(filters.COMMAND,cmd_unknown))
    app.run_polling()

if __name__=="__main__":
    main()

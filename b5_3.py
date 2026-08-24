#!/usr/bin/env python3
"""B5-8 普K：狀態持久化(重啟不失憶)+啟動認領+對帳。o3333o。"""
import sys, hmac, base64, hashlib, json, time, asyncio, uuid, os
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_DOWN
from datetime import datetime, timezone, timedelta
import httpx
from telegram import BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters
sys.path.insert(0,"/srv/1111bot")
from app.core import emoji as E
from app.strategy.normal import next_open_epoch, TF_SEC
BASE="https://www.okx.com"; ACCT="o3333o"; TZ8=timezone(timedelta(hours=8))
ACCOUNT_TF="5m"
STATE_FILE="/srv/1111bot/data/strategies_o3333o.json"
def load_env(p):
    d={}
    for line in open(p):
        line=line.strip()
        if "=" in line and not line.startswith("#"):
            k,v=line.split("=",1); d[k]=v
    return d
ACC=load_env("/srv/1111bot/config/accounts.env"); BOTS=load_env("/srv/1111bot/config/bots.env")
TOKEN=BOTS["BOT_o3333o_NORMAL"]; SYMS=json.load(open("/srv/1111bot/config/symbols.json"))["symbols"]
PENDING={}; STRATS={}; TASKS={}; STATS={}
CHAT_ID=None  # 記錄主要 chat，重啟通知用

def skey(s,d): return f"{s}_{d}"
def inst_id(s): return s.replace("USDT","")+"-USDT-SWAP"
def now8(): return datetime.now(TZ8)
def hhmmss(): return now8().strftime("%H:%M:%S")
def today8(): return now8().strftime("%Y-%m-%d")

# ===== 持久化 =====
def save_state():
    """把所有活著的策略寫進硬碟"""
    try:
        data={"chat":CHAT_ID,"tf":ACCOUNT_TF,"stats":STATS,"strats":[]}
        for k,S in STRATS.items():
            if S.get("alive"):
                data["strats"].append({k2:v2 for k2,v2 in S.items()
                    if k2 in ("sym","dir","tf","lev","margin","offset","tp","sl","te","state","round","chat")})
        # Decimal 轉字串
        def enc(o): return str(o) if isinstance(o,Decimal) else o
        with open(STATE_FILE,"w") as f:
            json.dump(data,f,default=enc)
    except Exception as e: print("save_state fail",e)

def bump(k,field):
    t=today8()
    if k not in STATS or STATS[k]["date"]!=t: STATS[k]={"date":t,"placed":0,"entered":0}
    STATS[k][field]+=1; save_state()
def get_stat(k):
    t=today8()
    if k not in STATS or STATS[k]["date"]!=t: return (0,0)
    return (STATS[k]["placed"],STATS[k]["entered"])

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
    try:
        while S["alive"]:
            S["state"]="等下輪"; save_state(); tf_sec=TF_SEC[S["tf"]]
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
            oid=r["data"][0]["ordId"]; S["state"]="委託中"; S["ordId"]=oid; bump(k,"placed")
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
                api("POST","/api/v5/trade/cancel-order",{"instId":iid,"ordId":oid}); continue
            bump(k,"entered"); S["state"]="持倉中"; S["entry_px"]=str(fpx); save_state()
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
        S["state"]="已停止"; S["alive"]=False; STRATS.pop(k,None); TASKS.pop(k,None); save_state()

def rebuild_strat(app,d):
    """從存檔資料重建一個策略dict"""
    spec=get_spec(d["sym"])
    S={"sym":d["sym"],"dir":d["dir"],"tf":d.get("tf",ACCOUNT_TF),"lev":int(d["lev"]),
       "margin":Decimal(str(d["margin"])),"offset":Decimal(str(d["offset"])),
       "tp":Decimal(str(d["tp"])),"sl":Decimal(str(d["sl"])),"te":int(d["te"]),
       "spec":spec,"alive":True,"state":"等下輪","chat":d.get("chat",CHAT_ID)}
    return S

async def startup_recover(app):
    """開機認領：讀存檔重建策略，並與OKX對帳"""
    global CHAT_ID,ACCOUNT_TF,STATS
    if not os.path.exists(STATE_FILE): 
        print("無存檔，全新啟動"); return
    try:
        data=json.load(open(STATE_FILE))
    except Exception as e:
        print("讀存檔失敗",e); return
    CHAT_ID=data.get("chat"); ACCOUNT_TF=data.get("tf","5m"); STATS=data.get("stats",{})
    saved=data.get("strats",[])
    if not saved: print("存檔無策略"); return
    print(f"認領 {len(saved)} 個策略...")
    recovered=[]
    for d in saved:
        try:
            S=rebuild_strat(app,d); k=skey(S["sym"],S["dir"])
            STRATS[k]=S; TASKS[k]=asyncio.create_task(loop(app,S["chat"],S))
            recovered.append(f"{E.dir_emoji(S['dir'])} {S['sym']} {E.dir_word(S['dir'])}")
        except Exception as e: print("重建失敗",d,e)
    # 對帳：查OKX現況
    if CHAT_ID and recovered:
        pend=api("GET","/api/v5/trade/orders-pending")
        pos=api("GET","/api/v5/account/positions")
        n_ord=len(pend.get("data",[])) if pend.get("code")=="0" else 0
        n_pos=len([p for p in pos.get("data",[]) if float(p.get("pos","0"))!=0]) if pos.get("code")=="0" else 0
        await notify(app,CHAT_ID,
            f"{E.BOT} OKXLive普K｜{ACCT}\n事件：🔄 重啟認領完成\n━━━━━━━━━━\n"
            f"已接管策略（{len(recovered)}）：\n"+"\n".join("・"+x for x in recovered)+"\n"
            f"OKX 現況：掛單{n_ord} 持倉{n_pos}\n"
            f"循環已接管，繼續運作\n時間：{hhmmss()}")

async def cmd_run(u,c):
    global CHAT_ID; CHAT_ID=u.effective_chat.id
    a=c.args
    fmt=f"用法：/run 商品 方向 槓桿 保證金 埋伏 TP SL TE\n例：/run ETHUSDT L 1x 3 0.5 0.5 0.5 180\n（週期依 /timeframe，目前 {ACCOUNT_TF}）"
    if len(a)!=8: await u.message.reply_text(f"{E.BOT} 參數數量錯誤（需8個）\n{fmt}"); return
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
    global CHAT_ID; CHAT_ID=u.effective_chat.id
    p=PENDING.get(u.effective_chat.id)
    if not p: await u.message.reply_text(f"{E.BOT} 沒有待確認的 /run"); return
    if time.time()-p["t"]>60: del PENDING[u.effective_chat.id]; await u.message.reply_text(f"{E.BOT} 確認逾時"); return
    del PENDING[u.effective_chat.id]; k=skey(p["sym"],p["dir"])
    S={**p,"alive":True,"state":"等下輪","chat":u.effective_chat.id}
    STRATS[k]=S; TASKS[k]=asyncio.create_task(loop(c.application,u.effective_chat.id,S)); save_state()
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
        S["alive"]=False; save_state(); pi=""
        pr=api("GET","/api/v5/account/positions")
        if pr.get("code")=="0":
            for pp in pr["data"]:
                if pp["instId"]==S["spec"]["iid"] and pp.get("posSide")==("long" if d=="L" else "short") and float(pp.get("pos","0"))!=0:
                    pi=f"倉位 {pp['pos']} 張 均價 {pp.get('avgPx','?')} 浮 {pp.get('upl','?')}"
        await u.message.reply_text(f"{E.BOT} /stop（持倉中）\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n⚠ 已進場不自動平倉\n{pi}\n請至 OKX 手動平倉")
    else:
        if S.get("ordId") and st=="委託中": api("POST","/api/v5/trade/cancel-order",{"instId":S["spec"]["iid"],"ordId":S["ordId"]})
        S["alive"]=False; save_state()
        await u.message.reply_text(f"{E.BOT} /stop\n{E.dir_emoji(d)} {S['sym']} {E.dir_word(d)}\n原狀態：{st}\n動作：{'已撤銷委託' if st=='委託中' else '已移除'}")
async def cmd_stopall(u,c):
    alive=[k for k,s in STRATS.items() if s.get("alive")]
    # 同時清 OKX 殘留掛單（解決孤兒）
    pend=api("GET","/api/v5/trade/orders-pending")
    okx_orders=pend.get("data",[]) if pend.get("code")=="0" else []
    held=[];done=[]
    for k in list(alive):
        S=STRATS[k]
        if S["state"]=="持倉中": S["alive"]=False; held.append(f"{S['sym']} {S['dir']}")
        else:
            if S.get("ordId") and S["state"]=="委託中": api("POST","/api/v5/trade/cancel-order",{"instId":S["spec"]["iid"],"ordId":S["ordId"]})
            S["alive"]=False; done.append(f"{S['sym']} {S['dir']}")
    # 清掉任何OKX殘單（孤兒）
    orphan=0
    for o in okx_orders:
        cr=api("POST","/api/v5/trade/cancel-order",{"instId":o["instId"],"ordId":o["ordId"]})
        if cr.get("code")=="0": orphan+=1
    save_state()
    m=f"{E.BOT} /stopall\n━━━━━━━━━━\n"
    if done: m+=f"已停止策略（{len(done)}）：\n"+"\n".join("・"+x for x in done)+"\n"
    if orphan: m+=f"另清除OKX殘留掛單：{orphan} 筆\n"
    if held: m+=f"⚠ 持倉需手動平倉（{len(held)}）：\n"+"\n".join("・"+x for x in held)+"\n請至 OKX 平倉\n"
    if not done and not orphan and not held: m+="目前無策略、無殘單\n"
    await u.message.reply_text(m+f"時間：{hhmmss()}")
async def cmd_status(u,c):
    global CHAT_ID; CHAT_ID=u.effective_chat.id
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
        k=skey(s["sym"],s["dir"]); placed,entered=get_stat(k)
        L.append(f"　{E.dir_emoji(s['dir'])} {s['sym']} {E.dir_word(s['dir'])}：{s['state']}(掛{placed}/進{entered})")
    L+=[f"持倉數：{len(pl)}",f"掛單數：{len(pdl)}"]
    for p in pl:
        s=E.LONG if p["posSide"]=="long" else E.SHORT; upl=Decimal(p.get('upl','0'))
        L.append(f"{s} {p['instId']} 張{p['pos']} 浮{upl:+.4f}{E.pnl_emoji(upl)}")
    L+=["━━━━━━━━━━",f"時間：{hhmmss()} UTC+8"]
    await u.message.reply_text("\n".join(L))
async def cmd_summary(u,c):
    r=api("GET","/api/v5/account/positions-history?instType=SWAP&limit=100")
    s8=now8().replace(hour=0,minute=0,second=0,microsecond=0); sms=int(s8.timestamp()*1000)
    td=[p for p in r.get("data",[]) if int(p.get("uTime") or 0)>=sms] if r.get("code")=="0" else []
    n=len(td)
    L=[f"{E.BOT} OKXLive普K｜{ACCT}",f"事件：當日戰報 {s8.strftime('%m-%d')}","━━━━━━━━━━"]
    if n>0:
        tp=sum((Decimal(p.get('pnl') or '0') for p in td),Decimal(0))
        tf=sum((Decimal(p.get('fee') or '0')+Decimal(p.get('fundingFee') or '0') for p in td),Decimal(0))
        tn=sum((Decimal(p.get('realizedPnl') or '0') for p in td),Decimal(0))
        win=sum(1 for p in td if Decimal(p.get('realizedPnl') or '0')>0)
        loss=sum(1 for p in td if Decimal(p.get('realizedPnl') or '0')<0); even=n-win-loss
        L+=[f"平倉筆數：{n}",f"獲利/虧損/打平：{win}/{loss}/{even}",f"勝率：{win/n*100:.1f}%",
            f"毛損益：{tp:+.6f}",f"手續費：{tf:+.6f}",f"淨損益：{tn:+.6f} {E.pnl_emoji(tn)}"]
    else: L.append("今日尚無已平倉交易")
    t=today8(); ts={k:v for k,v in STATS.items() if v.get("date")==t}
    if ts:
        L.append("━━ 各策略今日次數 ━━")
        for k,v in ts.items():
            sym,dr=k.rsplit("_",1)
            L.append(f"{E.dir_emoji(dr)} {sym} {E.dir_word(dr)}：掛{v['placed']} 進{v['entered']}")
    L+=["━━━━━━━━━━",f"時間：{hhmmss()}"]
    await u.message.reply_text("\n".join(L))
async def cmd_coins(u,c):
    on=[s["symbol"] for s in SYMS if s["enabled"]]
    L=[f"{E.BOT} OKXLive普K｜{ACCT}","事件：幣種清單（即時）","━━━━━━━━━━"]
    for sym in on:
        try:
            sp=get_spec(sym); last=get_last(sp["iid"]); mm=sp["minsz"]*sp["ctval"]*last
            L.append(f"{sym}｜最小{sp['minsz']}張｜{mm:.4f}U")
        except: L.append(f"{sym}｜查詢失敗")
    L+=["━━━━━━━━━━",f"時間：{hhmmss()}"]
    await u.message.reply_text("\n".join(L))
async def cmd_timeframe(u,c):
    global ACCOUNT_TF
    if not c.args:
        await u.message.reply_text(f"{E.BOT} 目前週期：{ACCOUNT_TF}\n可選 3m/5m/10m/15m\n變更：/timeframe 10m"); return
    tf=c.args[0]
    if tf not in TF_SEC: await u.message.reply_text(f"{E.BOT} 週期須 3m/5m/10m/15m"); return
    ACCOUNT_TF=tf; save_state()
    await u.message.reply_text(f"{E.BOT} ✅ 帳戶週期已設為 {tf}\n（僅影響之後新建立的策略）")
async def cmd_menu(u,c):
    await u.message.reply_text(f"{E.BOT} OKXLive普K｜{ACCT}\n使用說明\n━━━━━━━━━━\n"
        "/run 商品 方向 槓桿 保證金 埋伏 TP SL TE\n"
        f"例：/run ETHUSDT L 1x 3 0.5 0.5 0.5 180\n　週期依 /timeframe（目前 {ACCOUNT_TF}）\n"
        "　同幣可雙向、多幣可並行\n/confirm 確認啟動\n/stop 商品 方向\n/stopall 停全部+清殘單\n"
        "/status 所有策略現況\n/summary 當日戰報\n/timeframe 查看/設定週期\n/coins 幣種(最小張/最小保證金)\n"
        "━━━━━━━━━━\n⚠ 真實下單，循環交易\n✅ 重啟不失憶（狀態持久化）")
async def cmd_unknown(u,c): await u.message.reply_text(f"{E.BOT} 指令無法辨識：{u.message.text}\n請用 /menu")
async def _post_init(app):
    await app.bot.delete_my_commands()
    await app.bot.set_my_commands([BotCommand("run","建立策略"),BotCommand("confirm","確認啟動"),
        BotCommand("stop","停指定"),BotCommand("stopall","停全部"),BotCommand("status","現況"),
        BotCommand("summary","當日戰報"),BotCommand("timeframe","週期"),BotCommand("coins","幣種"),
        BotCommand("menu","說明")])
    print("左下 Menu 已更新")
    await startup_recover(app)  # 開機認領

def main():
    os.makedirs(os.path.dirname(STATE_FILE),exist_ok=True)
    print(f"啟動 o3333o 普K B5-8 持久化版（token ...{TOKEN[-6:]}）")
    app=Application.builder().token(TOKEN).post_init(_post_init).build()
    for cmd,fn in [(["menu","start"],cmd_menu),("run",cmd_run),("confirm",cmd_confirm),("stop",cmd_stop),
        ("stopall",cmd_stopall),("status",cmd_status),("summary",cmd_summary),
        ("timeframe",cmd_timeframe),("coins",cmd_coins)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(MessageHandler(filters.COMMAND,cmd_unknown))
    app.run_polling()
if __name__=="__main__": main()

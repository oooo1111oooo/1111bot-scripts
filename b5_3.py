#!/usr/bin/env python3
"""B5-3 完整版：只留/menu(刪/help)+左下選單同步+逾時+未知指令+查詢。不真下單。"""
import sys, hmac, base64, hashlib, json, time, asyncio
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import httpx
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

sys.path.insert(0, "/srv/1111bot")
from app.core import emoji as E
from app.core.precision import align_price, calc_size, min_order_usdt
from app.strategy.normal import next_open_epoch, plan_entry, plan_exits, valid_te, TF_SEC

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
PENDING={}

def inst_id(s): return s.replace("USDT","")+"-USDT-SWAP"
def ts_now():
    n=datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.")+f"{n.microsecond//1000:03d}Z"
def sign(sec,ts,m,p,b=""):
    return base64.b64encode(hmac.new(sec.encode(),f"{ts}{m}{p}{b}".encode(),hashlib.sha256).digest()).decode()
def okx_get(path):
    ts=ts_now()
    h={"OK-ACCESS-KEY":ACC[f"OKX_{ACCT}_API_KEY"],
       "OK-ACCESS-SIGN":sign(ACC[f"OKX_{ACCT}_SECRET"],ts,"GET",path),
       "OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":ACC[f"OKX_{ACCT}_PASSPHRASE"],
       "Content-Type":"application/json"}
    return httpx.get(BASE+path,headers=h,timeout=15).json()
def pub_get(p): return httpx.get(BASE+p,timeout=15).json()
def get_spec(sym):
    iid=inst_id(sym)
    d=pub_get(f"/api/v5/public/instruments?instType=SWAP&instId={iid}")["data"][0]
    t=pub_get(f"/api/v5/market/ticker?instId={iid}")["data"][0]
    return {"iid":iid,"tick":Decimal(d["tickSz"]),"lot":Decimal(d["lotSz"]),
            "minsz":Decimal(d["minSz"]),"ctval":Decimal(d["ctVal"]),
            "maxlev":Decimal(d["lever"]),"last":Decimal(t["last"]),"ctvalccy":d["ctValCcy"]}

MENU=(f"{E.BOT} OKXLive普K｜{ACCT}\n事件：使用說明\n━━━━━━━━━━\n"
      "【交易】\n"
      "/run 建立普K策略\n"
      "　商品 方向 週期 槓桿 保證金 埋伏 TP SL TE\n"
      "　例：/run ETHUSDT L 5m 1x 100 0.5 0.7 0.7 180\n"
      "/confirm 60秒內確認 /run\n"
      "/status 查看策略、委託、持倉現況\n"
      "/summary 查看已完成交易與損益\n"
      "【工具】\n"
      "/leverage 查詢最低下單額與最大槓桿\n"
      "　例：/leverage ETHUSDT 1x\n"
      "/timeframe 查看／設定進場週期\n"
      "/coins 查看幣種清單\n"
      "/menu 顯示本說明\n"
      "━━━━━━━━━━\n"
      "⚠ B5-3 測試版：/confirm 尚未真下單")

async def cmd_menu(u,c): await u.message.reply_text(MENU)

async def timeout_watch(app, chat_id, stamp):
    await asyncio.sleep(61)
    p=PENDING.get(chat_id)
    if p and p["t"]==stamp:
        del PENDING[chat_id]
        await app.bot.send_message(chat_id,
            f"{E.BOT} OKXLive普K｜{ACCT}\n事件：參數輸入逾時\n指令：/run\n"
            f"等待時間：60秒\n結果：已取消\n下一步：請重新 /run\n"
            f"時間：{datetime.now(TZ8).strftime('%H:%M:%S')} UTC+8")

async def cmd_run(u,c):
    args=c.args
    fmt=("用法：/run 商品 方向 週期 槓桿 保證金 埋伏 TP SL TE\n"
         "例：/run ETHUSDT L 5m 1x 100 0.5 0.7 0.7 180")
    if len(args)!=9:
        await u.message.reply_text(f"{E.BOT} 參數數量錯誤（需9個）\n{fmt}"); return
    try:
        sym=args[0].upper(); direction=args[1].upper(); tf=args[2]
        lev=Decimal(args[3].replace("x","")); margin=Decimal(args[4])
        offset=Decimal(args[5]); tp=Decimal(args[6]); sl=Decimal(args[7]); te=int(args[8])
    except Exception:
        await u.message.reply_text(f"{E.BOT} 參數格式錯誤\n{fmt}"); return
    errs=[]
    if direction not in ("L","S"): errs.append("方向須 L 或 S")
    if tf not in TF_SEC: errs.append("週期須 3m/5m/10m/15m")
    if not valid_te(te): errs.append("TE 須 1~900 秒")
    if errs:
        await u.message.reply_text(f"{E.BOT} 參數錯誤：\n"+"\n".join("・"+e for e in errs)); return
    try: spec=get_spec(sym)
    except Exception:
        await u.message.reply_text(f"{E.LOSS} 找不到商品 {sym}，確認代號"); return
    if lev>spec["maxlev"]:
        await u.message.reply_text(f"{E.BOT} 槓桿 {lev}x 超過上限 {spec['maxlev']}x"); return
    op=spec["last"]; now=datetime.now(TZ8)
    nopen=datetime.fromtimestamp(next_open_epoch(int(now.timestamp()),tf),TZ8)
    ambush=plan_entry(op,direction,offset,spec["tick"])
    ex=plan_exits(ambush,direction,tp,sl,spec["tick"])
    size=calc_size(margin,lev,ambush,spec["ctval"],spec["lot"])
    notional=size*spec["ctval"]*ambush
    de=E.dir_emoji(direction); dw=E.dir_word(direction)
    if size<spec["minsz"]:
        await u.message.reply_text(
            f"{E.BOT} {E.LOSS} 保證金不足\n{margin} USDT 在 {lev}x 下算出 {size} 張，"
            f"低於最小 {spec['minsz']} 張\n最低約需 {min_order_usdt(spec['minsz'],spec['ctval'],op,lev):.4f} USDT"); return
    msg=(f"{E.BOT} OKXLive普K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
         f"商　　品：{de} {sym} {dw} {lev}x\n週　　期：{tf}\n進場模式：直接埋伏 Maker\n"
         f"開盤時間：{nopen.strftime('%H:%M:%S')}（下一根）\n開盤估價：{op}\n"
         f"埋伏距離：{offset}%\n埋伏價格：{ambush}\n"
         f"止盈 TP：{tp}% → {ex['tp']}\n止損 SL：{sl}% → {ex['sl']}\n持倉 TE：{te}s\n"
         f"未成交：TF邊界前3秒撤單重掛\n保 證 金：{margin} USDT\n下單張數：{size}\n"
         f"名目價值：{notional:.4f} USDT\n━━━━━━━━━━\n下一步：60秒內 /confirm\n"
         f"時間：{now.strftime('%H:%M:%S')} UTC+8")
    stamp=time.time()
    PENDING[u.effective_chat.id]={"t":stamp,"sym":sym,"dir":direction,"tf":tf}
    await u.message.reply_text(msg)
    asyncio.create_task(timeout_watch(c.application,u.effective_chat.id,stamp))

async def cmd_confirm(u,c):
    p=PENDING.get(u.effective_chat.id)
    if not p:
        await u.message.reply_text(f"{E.BOT} 沒有待確認的 /run（可能已逾時或尚未送出）"); return
    if time.time()-p["t"]>60:
        del PENDING[u.effective_chat.id]
        await u.message.reply_text(f"{E.BOT} 確認逾時，請重新 /run"); return
    de=E.dir_emoji(p["dir"]); dw=E.dir_word(p["dir"])
    del PENDING[u.effective_chat.id]
    await u.message.reply_text(
        f"{E.BOT} OKXLive普K｜{ACCT}\n事件：參數確認\n結果：PASS\n━━━━━━━━━━\n"
        f"商　　品：{de} {p['sym']} {dw}\n狀　　態：✅ 已確認\n━━━━━━━━━━\n"
        f"⚠ B5-3 測試版：尚未接上真實下單\n（B5-4 才會真的送單到 OKX）")

async def cmd_status(u,c):
    try:
        bal=okx_get("/api/v5/account/balance"); pos=okx_get("/api/v5/account/positions")
        pend=okx_get("/api/v5/trade/orders-pending")
        eq=avail="?"
        if bal.get("code")=="0":
            det=bal["data"][0].get("details",[])
            x=next((d for d in det if d["ccy"]=="USDT"),None)
            if x:
                eq=f"{Decimal(x.get('eq','0')):.4f}"
                avail=f"{Decimal(x.get('availEq') or x.get('availBal') or '0'):.4f}"
        pl=[p for p in pos.get("data",[]) if float(p.get("pos","0"))!=0] if pos.get("code")=="0" else []
        pdl=pend.get("data",[]) if pend.get("code")=="0" else []
        L=[f"{E.BOT} OKXLive普K｜{ACCT}","事件：帳戶現況（即時查 OKX）","━━━━━━━━━━",
           f"USDT權益：{eq}",f"可用餘額：{avail}",f"持倉數：{len(pl)}",f"掛單數：{len(pdl)}"]
        for p in pl:
            s=E.LONG if p["posSide"]=="long" else E.SHORT; upl=Decimal(p.get('upl','0'))
            L.append(f"{s} {p['instId']} 張{p['pos']} 浮{upl:+.4f}{E.pnl_emoji(upl)}")
        L+=["━━━━━━━━━━",f"時間：{datetime.now(TZ8).strftime('%H:%M:%S')} UTC+8",
            "（策略生命週期狀態於 B5-4 接單後顯示）"]
        await u.message.reply_text("\n".join(L))
    except Exception as e:
        await u.message.reply_text(f"{E.LOSS} 查詢失敗：{type(e).__name__}")

async def cmd_leverage(u,c):
    if len(c.args)<1:
        await u.message.reply_text(f"{E.BOT} 用法：/leverage 商品 [槓桿]\n例：/leverage ETHUSDT 1x"); return
    sym=c.args[0].upper(); lev=Decimal(c.args[1].replace("x","")) if len(c.args)>1 else Decimal(1)
    try:
        spec=get_spec(sym); m=min_order_usdt(spec["minsz"],spec["ctval"],spec["last"],lev)
        L=[f"{E.BOT} OKXLive普K｜{ACCT}","事件：最低下單金額查詢","━━━━━━━━━━",
           f"商　　品：{sym}",f"查詢槓桿：{lev}x",f"標記價格：{spec['last']}",
           f"最小張數：{spec['minsz']}",f"每張面值：{spec['ctval']}{spec['ctvalccy']}",
           f"最低金額：{m:.4f} USDT",f"最大槓桿：{spec['maxlev']}x","━━━━━━━━━━",
           f"時間：{datetime.now(TZ8).strftime('%H:%M:%S')} UTC+8"]
        await u.message.reply_text("\n".join(L))
    except Exception:
        await u.message.reply_text(f"{E.LOSS} 查詢失敗（確認商品代號）")

async def cmd_coins(u,c):
    on=[s["symbol"] for s in SYMS if s["enabled"]]
    L=[f"{E.BOT} OKXLive普K｜{ACCT}","事件：幣種清單","━━━━━━━━━━",f"啟用（{len(on)}）："]+["　"+s for s in on]
    L+=["━━━━━━━━━━","（幣種增刪由維護腳本處理）"]
    await u.message.reply_text("\n".join(L))

async def cmd_timeframe(u,c):
    await u.message.reply_text(
        f"{E.BOT} OKXLive普K｜{ACCT}\n事件：週期設定\n━━━━━━━━━━\n"
        f"目前帳戶週期：5m\n可選：3m / 5m / 10m / 15m\n（設定功能於 B5-5 開放）")

async def cmd_unknown(u,c):
    await u.message.reply_text(
        f"{E.BOT} OKXLive普K｜{ACCT}\n事件：指令無法辨識\n"
        f"你輸入：{u.message.text}\n下一步：請用 /menu 查看可用指令")

async def _set_menu(app):
    # 先刪除舊選單，再設定新的（避免舊殘留）
    await app.bot.delete_my_commands()
    await app.bot.set_my_commands([
        BotCommand("run","建立普K策略"),
        BotCommand("confirm","60秒內確認"),
        BotCommand("status","策略/委託/持倉現況"),
        BotCommand("summary","已完成交易與損益"),
        BotCommand("stop","停止指定策略"),
        BotCommand("stopall","停止全部策略"),
        BotCommand("leverage","最低下單額查詢"),
        BotCommand("timeframe","查看/設定週期"),
        BotCommand("coins","幣種清單"),
        BotCommand("menu","使用說明"),
    ])
    print("左下 Menu 選單已更新")

def main():
    print(f"啟動 o3333o 普K bot B5-3完整版（token ...{TOKEN[-6:]}）")
    app=Application.builder().token(TOKEN).post_init(_set_menu).build()
    app.add_handler(CommandHandler("menu",cmd_menu))
    app.add_handler(CommandHandler("start",cmd_menu))
    app.add_handler(CommandHandler("run",cmd_run))
    app.add_handler(CommandHandler("confirm",cmd_confirm))
    app.add_handler(CommandHandler("status",cmd_status))
    app.add_handler(CommandHandler("leverage",cmd_leverage))
    app.add_handler(CommandHandler("coins",cmd_coins))
    app.add_handler(CommandHandler("timeframe",cmd_timeframe))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.run_polling()

if __name__=="__main__":
    main()

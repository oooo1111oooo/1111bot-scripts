#!/usr/bin/env python3
"""B5-2 查詢指令：/status(權益+可用) /leverage /timeframe /coins。唯讀不下單。"""
import sys, hmac, base64, hashlib, json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

sys.path.insert(0, "/srv/1111bot")
from app.core import emoji as E
from app.core.precision import min_order_usdt

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
def pub_get(path): return httpx.get(BASE+path,timeout=15).json()

HELP=(f"{E.BOT} OKXLive普K｜{ACCT}\n事件：使用說明\n━━━━━━━━━━\n"
      "【交易】\n建立策略：/run\n查看現況：/status\n交易統計：/summary\n"
      "【控制】\n停止策略：/stop\n停止全部：/stopall\n"
      "【工具】\n槓桿查詢：/leverage\n週期設定：/timeframe\n幣種清單：/coins\n"
      "【說明】\n操作選單：/menu\n━━━━━━━━━━\n"
      "（B5-2：/help /status /menu /leverage /timeframe /coins 可用）")

async def cmd_help(u,c): await u.message.reply_text(HELP)

async def cmd_status(u,c):
    try:
        bal=okx_get("/api/v5/account/balance")
        pos=okx_get("/api/v5/account/positions")
        pend=okx_get("/api/v5/trade/orders-pending")
        eq=avail="?"
        if bal.get("code")=="0":
            det=bal["data"][0].get("details",[])
            u_=next((d for d in det if d["ccy"]=="USDT"),None)
            if u_:
                eq=f"{Decimal(u_.get('eq','0')):.4f}"
                avail=f"{Decimal(u_.get('availEq') or u_.get('availBal') or '0'):.4f}"
        pl=[p for p in pos.get("data",[]) if float(p.get("pos","0"))!=0] if pos.get("code")=="0" else []
        pdl=pend.get("data",[]) if pend.get("code")=="0" else []
        L=[f"{E.BOT} OKXLive普K｜{ACCT}","事件：帳戶現況（即時查 OKX）","━━━━━━━━━━",
           f"USDT權益：{eq}",f"可用餘額：{avail}",f"持倉數：{len(pl)}",f"掛單數：{len(pdl)}"]
        if pl:
            L.append("━━ 持倉 ━━")
            for p in pl:
                s=E.LONG if p["posSide"]=="long" else E.SHORT
                upl=Decimal(p.get('upl','0'))
                L.append(f"{s} {p['instId']} 張{p['pos']} 均{p.get('avgPx','?')} 浮{upl:+.4f}{E.pnl_emoji(upl)}")
        if pdl:
            L.append("━━ 掛單 ━━")
            for o in pdl[:10]:
                s=E.LONG if o.get("posSide")=="long" else E.SHORT
                L.append(f"{s} {o['instId']} {o['px']} 張{o['sz']}")
        L+=["━━━━━━━━━━",f"時間：{datetime.now(TZ8).strftime('%H:%M:%S')} UTC+8"]
        await u.message.reply_text("\n".join(L))
    except Exception as e:
        await u.message.reply_text(f"{E.LOSS} 查詢失敗：{type(e).__name__}: {e}")

async def cmd_leverage(u,c):
    args=c.args
    if len(args)<1:
        await u.message.reply_text(f"{E.BOT} 用法：/leverage 商品 [槓桿]\n例：/leverage ETHUSDT 1x")
        return
    sym=args[0].upper()
    lev=Decimal(args[1].replace("x","")) if len(args)>1 else Decimal(1)
    try:
        iid=inst_id(sym)
        d=pub_get(f"/api/v5/public/instruments?instType=SWAP&instId={iid}")["data"][0]
        t=pub_get(f"/api/v5/market/ticker?instId={iid}")["data"][0]
        last=Decimal(t["last"]); minsz=Decimal(d["minSz"]); ctval=Decimal(d["ctVal"])
        m=min_order_usdt(minsz,ctval,last,lev)
        L=[f"{E.BOT} OKXLive普K｜{ACCT}","事件：最低下單金額查詢","━━━━━━━━━━",
           f"商　　品：{sym}",f"查詢槓桿：{lev}x",f"標記價格：{last}",
           f"最小張數：{minsz}",f"每張面值：{ctval}{d['ctValCcy']}",
           f"最低金額：{m:.4f} USDT",f"最大槓桿：{d['lever']}x",
           "━━━━━━━━━━",f"時間：{datetime.now(TZ8).strftime('%H:%M:%S')} UTC+8"]
        await u.message.reply_text("\n".join(L))
    except Exception as e:
        await u.message.reply_text(f"{E.LOSS} 查詢失敗（確認商品代號）：{type(e).__name__}")

async def cmd_timeframe(u,c):
    # 讀帳戶 TF（來自 accounts.env 或預設 5m）
    tf="5m"
    await u.message.reply_text(
        f"{E.BOT} OKXLive普K｜{ACCT}\n事件：週期設定\n━━━━━━━━━━\n"
        f"目前帳戶週期：{tf}\n可選：3m / 5m / 10m / 15m\n"
        f"（B5-2 為唯讀顯示，設定功能後續開放）")

async def cmd_coins(u,c):
    on=[s["symbol"] for s in SYMS if s["enabled"]]
    off=[s["symbol"] for s in SYMS if not s["enabled"]]
    L=[f"{E.BOT} OKXLive普K｜{ACCT}","事件：幣種清單","━━━━━━━━━━",
       f"啟用（{len(on)}）："]+["　"+s for s in on]
    if off: L+=[f"停用（{len(off)}）："]+["　"+s for s in off]
    L+=["━━━━━━━━━━","（幣種增刪由維護腳本處理，此為唯讀查詢）"]
    await u.message.reply_text("\n".join(L))

def main():
    print(f"啟動 o3333o 普K bot B5-2（token ...{TOKEN[-6:]}）")
    print("測試：/status /leverage ETHUSDT 1x /timeframe /coins")
    app=Application.builder().token(TOKEN).build()
    for cmd,fn in [("help",cmd_help),("start",cmd_help),("menu",cmd_help),
                   ("status",cmd_status),("leverage",cmd_leverage),
                   ("timeframe",cmd_timeframe),("coins",cmd_coins)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.run_polling()

if __name__=="__main__":
    main()

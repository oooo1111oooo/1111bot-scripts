#!/usr/bin/env python3
"""B5-1 接通 o3333o 普K bot：/help /status /menu。唯讀，不下單。"""
import sys, hmac, base64, hashlib
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

sys.path.insert(0, "/srv/1111bot")
from app.core import emoji as E

BASE = "https://www.okx.com"
ACCT = "o3333o"
TZ8 = timezone(timedelta(hours=8))

def load_env(path):
    d={}
    with open(path) as f:
        for line in f:
            line=line.strip()
            if "=" in line and not line.startswith("#"):
                k,v=line.split("=",1); d[k]=v
    return d

ACC = load_env("/srv/1111bot/config/accounts.env")
BOTS = load_env("/srv/1111bot/config/bots.env")
TOKEN = BOTS["BOT_o3333o_NORMAL"]

def ts_now():
    n=datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.")+f"{n.microsecond//1000:03d}Z"

def sign(secret,ts,method,path,body=""):
    mac=hmac.new(secret.encode(),f"{ts}{method}{path}{body}".encode(),hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_get(path):
    ts=ts_now()
    h={"OK-ACCESS-KEY":ACC[f"OKX_{ACCT}_API_KEY"],
       "OK-ACCESS-SIGN":sign(ACC[f"OKX_{ACCT}_SECRET"],ts,"GET",path),
       "OK-ACCESS-TIMESTAMP":ts,
       "OK-ACCESS-PASSPHRASE":ACC[f"OKX_{ACCT}_PASSPHRASE"],
       "Content-Type":"application/json"}
    return httpx.get(BASE+path,headers=h,timeout=15).json()

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (f"{E.BOT} OKXLive普K｜{ACCT}\n"
           "事件：使用說明\n"
           "━━━━━━━━━━\n"
           "【交易】\n"
           "建立策略：/run\n"
           "查看現況：/status\n"
           "交易統計：/summary\n"
           "【控制】\n"
           "停止策略：/stop\n"
           "停止全部：/stopall\n"
           "【工具】\n"
           "槓桿查詢：/leverage\n"
           "週期設定：/timeframe\n"
           "幣種清單：/coins\n"
           "【說明】\n"
           "操作選單：/menu\n"
           "━━━━━━━━━━\n"
           "（B5-1 測試版：目前僅 /help /status /menu 可用）")
    await update.message.reply_text(msg)

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_help(update, ctx)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        pos = okx_get("/api/v5/account/positions")
        pend = okx_get("/api/v5/trade/orders-pending")
        bal = okx_get("/api/v5/account/balance")
        usdt="?"
        if bal.get("code")=="0":
            det=bal["data"][0].get("details",[])
            usdt=next((d["eq"] for d in det if d["ccy"]=="USDT"),"0")
            usdt=f"{Decimal(usdt):.4f}"
        poslist=[p for p in pos.get("data",[]) if float(p.get("pos","0"))!=0] if pos.get("code")=="0" else []
        pendlist=pend.get("data",[]) if pend.get("code")=="0" else []
        now=datetime.now(TZ8).strftime("%H:%M:%S")

        lines=[f"{E.BOT} OKXLive普K｜{ACCT}",
               "事件：帳戶現況（即時查 OKX）",
               "━━━━━━━━━━",
               f"USDT權益：{usdt}",
               f"持倉數：{len(poslist)}",
               f"掛單數：{len(pendlist)}"]
        if poslist:
            lines.append("━━ 持倉 ━━")
            for p in poslist:
                side = E.LONG if p["posSide"]=="long" else E.SHORT
                lines.append(f"{side} {p['instId']} 張{p['pos']} 均價{p.get('avgPx','?')}")
        if pendlist:
            lines.append("━━ 掛單 ━━")
            for o in pendlist[:10]:
                side = E.LONG if o.get("posSide")=="long" else E.SHORT
                lines.append(f"{side} {o['instId']} {o['px']} 張{o['sz']}")
        lines.append("━━━━━━━━━━")
        lines.append(f"時間：{now} UTC+8")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"{E.LOSS} 查詢失敗：{type(e).__name__}: {e}")

def main():
    print(f"啟動 o3333o 普K bot（token 尾碼 ...{TOKEN[-6:]}）")
    print("在 TG 對這個 bot 發 /help /status /menu 測試")
    print("停止：Ctrl+C")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("status", cmd_status))
    app.run_polling()

if __name__=="__main__":
    main()

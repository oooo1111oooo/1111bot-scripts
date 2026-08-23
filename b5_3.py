#!/usr/bin/env python3
"""B5-3 /run 預覽 + /confirm 流程。confirm 後不真下單（測試版）。"""
import sys, hmac, base64, hashlib, json, time
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

# 每個 chat 的待確認預覽（60秒有效）
PENDING={}

def inst_id(s): return s.replace("USDT","")+"-USDT-SWAP"
def pub_get(p): return httpx.get(BASE+p,timeout=15).json()
def get_spec(sym):
    iid=inst_id(sym)
    d=pub_get(f"/api/v5/public/instruments?instType=SWAP&instId={iid}")["data"][0]
    t=pub_get(f"/api/v5/market/ticker?instId={iid}")["data"][0]
    return {"iid":iid,"tick":Decimal(d["tickSz"]),"lot":Decimal(d["lotSz"]),
            "minsz":Decimal(d["minSz"]),"ctval":Decimal(d["ctVal"]),
            "maxlev":Decimal(d["lever"]),"last":Decimal(t["last"])}

async def cmd_run(u,c):
    args=c.args
    fmt=("用法：/run 商品 方向 週期 槓桿 保證金 埋伏 TP SL TE\n"
         "例：/run ETHUSDT L 5m 1x 100 0.5 0.7 0.7 180")
    if len(args)!=9:
        await u.message.reply_text(f"{E.BOT} 參數數量錯誤（需9個）\n{fmt}")
        return
    try:
        sym=args[0].upper(); direction=args[1].upper()
        tf=args[2]; lev=Decimal(args[3].replace("x",""))
        margin=Decimal(args[4]); offset=Decimal(args[5])
        tp=Decimal(args[6]); sl=Decimal(args[7]); te=int(args[8])
    except Exception:
        await u.message.reply_text(f"{E.BOT} 參數格式錯誤\n{fmt}")
        return
    # 驗證
    errs=[]
    if direction not in ("L","S"): errs.append("方向須 L 或 S")
    if tf not in TF_SEC: errs.append("週期須 3m/5m/10m/15m")
    if not valid_te(te): errs.append("TE 須 1~900 秒")
    if errs:
        await u.message.reply_text(f"{E.BOT} 參數錯誤：\n"+"\n".join("・"+e for e in errs))
        return
    try:
        spec=get_spec(sym)
    except Exception:
        await u.message.reply_text(f"{E.LOSS} 找不到商品 {sym}，確認代號")
        return
    if lev>spec["maxlev"]:
        await u.message.reply_text(f"{E.BOT} 槓桿 {lev}x 超過上限 {spec['maxlev']}x")
        return

    op=spec["last"]  # 預覽用現價估
    now=datetime.now(TZ8)
    nopen=datetime.fromtimestamp(next_open_epoch(int(now.timestamp()),tf),TZ8)
    ambush=plan_entry(op,direction,offset,spec["tick"])
    ex=plan_exits(ambush,direction,tp,sl,spec["tick"])
    size=calc_size(margin,lev,ambush,spec["ctval"],spec["lot"])
    notional=size*spec["ctval"]*ambush
    de=E.dir_emoji(direction); dw=E.dir_word(direction)

    if size<spec["minsz"]:
        await u.message.reply_text(
            f"{E.BOT} {E.LOSS} 保證金不足\n{margin} USDT 在 {lev}x 下算出 {size} 張，"
            f"低於最小 {spec['minsz']} 張\n最低約需 {min_order_usdt(spec['minsz'],spec['ctval'],op,lev):.4f} USDT")
        return

    msg=(f"{E.BOT} OKXLive普K｜{ACCT}\n事件：交易參數預覽\n━━━━━━━━━━\n"
         f"商　　品：{de} {sym} {dw} {lev}x\n"
         f"週　　期：{tf}\n進場模式：直接埋伏 Maker\n"
         f"開盤時間：{nopen.strftime('%H:%M:%S')}（下一根）\n"
         f"開盤估價：{op}\n"
         f"埋伏距離：{offset}%\n埋伏價格：{ambush}\n"
         f"止盈 TP：{tp}% → {ex['tp']}\n"
         f"止損 SL：{sl}% → {ex['sl']}\n"
         f"持倉 TE：{te}s\n"
         f"未成交：TF邊界前3秒撤單重掛\n"
         f"保 證 金：{margin} USDT\n下單張數：{size}\n"
         f"名目價值：{notional:.4f} USDT\n"
         f"━━━━━━━━━━\n下一步：60秒內 /confirm\n"
         f"時間：{now.strftime('%H:%M:%S')} UTC+8")
    PENDING[u.effective_chat.id]={"t":time.time(),"sym":sym,"dir":direction,"tf":tf}
    await u.message.reply_text(msg)

async def cmd_confirm(u,c):
    p=PENDING.get(u.effective_chat.id)
    if not p:
        await u.message.reply_text(f"{E.BOT} 沒有待確認的 /run，請先送出參數")
        return
    if time.time()-p["t"]>60:
        del PENDING[u.effective_chat.id]
        await u.message.reply_text(f"{E.BOT} 確認逾時（超過60秒），請重新 /run")
        return
    de=E.dir_emoji(p["dir"]); dw=E.dir_word(p["dir"])
    del PENDING[u.effective_chat.id]
    await u.message.reply_text(
        f"{E.BOT} OKXLive普K｜{ACCT}\n事件：參數確認\n結果：PASS\n━━━━━━━━━━\n"
        f"商　　品：{de} {p['sym']} {dw}\n"
        f"狀　　態：✅ 已確認\n"
        f"━━━━━━━━━━\n"
        f"⚠ B5-3 測試版：尚未接上真實下單\n（B5-4 才會真的送單到 OKX）")

async def cmd_help(u,c):
    await u.message.reply_text(
        f"{E.BOT} OKXLive普K｜{ACCT}\n事件：使用說明（B5-3）\n━━━━━━━━━━\n"
        "/run 商品 方向 週期 槓桿 保證金 埋伏 TP SL TE\n"
        "例：/run ETHUSDT L 5m 1x 100 0.5 0.7 0.7 180\n"
        "/confirm 60秒內確認\n"
        "/status /leverage /coins /timeframe\n"
        "━━━━━━━━━━\n⚠ confirm 尚未真下單（測試版）")

def main():
    print(f"啟動 o3333o 普K bot B5-3（token ...{TOKEN[-6:]}）")
    print("測試：/run ETHUSDT L 5m 1x 100 0.5 0.7 0.7 180 → /confirm")
    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["help","start","menu"],cmd_help))
    app.add_handler(CommandHandler("run",cmd_run))
    app.add_handler(CommandHandler("confirm",cmd_confirm))
    # 沿用 B5-2 的查詢指令
    from importlib import import_module
    app.run_polling()

if __name__=="__main__":
    main()

#!/usr/bin/env python3
"""一次性戰報：查 OKX o3333o 已平倉部位歷史，統計損益。"""
import hmac, base64, hashlib
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import httpx
ENVFILE="/srv/1111bot/config/accounts.env"; BASE="https://www.okx.com"; ACCT="o3333o"
TZ8=timezone(timedelta(hours=8))
def load_env():
    d={}
    for line in open(ENVFILE):
        line=line.strip()
        if "=" in line and not line.startswith("#"):
            k,v=line.split("=",1); d[k]=v
    return d
ACC=load_env()
def ts_now():
    n=datetime.now(timezone.utc); return n.strftime("%Y-%m-%dT%H:%M:%S.")+f"{n.microsecond//1000:03d}Z"
def sign(sec,ts,m,p,b=""):
    return base64.b64encode(hmac.new(sec.encode(),f"{ts}{m}{p}{b}".encode(),hashlib.sha256).digest()).decode()
def okx_get(path):
    ts=ts_now()
    h={"OK-ACCESS-KEY":ACC[f"OKX_{ACCT}_API_KEY"],"OK-ACCESS-SIGN":sign(ACC[f"OKX_{ACCT}_SECRET"],ts,"GET",path),
       "OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":ACC[f"OKX_{ACCT}_PASSPHRASE"],"Content-Type":"application/json"}
    return httpx.get(BASE+path,headers=h,timeout=20).json()
def t8(ms): return datetime.fromtimestamp(int(ms)/1000,TZ8).strftime("%m-%d %H:%M")
def main():
    r=okx_get("/api/v5/account/positions-history?instType=SWAP&limit=100")
    if r.get("code")!="0": print("查詢失敗:",r); return
    data=r["data"]
    print("="*58)
    print(f"OKX {ACCT} 已平倉戰報（最近 {len(data)} 筆，時間UTC+8）")
    print("="*58)
    print(f"{'平倉時間':<13}{'商品':<14}{'方向':<7}{'淨損益':>14}")
    print("-"*58)
    tp=tf=tn=Decimal(0); win=loss=even=0
    for p in reversed(data):
        net=Decimal(p.get("realizedPnl") or "0"); pnl=Decimal(p.get("pnl") or "0")
        fee=Decimal(p.get("fee") or "0")+Decimal(p.get("fundingFee") or "0")
        tp+=pnl; tf+=fee; tn+=net
        win+=net>0; loss+=net<0; even+=net==0
        side="Long" if p.get("posSide")=="long" else "Short"
        print(f"{t8(p.get('uTime')):<13}{p.get('instId',''):<14}{side:<7}{net:>+14.6f}")
    n=len(data); wr=(win/n*100) if n else 0
    print("-"*58)
    print(f"總筆數:{n}  獲利:{win}  虧損:{loss}  打平:{even}  勝率:{wr:.1f}%")
    print(f"毛損益:{tp:+.6f}  手續費:{tf:+.6f}  淨損益:{tn:+.6f}")
    print("="*58)
if __name__=="__main__": main()

#!/usr/bin/env python3
"""緊急清場：直接對 OKX o3333o 撤所有掛單 + 平所有持倉。不依賴程式記憶。"""
import hmac, base64, hashlib, json
from datetime import datetime, timezone
import httpx
ENVFILE="/srv/1111bot/config/accounts.env"; BASE="https://www.okx.com"; ACCT="o3333o"
def load_env():
    d={}
    for line in open(ENVFILE):
        line=line.strip()
        if "=" in line and not line.startswith("#"): k,v=line.split("=",1); d[k]=v
    return d
ACC=load_env()
def ts_now():
    n=datetime.now(timezone.utc); return n.strftime("%Y-%m-%dT%H:%M:%S.")+f"{n.microsecond//1000:03d}Z"
def sign(sec,ts,m,p,b=""):
    return base64.b64encode(hmac.new(sec.encode(),f"{ts}{m}{p}{b}".encode(),hashlib.sha256).digest()).decode()
def api(method,path,body=None):
    b=json.dumps(body) if body else ""; ts=ts_now()
    h={"OK-ACCESS-KEY":ACC[f"OKX_{ACCT}_API_KEY"],"OK-ACCESS-SIGN":sign(ACC[f"OKX_{ACCT}_SECRET"],ts,method,path,b),
       "OK-ACCESS-TIMESTAMP":ts,"OK-ACCESS-PASSPHRASE":ACC[f"OKX_{ACCT}_PASSPHRASE"],"Content-Type":"application/json"}
    return httpx.request(method,BASE+path,headers=h,content=b,timeout=15).json()

print("="*50)
print(f"緊急清場 {ACCT}")
print("="*50)

# 1. 撤所有掛單
print("\n[1] 撤銷所有掛單")
pend=api("GET","/api/v5/trade/orders-pending")
orders=pend.get("data",[]) if pend.get("code")=="0" else []
print(f"  找到 {len(orders)} 筆掛單")
for o in orders:
    r=api("POST","/api/v5/trade/cancel-order",{"instId":o["instId"],"ordId":o["ordId"]})
    ok="✓" if r.get("code")=="0" else "✗"
    print(f"  {ok} 撤 {o['instId']} {o.get('posSide','')} px={o['px']} 張={o['sz']}")

# 2. 平所有持倉（市價反向）
print("\n[2] 平所有持倉（市價）")
pos=api("GET","/api/v5/account/positions")
positions=[p for p in pos.get("data",[]) if float(p.get("pos","0"))!=0] if pos.get("code")=="0" else []
print(f"  找到 {len(positions)} 個持倉")
for p in positions:
    posSide=p["posSide"]; sz=p["pos"]
    close_side="sell" if posSide=="long" else "buy"
    r=api("POST","/api/v5/trade/order",{"instId":p["instId"],"tdMode":"isolated",
        "side":close_side,"posSide":posSide,"ordType":"market","sz":str(abs(float(sz)))})
    ok="✓" if r.get("code")=="0" else "✗"
    print(f"  {ok} 平 {p['instId']} {posSide} 張={sz} → {r.get('data',[{}])[0].get('sMsg',r.get('msg',''))}")

# 3. 確認清乾淨
print("\n[3] 確認")
import time; time.sleep(2)
pend2=api("GET","/api/v5/trade/orders-pending")
pos2=api("GET","/api/v5/account/positions")
n_ord=len(pend2.get("data",[])) if pend2.get("code")=="0" else -1
n_pos=len([p for p in pos2.get("data",[]) if float(p.get("pos","0"))!=0]) if pos2.get("code")=="0" else -1
print(f"  剩餘掛單：{n_ord}")
print(f"  剩餘持倉：{n_pos}")
print("="*50)
print("✓ 清場完成" if n_ord==0 and n_pos==0 else "⚠ 仍有殘留，請至 OKX 手動檢查")

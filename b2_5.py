#!/usr/bin/env python3
"""B2-5 冪等測試：同一 clOrdId 送兩次，確認 OKX 只成立一張。o3333o ETH -10% 測完即撤。"""
import hmac, base64, hashlib, json, time, uuid
from decimal import Decimal, ROUND_FLOOR
from datetime import datetime, timezone
import httpx

ENVFILE = "/srv/1111bot/config/accounts.env"
BASE = "https://www.okx.com"
ACCT = "o3333o"
INST = "ETH-USDT-SWAP"

def load_env():
    d={}
    with open(ENVFILE) as f:
        for line in f:
            line=line.strip()
            if "=" in line and not line.startswith("#"):
                k,v=line.split("=",1); d[k]=v
    return d

def ts_now():
    n=datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.")+f"{n.microsecond//1000:03d}Z"

def sign(secret,ts,method,path,body=""):
    mac=hmac.new(secret.encode(),f"{ts}{method}{path}{body}".encode(),hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def api(env,acct,method,path,body_obj=None):
    body=json.dumps(body_obj) if body_obj else ""
    ts=ts_now()
    h={"OK-ACCESS-KEY":env[f"OKX_{acct}_API_KEY"],
       "OK-ACCESS-SIGN":sign(env[f"OKX_{acct}_SECRET"],ts,method,path,body),
       "OK-ACCESS-TIMESTAMP":ts,
       "OK-ACCESS-PASSPHRASE":env[f"OKX_{acct}_PASSPHRASE"],
       "Content-Type":"application/json"}
    return httpx.request(method,BASE+path,headers=h,content=body,timeout=15).json()

def main():
    env=load_env()

    print("="*52)
    print("[1] 抓現價，算 -10% 掛單價")
    d=httpx.get(BASE+f"/api/v5/public/instruments?instType=SWAP&instId={INST}",timeout=15).json()["data"][0]
    tick=Decimal(d["tickSz"]); minsz=Decimal(d["minSz"])
    last=Decimal(httpx.get(BASE+f"/api/v5/market/ticker?instId={INST}",timeout=15).json()["data"][0]["last"])
    px=(last*Decimal("0.9")/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
    print(f"  現價={last} 掛單價={px} 張數={minsz}")

    # 關鍵：兩次都用同一個 clOrdId
    cl_id="b25idem"+uuid.uuid4().hex[:12]
    order={"instId":INST,"tdMode":"isolated","side":"buy","posSide":"long",
           "ordType":"limit","px":str(px),"sz":str(minsz),"clOrdId":cl_id}
    print(f"  共用 clOrdId={cl_id}")

    print("\n[2] 第一次送單")
    r1=api(env,ACCT,"POST","/api/v5/trade/order",order)
    print("  回應:",json.dumps(r1,ensure_ascii=False))
    ord1=r1["data"][0].get("ordId") if r1.get("code")=="0" else None
    code1=r1["data"][0].get("sCode")
    print(f"  → sCode={code1} ordId={ord1}")

    print("\n[3] 第二次送單（相同 clOrdId，模擬重送）")
    r2=api(env,ACCT,"POST","/api/v5/trade/order",order)
    print("  回應:",json.dumps(r2,ensure_ascii=False))
    ord2=r2["data"][0].get("ordId") if r2.get("data") else None
    code2=r2["data"][0].get("sCode") if r2.get("data") else r2.get("code")
    smsg2=r2["data"][0].get("sMsg") if r2.get("data") else r2.get("msg")
    print(f"  → sCode={code2} ordId={ord2} msg={smsg2}")

    print("\n[4] 判定冪等性")
    idempotent = (ord2 is None or ord2=="" or ord2==ord1 or code2!="0")
    if idempotent:
        print("  ✓ 第二次被擋（重複 clOrdId 未產生新單）")
    else:
        print(f"  ✗ 危險：第二次產生了不同的單 ord2={ord2}")

    print("\n[5] 查詢實際掛單數（應只有 1 張）")
    time.sleep(1)
    r=api(env,ACCT,"GET",f"/api/v5/trade/orders-pending?instId={INST}")
    mine=[o for o in r.get("data",[]) if o.get("clOrdId")==cl_id]
    print(f"  clOrdId={cl_id} 的掛單數: {len(mine)}")

    print("\n[6] 撤掉所有本測試掛單")
    cancelled=0
    for o in mine:
        cr=api(env,ACCT,"POST","/api/v5/trade/cancel-order",{"instId":INST,"ordId":o["ordId"]})
        if cr.get("code")=="0": cancelled+=1
    print(f"  已撤 {cancelled} 張")

    print("\n[7] 確認無殘留、無持倉")
    time.sleep(1)
    r=api(env,ACCT,"GET",f"/api/v5/trade/orders-pending?instId={INST}")
    left=[o for o in r.get("data",[]) if o.get("clOrdId")==cl_id]
    pr=api(env,ACCT,"GET","/api/v5/account/positions")
    pos=[p for p in pr.get("data",[]) if float(p.get("pos","0"))!=0]
    print(f"  殘留掛單: {'✗ '+str(len(left)) if left else '✓ 無'}")
    print(f"  持倉: {'✗ 有' if pos else '✓ 無'}")

    print("\n"+"="*52)
    if idempotent and len(mine)<=1 and not left and not pos:
        print("✓ B2-5 通過：冪等有效，重送不會產生第二張單")
    else:
        print("⚠ 請檢查上面結果")

if __name__=="__main__":
    main()

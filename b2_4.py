#!/usr/bin/env python3
"""B2-4 第一次 live 下單測試：o3333o ETH 掛 -10% 限價 → 立即撤單。"""
import hmac, base64, hashlib, json, time, uuid
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR
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
    body = json.dumps(body_obj) if body_obj else ""
    ts=ts_now()
    h={"OK-ACCESS-KEY":env[f"OKX_{acct}_API_KEY"],
       "OK-ACCESS-SIGN":sign(env[f"OKX_{acct}_SECRET"],ts,method,path,body),
       "OK-ACCESS-TIMESTAMP":ts,
       "OK-ACCESS-PASSPHRASE":env[f"OKX_{acct}_PASSPHRASE"],
       "Content-Type":"application/json"}
    r=httpx.request(method,BASE+path,headers=h,content=body,timeout=15)
    return r.json()

def main():
    env=load_env()

    print("="*50)
    print("[1] 即時抓 ETH 規格與現價")
    r=httpx.get(BASE+f"/api/v5/public/instruments?instType=SWAP&instId={INST}",timeout=15).json()
    d=r["data"][0]
    tick=Decimal(d["tickSz"]); lot=Decimal(d["lotSz"]); minsz=Decimal(d["minSz"]); ctval=Decimal(d["ctVal"])
    t=httpx.get(BASE+f"/api/v5/market/ticker?instId={INST}",timeout=15).json()
    last=Decimal(t["data"][0]["last"])
    # 掛單價 = 現價 -10%，向下對齊 tickSz
    raw=last*Decimal("0.9")
    px=(raw/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
    size=minsz  # 最小張
    notional=size*ctval*last
    print(f"  現價={last}  tickSz={tick}  最小張={minsz}")
    print(f"  掛單價(-10%)={px}  張數={size}")
    print(f"  名目價值≈{notional:.4f} USDT (1x 保證金)")

    print("\n[2] 即將下單，參數如下：")
    print(f"  帳戶={ACCT} 商品={INST} 方向=Long limit 逐倉 1x")
    print(f"  價格={px} 張數={size}")
    cl_id="b24test"+uuid.uuid4().hex[:12]
    print(f"  client_order_id={cl_id}")
    print("  >>> 3 秒後送出，要喊停按 Ctrl+C <<<")
    time.sleep(3)

    print("\n[3] 下單中...")
    order={
        "instId":INST,"tdMode":"isolated","side":"buy","posSide":"long",
        "ordType":"limit","px":str(px),"sz":str(size),"clOrdId":cl_id
    }
    r=api(env,ACCT,"POST","/api/v5/trade/order",order)
    print("  OKX 回應:", json.dumps(r,ensure_ascii=False))
    if r.get("code")!="0":
        print("  ✗ 下單失敗，停止")
        return
    ord_id=r["data"][0]["ordId"]
    print(f"  ✓ 下單成功 ordId={ord_id}")

    print("\n[4] 查詢掛單確認存在")
    time.sleep(1)
    r=api(env,ACCT,"GET",f"/api/v5/trade/orders-pending?instId={INST}")
    found=[o for o in r.get("data",[]) if o["ordId"]==ord_id]
    if found:
        o=found[0]
        print(f"  ✓ 找到掛單 價格={o['px']} 張數={o['sz']} 狀態={o['state']}")
    else:
        print("  ⚠ 掛單列表沒找到（可能已成交？立即檢查）")

    print("\n[5] 立即撤單")
    r=api(env,ACCT,"POST","/api/v5/trade/cancel-order",{"instId":INST,"ordId":ord_id})
    print("  OKX 回應:", json.dumps(r,ensure_ascii=False))
    if r.get("code")=="0":
        print("  ✓ 撤單指令送出成功")

    print("\n[6] 確認撤單完成、無殘留、無持倉")
    time.sleep(1)
    r=api(env,ACCT,"GET",f"/api/v5/trade/orders-pending?instId={INST}")
    pending=[o for o in r.get("data",[]) if o["ordId"]==ord_id]
    print(f"  殘留掛單: {'✗ 還有！'+str(pending) if pending else '✓ 無'}")
    r=api(env,ACCT,"GET","/api/v5/account/positions")
    pos=[p for p in r.get("data",[]) if float(p.get("pos","0"))!=0]
    print(f"  持倉: {'✗ 有倉！'+str([(p['instId'],p['pos']) for p in pos]) if pos else '✓ 無'}")

    print("\n"+"="*50)
    if not pending and not pos:
        print("✓ B2-4 通過：下單→查詢→撤單 全鏈路正常，無殘留無持倉")
    else:
        print("⚠ 有殘留，請至 OKX 檢查")

if __name__=="__main__":
    main()

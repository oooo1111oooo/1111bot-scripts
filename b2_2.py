#!/usr/bin/env python3
"""B2-2 查詢能力：持倉、掛單、9檔規格驗證。不下單。"""
import hmac, base64, hashlib
from datetime import datetime, timezone
import httpx

ENVFILE = "/srv/1111bot/config/accounts.env"
BASE = "https://www.okx.com"
ACCOUNTS = ["o2222o", "o3333o", "o4444o", "o5555o"]
SYMBOLS = ["BTCUSDT","ETHUSDT","DOGEUSDT","HYPEUSDT","SOLUSDT",
           "SUIUSDT","XRPUSDT","XAUUSDT","ADAUSDT"]

def inst_id(sym):  # BTCUSDT -> BTC-USDT-SWAP
    return sym.replace("USDT","") + "-USDT-SWAP"

def load_env():
    d = {}
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

def priv(env,acct,method,path):
    ts=ts_now()
    h={"OK-ACCESS-KEY":env[f"OKX_{acct}_API_KEY"],
       "OK-ACCESS-SIGN":sign(env[f"OKX_{acct}_SECRET"],ts,method,path),
       "OK-ACCESS-TIMESTAMP":ts,
       "OK-ACCESS-PASSPHRASE":env[f"OKX_{acct}_PASSPHRASE"],
       "Content-Type":"application/json"}
    return httpx.request(method,BASE+path,headers=h,timeout=15).json()

def main():
    env=load_env()

    print("="*46)
    print("[1] 四帳戶目前持倉（應該都無倉）")
    for acct in ACCOUNTS:
        r=priv(env,acct,"GET","/api/v5/account/positions")
        if r.get("code")=="0":
            pos=[p for p in r["data"] if float(p.get("pos","0"))!=0]
            if pos:
                for p in pos:
                    print(f"  {acct}: {p['instId']} {p['posSide']} 張數={p['pos']} 未實現={p.get('upl','?')}")
            else:
                print(f"  ✓ {acct}: 無持倉")
        else:
            print(f"  ✗ {acct}: {r.get('msg')}")

    print("\n[2] 四帳戶未成交掛單（應該都無單）")
    for acct in ACCOUNTS:
        r=priv(env,acct,"GET","/api/v5/trade/orders-pending")
        if r.get("code")=="0":
            n=len(r["data"])
            print(f"  ✓ {acct}: {n} 筆掛單" if n else f"  ✓ {acct}: 無掛單")
        else:
            print(f"  ✗ {acct}: {r.get('msg')}")

    print("\n[3] 9 檔幣種規格（下單精度依據）")
    print(f"  {'幣種':<10}{'最小張':<10}{'張增量':<10}{'面值':<12}{'最大槓桿'}")
    ok=0; missing=[]
    for sym in SYMBOLS:
        iid=inst_id(sym)
        r=httpx.get(BASE+f"/api/v5/public/instruments?instType=SWAP&instId={iid}",timeout=15).json()
        if r.get("code")=="0" and r["data"]:
            d=r["data"][0]
            print(f"  {sym:<10}{d['minSz']:<10}{d['lotSz']:<10}{d['ctVal']+d['ctValCcy']:<12}{d['lever']}")
            ok+=1
        else:
            print(f"  {sym:<10}✗ 找不到此合約")
            missing.append(sym)

    print("\n[4] XAUUSDT 交易時段檢查")
    r=httpx.get(BASE+"/api/v5/public/instruments?instType=SWAP&instId=XAU-USDT-SWAP",timeout=15).json()
    if r.get("code")=="0" and r["data"]:
        d=r["data"][0]
        print(f"  狀態: {d.get('state')} | 到期: {d.get('expTime') or '永續'}")
        print("  註：若 state=live 表示可交易；貴金屬連續性待實盤觀察")
    else:
        print("  ✗ XAU-USDT-SWAP 查詢失敗（可能此合約在 OKX 不存在或代號不同）")

    print("\n"+"="*46)
    print(f"規格取得：{ok}/9 檔")
    if missing:
        print("⚠ 找不到的幣種:", ", ".join(missing), "（可能代號不同，需確認）")
    else:
        print("✓ B2-2 通過：查詢能力正常，9 檔規格齊全")

if __name__=="__main__":
    main()

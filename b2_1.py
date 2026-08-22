#!/usr/bin/env python3
"""B2-1 唯讀連線測試：查時間、查餘額、查行情。不下單。"""
import sys, hmac, base64, hashlib, json
from datetime import datetime, timezone
import httpx

ENVFILE = "/srv/1111bot/config/accounts.env"
BASE = "https://www.okx.com"
ACCOUNTS = ["o2222o", "o3333o", "o4444o", "o5555o"]

def load_env():
    d = {}
    with open(ENVFILE) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                d[k] = v
    return d

def ts_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
           f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"

def sign(secret, ts, method, path, body=""):
    msg = f"{ts}{method}{path}{body}"
    mac = hmac.new(secret.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def req(env, acct, method, path):
    key = env[f"OKX_{acct}_API_KEY"]
    secret = env[f"OKX_{acct}_SECRET"]
    passph = env[f"OKX_{acct}_PASSPHRASE"]
    ts = ts_now()
    headers = {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": sign(secret, ts, method, path),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passph,
        "Content-Type": "application/json",
    }
    r = httpx.request(method, BASE + path, headers=headers, timeout=15)
    return r.json()

def main():
    env = load_env()
    print("=" * 40)

    # 1. 公開端點：伺服器時間（不需簽名，測基本連線）
    print("[1] OKX 伺服器時間（測基本連線）")
    r = httpx.get(BASE + "/api/v5/public/time", timeout=15).json()
    if r.get("code") == "0":
        srv_ms = int(r["data"][0]["ts"])
        srv = datetime.fromtimestamp(srv_ms/1000, timezone.utc)
        print(f"  ✓ 連線成功，OKX 時間 {srv.strftime('%H:%M:%S')} UTC")
    else:
        print("  ✗ 連線失敗:", r); sys.exit(1)

    # 2. 公開端點：BTC 行情
    print("\n[2] BTC-USDT-SWAP 行情")
    r = httpx.get(BASE + "/api/v5/market/ticker?instId=BTC-USDT-SWAP", timeout=15).json()
    if r.get("code") == "0":
        print(f"  ✓ BTC 最新價 {r['data'][0]['last']} USDT")
    else:
        print("  ✗ 失敗:", r)

    # 3. 私有端點：四帳戶餘額（測簽名 + key 有效性）
    print("\n[3] 四帳戶餘額查詢（測金鑰簽名）")
    ok = 0
    for acct in ACCOUNTS:
        try:
            r = req(env, acct, "GET", "/api/v5/account/balance")
            if r.get("code") == "0":
                details = r["data"][0].get("details", [])
                usdt = next((d["eq"] for d in details if d["ccy"] == "USDT"), "0")
                print(f"  ✓ {acct}: 連線OK，USDT 權益 ≈ {usdt}")
                ok += 1
            else:
                print(f"  ✗ {acct}: OKX 回傳錯誤 code={r.get('code')} msg={r.get('msg')}")
        except Exception as e:
            print(f"  ✗ {acct}: 例外 {type(e).__name__}: {e}")

    print("\n" + "=" * 40)
    print(f"結果：{ok}/4 帳戶連線成功")
    if ok == 4:
        print("✓ B2-1 通過：四把金鑰全部有效，簽名正確")
    else:
        print("✗ 部分帳戶失敗，請看上面錯誤訊息")

if __name__ == "__main__":
    main()

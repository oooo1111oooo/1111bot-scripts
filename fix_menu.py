#!/usr/bin/env python3
"""強制重設 o3333o 普K bot 的左下選單。獨立執行，與 bot 啟動無關。"""
import json, httpx
def load_env(p):
    d={}
    for line in open(p):
        line=line.strip()
        if "=" in line and not line.startswith("#"):
            k,v=line.split("=",1); d[k]=v
    return d
TOKEN=load_env("/srv/1111bot/config/bots.env")["BOT_o3333o_NORMAL"]
API=f"https://api.telegram.org/bot{TOKEN}"

# 1. 先徹底刪除舊選單（所有 scope）
print("[1] 刪除舊選單...")
for scope in [None, {"type":"default"}, {"type":"all_private_chats"}]:
    body={} if scope is None else {"scope":scope}
    r=httpx.post(f"{API}/deleteMyCommands",json=body,timeout=15).json()
    print(f"  delete scope={scope}: {r.get('ok')}")

# 2. 設定新選單（default scope）
print("[2] 設定新選單...")
cmds=[
    {"command":"run","description":"建立策略"},
    {"command":"confirm","description":"確認啟動"},
    {"command":"stop","description":"停指定"},
    {"command":"stopall","description":"停全部"},
    {"command":"status","description":"現況"},
    {"command":"summary","description":"當日戰報"},
    {"command":"timeframe","description":"週期"},
    {"command":"coins","description":"幣種(含槓桿/最低額)"},
    {"command":"menu","description":"說明"},
]
r=httpx.post(f"{API}/setMyCommands",json={"commands":cmds},timeout=15).json()
print(f"  setMyCommands: {r.get('ok')}")

# 3. 讀回確認
print("[3] 確認目前選單：")
r=httpx.get(f"{API}/getMyCommands",timeout=15).json()
for c in r.get("result",[]):
    print(f"  /{c['command']} - {c['description']}")
print("完成。請關閉 TG App 重開，選單即更新（有快取）")

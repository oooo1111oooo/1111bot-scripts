#!/usr/bin/env bash
set -e
ROOT=/srv/1111bot
s(){ echo; echo "########## $1 ##########"; }

s "0 安裝 tmux"
if command -v tmux >/dev/null 2>&1; then echo "tmux 已安裝: $(tmux -V)"; else sudo apt-get install -y -q tmux >/dev/null 2>&1 && echo "tmux 安裝完成: $(tmux -V)"; fi

s "1 global.env.example（全域設定範本，不含機密）"
cat > $ROOT/config/global.env.example <<'ENV'
# 全域設定範本。實際使用時複製為 global.env 並填值。
TZ=Asia/Taipei
# 顯示精度
TG_PRICE_DP=6
TG_AMOUNT_DP=6
TG_PCT_DP=4
XLSX_PRICE_DP=6
XLSX_AMOUNT_DP=8
XLSX_PCT_DP=6
# 報表
REPORT_EMAIL_TO=a0936880936@gmail.com
REPORT_HOUR=0
REPORT_MINUTE=5
# Watchdog 對帳間隔（秒），實際值 B3 決定
RECON_INTERVAL_SEC=30
ENV
echo "已建立 global.env.example"

s "2 accounts.example.json（4 帳戶，金鑰留空）"
cat > $ROOT/config/accounts.example.json <<'JSON'
{
  "o2222o": { "api_key": "<FILL_ME>", "api_secret": "<FILL_ME>", "passphrase": "<FILL_ME>", "timeframe": "5m" },
  "o3333o": { "api_key": "<FILL_ME>", "api_secret": "<FILL_ME>", "passphrase": "<FILL_ME>", "timeframe": "5m" },
  "o4444o": { "api_key": "<FILL_ME>", "api_secret": "<FILL_ME>", "passphrase": "<FILL_ME>", "timeframe": "5m" },
  "o5555o": { "api_key": "<FILL_ME>", "api_secret": "<FILL_ME>", "passphrase": "<FILL_ME>", "timeframe": "5m" }
}
JSON
echo "已建立 accounts.example.json"

s "3 bots.example.json（8 bot token 留空）"
cat > $ROOT/config/bots.example.json <<'JSON'
{
  "o2222o": { "normal": "<FILL_ME>", "ha": "<FILL_ME>" },
  "o3333o": { "normal": "<FILL_ME>", "ha": "<FILL_ME>" },
  "o4444o": { "normal": "<FILL_ME>", "ha": "<FILL_ME>" },
  "o5555o": { "normal": "<FILL_ME>", "ha": "<FILL_ME>" }
}
JSON
echo "已建立 bots.example.json"

s "4 symbols.json（9 檔幣種，可直接使用）"
cat > $ROOT/config/symbols.json <<'JSON'
{
  "symbols": [
    { "symbol": "BTCUSDT",  "enabled": true,  "note": "" },
    { "symbol": "ETHUSDT",  "enabled": true,  "note": "" },
    { "symbol": "DOGEUSDT", "enabled": true,  "note": "" },
    { "symbol": "HYPEUSDT", "enabled": true,  "note": "demo API 異常 live 正常" },
    { "symbol": "SOLUSDT",  "enabled": true,  "note": "" },
    { "symbol": "SUIUSDT",  "enabled": true,  "note": "" },
    { "symbol": "XRPUSDT",  "enabled": true,  "note": "" },
    { "symbol": "XAUUSDT",  "enabled": true,  "note": "貴金屬 須驗證交易時段" },
    { "symbol": "ADAUSDT",  "enabled": true,  "note": "v1.4 新增 待驗證" }
  ]
}
JSON
echo "已建立 symbols.json"

s "5 驗證 JSON 格式正確"
$ROOT/.venv/bin/python - <<PY
import json
for f in ["accounts.example.json","bots.example.json","symbols.json"]:
    p="$ROOT/config/"+f
    with open(p) as fh: json.load(fh)
    print("OK:", f)
syms=json.load(open("$ROOT/config/symbols.json"))["symbols"]
print("幣種數:", len(syms), "| 啟用:", sum(1 for x in syms if x["enabled"]))
PY

s "6 確認機密範本規則"
echo "檢查 .gitignore 是否會擋掉真實 .env（非 .example）"
cd $ROOT
echo "TEST=1" > config/accounts.env
if git status -s | grep -q 'accounts.env'; then echo "DANGER: .env 沒被擋"; else echo "OK: 真實 .env/.json 機密不會進版控"; fi
rm -f config/accounts.env

s "7 commit 範本（.example 與 symbols.json 可進版控，因無機密）"
cd $ROOT
git add config/global.env.example config/accounts.example.json config/bots.example.json config/symbols.json
git commit -q -m "B1-5: 設定檔範本與幣種池"
git log --oneline

s "8 tmux 快速教學"
echo "以後跑長任務前，先打： tmux"
echo "斷線後重新連上，打： tmux attach"
echo "工作會一直在背景跑，不怕斷線"

s END

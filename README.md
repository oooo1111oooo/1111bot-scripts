# 1111bot — OKX 多帳戶 Telegram bot

## 帳戶
- **o1111o**（OKX 主帳戶）：測試帳戶，跑測試版 `run_bot_next.py`。新版本先在這裡測。
- **o2222o～o6666o**（OKX 子帳戶）：跑正式版 `run_bot.py`。o1111o 測過才推過來。
- 每個帳戶各自一個 TG bot、一組 OKX API Key（只在 VPS 的 config，不進 GitHub）。

## 檔案
- `run_bot.py`：bot 本體（6 個帳戶同一份程式）。規則與每一版的改動寫在檔案裡「/runt 佈局策略」、「/run 實盤」兩區的註解。
- `deploy.sh`：VPS 部署腳本（2026-10-08 起取代 deploy5.sh）。
- `setup_o1111o.sh`：o1111o 設定（在 VPS 上輸入四個值；再跑一次＝換 API Key／token）。
- `app/core/emoji.py`：畫面用的表情符號（run_bot.py 會載入）。
- `requirements.txt`：Python 套件。

## 部署（先測 o1111o，再推 5 個子帳戶）
1. GitHub 網頁上傳 `run_bot.py` 到 main，確認時間顯示「now」。
2. VPS：`bash /srv/1111bot/deploy.sh test <行數> <版本>`
   → 抓 GitHub、驗證行數／版本／語法，只更新 o1111o 並重啟；o1111o 起不來會自動退回正式版。
3. TG 上用 o1111o 測。
4. 沒問題：`bash /srv/1111bot/deploy.sh all` → 推到 o2222o～o6666o（舊版備份 run_bot.py.bak）。
   有問題：`bash /srv/1111bot/deploy.sh rollback`
   （還沒 all＝只把 o1111o 退回正式版；已經 all＝6 個一起退回上一版）。
- 看狀態：`bash /srv/1111bot/deploy.sh status`（6 個帳戶在跑的版本＋正式版／測試版／備份三個檔案的版本）。

## VPS（/srv/1111bot）
- `run_bot.py`（正式版）、`run_bot_next.py`（測試版，o1111o）、`run_bot.py.bak`（上一版）。
- `config/`：accounts.env（OKX API）、bots.env（TG）、acct_oXXXXo.env（每個帳戶的 ACCT）、symbols.json。金鑰只在 VPS，不進 GitHub。
- `data/`：runt*（/runt 模擬）、run*（/run 實盤：runlayer、runday、runlog、runevent）、strategies_*（聊天室、/tf 週期）。
- `.venv/`：bot 用的 Python。服務：`1111bot-oXXXXo-normal.service`。

## 原則
OKX 為唯一真相；BOT 永遠不平倉；Telegram、WebSocket 是旁路。

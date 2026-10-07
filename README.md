# 1111bot — OKX 多帳戶 Telegram bot

## 檔案
- `run_bot.py`：bot 本體（五個帳戶 o2222o～o6666o 共用同一份）。規則與每一版的改動寫在檔案裡「/runt 佈局策略」那一區的註解。
- `deploy5.sh`：VPS 部署腳本（`bash /srv/1111bot/deploy5.sh <行數> <版本>`；還原 `rollback`；狀態 `status`）。
- `app/core/emoji.py`：畫面用的表情符號（run_bot.py 會載入）。
- `requirements.txt`：Python 套件。

## 部署
1. GitHub 網頁上傳 `run_bot.py` 到 main，確認時間顯示「now」。
2. VPS：`bash /srv/1111bot/deploy5.sh <行數> <版本>`（從 GitHub 抓檔、驗證行數與版本、備份、重啟五個帳戶）。

## VPS（/srv/1111bot）
- `config/`：accounts.env（OKX API）、bots.env（TG）、acct_oXXXXo.env（每個帳戶的 ACCT）、symbols.json。金鑰只在 VPS，不進 GitHub。
- `data/`：runtlayer_*（/runt 持倉與掛單）、runtday_*（本日已實現）、runtlog_*（逐單出場紀錄，留 30 天）、strategies_*（聊天室、/tf 週期）。
- `.venv/`：bot 用的 Python。服務：`1111bot-oXXXXo-normal.service`。

## 原則
OKX 為唯一真相；BOT 永遠不平倉；Telegram、WebSocket 是旁路。

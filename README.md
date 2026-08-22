# 1111BOT — OKX 交易系統（重建版）

## 目錄結構
- app/adapter    OKX Adapter（唯一對外出口）
- app/strategy   策略引擎（普K / 均K，純函式）
- app/gateway    Telegram Gateway（8 bot + 通知）
- app/watchdog   對帳
- app/reporter   日報表
- app/core       共用（Decimal、時間、精度）
- config         設定檔（.env 不進版控）
- data           SQLite（不進版控）
- logs           日誌（不進版控）
- systemd        服務單元檔
- scripts        維運腳本
- tests          測試

## 原則
OKX 為唯一真相；DB 僅紀錄，不參與下單判斷。

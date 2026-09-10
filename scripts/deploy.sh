#!/bin/bash
# deploy.sh — 一鍵部署：將最新腳本重啟到所有生產帳戶
# 用法：./scripts/deploy.sh
# 注意：o6666o 是測試帳戶，不在部署範圍內
# 執行前請確認 o6666o 測試無誤

set -e

ACCOUNTS=("o2222o" "o3333o" "o4444o" "o5555o")

echo "==============================="
echo "  1111bot 一鍵部署（原K）"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "==============================="
echo ""
echo "部署目標：${ACCOUNTS[*]}"
echo "策略：原K (normal)"
echo ""

# 確認提示
read -p "確認部署？(輸入 yes 繼續) " confirm
if [ "$confirm" != "yes" ]; then
    echo "已取消。"
    exit 0
fi

echo ""
echo "--- 開始重啟服務 ---"

for acct in "${ACCOUNTS[@]}"; do
    service="1111bot-${acct}-normal.service"
    echo -n "重啟 $service ... "
    sudo systemctl restart "$service"
    sleep 2
    status=$(systemctl is-active "$service")
    if [ "$status" = "active" ]; then
        echo "✅ running"
    else
        echo "❌ $status（請檢查：sudo journalctl -u $service -n 20）"
    fi
done

echo ""
echo "--- 最終狀態確認 ---"
systemctl list-units | grep 1111bot

echo ""
echo "部署完成：$(date '+%Y-%m-%d %H:%M:%S')"

#!/usr/bin/env bash
# ============================================================
# 1111bot 全帳戶部署腳本（非互動，不會問 yes/no）
#
#   部署：bash /srv/1111bot/deploy5.sh <預期行數> <預期版本>
#         例：bash /srv/1111bot/deploy5.sh 3401 v3.6.1
#   回滾：bash /srv/1111bot/deploy5.sh rollback
#   查狀態：bash /srv/1111bot/deploy5.sh status
#
# 安全設計：
#   1. 行數 + 版本 + 語法 三項全過才動檔案，任一不過直接中止、不覆蓋
#   2. 跑在子行程，中止不會斷你的 SSH
#   3. 五個帳戶共用同一份 run_bot.py，只有服務要各自重啟
#   4. 只重啟「實際存在」的服務，缺的略過不報錯
# ============================================================
ACCTS="o2222o o3333o o4444o o5555o o6666o"
TARGET="/srv/1111bot/run_bot.py"
BAK="/srv/1111bot/run_bot.py.bak"
TMP="/tmp/run_bot.py"
RAW="https://raw.githubusercontent.com/oooo1111oooo/1111bot-scripts/main/run_bot.py"

svc() { echo "1111bot-$1-normal.service"; }
exists() { systemctl list-unit-files "$(svc "$1")" --no-legend 2>/dev/null | grep -q .; }

restart_all() {
    local done_n=0
    for A in $ACCTS; do
        if exists "$A"; then
            sudo systemctl restart "$(svc "$A")"
            done_n=$((done_n+1))
        else
            echo "  $A 　　（無此服務，略過）"
        fi
    done
    echo "  已重啟 $done_n 個服務，等待啟動…"
    sleep 10
}

report() {
    echo "────────── 各帳戶狀態 ──────────"
    for A in $ACCTS; do
        exists "$A" || continue
        local st ver
        st=$(systemctl is-active "$(svc "$A")" 2>/dev/null)
        ver=$(sudo journalctl -u "$(svc "$A")" --since "-60 sec" --no-pager 2>/dev/null \
              | grep -o '移動任務已啟動（v[0-9.]*）' | tail -1)
        printf "  %-8s %-10s %s\n" "$A" "$st" "${ver:-（尚未印出版本）}"
    done
    echo "────────────────────────────────"
}

case "${1:-}" in
  status)
      report; exit 0 ;;
  rollback)
      if [ ! -f "$BAK" ]; then echo "❌ 找不到 $BAK，無法回滾"; exit 1; fi
      echo "⏪ 回滾到上一版：$(grep -o 'VERSION = \"[^\"]*\"' "$BAK" | head -1)"
      cp "$BAK" "$TARGET"
      restart_all
      report
      echo "✅ 回滾完成"
      exit 0 ;;
esac

WANT_L="${1:-}"; WANT_V="${2:-}"
if [ -z "$WANT_L" ] || [ -z "$WANT_V" ]; then
    echo "用法：bash $0 <預期行數> <預期版本>"
    echo "  例：bash $0 3401 v3.6.1"
    echo "  其他：bash $0 rollback ｜ bash $0 status"
    exit 1
fi

echo "① 下載 GitHub main…"
if ! curl -sSL -f -o "$TMP" "$RAW"; then
    echo "❌ 下載失敗，未部署（現有版本完好）"; exit 1
fi

GOT_L=$(wc -l < "$TMP")
GOT_V=$(grep -o 'VERSION = "[^"]*"' "$TMP" | head -1 | sed 's/.*"\(.*\)"/\1/')
echo "② 驗證：行數 $GOT_L（需 $WANT_L）｜版本 $GOT_V（需 $WANT_V）"

if [ "$GOT_L" != "$WANT_L" ] || [ "$GOT_V" != "$WANT_V" ]; then
    echo "❌ 對不上 —— GitHub 可能還沒更新，未部署（現有版本完好）"
    echo "   請確認網頁上 run_bot.py 顯示「now」後重跑"
    exit 1
fi

if ! python3 -c "import ast;ast.parse(open('$TMP',encoding='utf-8').read())" 2>/dev/null; then
    echo "❌ 語法錯誤，未部署（現有版本完好）"; exit 1
fi
echo "   語法OK"

OLD_V=$(grep -o 'VERSION = "[^"]*"' "$TARGET" 2>/dev/null | head -1 | sed 's/.*"\(.*\)"/\1/')
echo "③ 備份 $OLD_V → run_bot.py.bak，寫入 $GOT_V"
cp "$TARGET" "$BAK" 2>/dev/null
cp "$TMP" "$TARGET"

echo "④ 重啟全部帳戶"
restart_all
report
echo "✅ $GOT_V 部署完成（$OLD_V 已備份，回滾：bash $0 rollback）"

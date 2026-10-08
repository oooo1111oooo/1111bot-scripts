#!/usr/bin/env bash
# ============================================================
# 1111bot 部署腳本（6 個帳戶；2026-10-08 起取代 deploy5.sh）
#
#   測試：bash /srv/1111bot/deploy.sh test <行數> <版本>
#         例：bash /srv/1111bot/deploy.sh test 4334 v11.2
#         → 從 GitHub 下載、驗證，只更新 o1111o（測試版 run_bot_next.py）並重啟；其他 5 個完全不動。
#           o1111o 起不來 → 自動退回正式版。
#   全部：bash /srv/1111bot/deploy.sh all
#         → o1111o 測過的版本推到 o2222o～o6666o（正式版 run_bot.py；舊版備份 run_bot.py.bak）。
#   回滾：bash /srv/1111bot/deploy.sh rollback
#         → 還沒推（測試版≠正式版）：只把 o1111o 退回正式版。
#           已經推了：6 個帳戶一起退回上一版（run_bot.py.bak）。
#   狀態：bash /srv/1111bot/deploy.sh status
#
# 安全設計：
#   1. 行數＋版本＋語法 三項全過才動檔案，任一不過直接中止、不覆蓋
#   2. 正式版、測試版是兩個檔案：test 只碰測試版，all 才碰正式版
#   3. all 之前檢查 o1111o 真的在跑那一版；推完檢查 5 個帳戶都印出新版本
#   4. 只重啟實際存在的服務，缺的略過
# ============================================================
TEST="o1111o"
PROD="o2222o o3333o o4444o o5555o o6666o"
B="${BOT_DIR:-/srv/1111bot}"
LIVE="$B/run_bot.py"
NEXT="$B/run_bot_next.py"
BAK="$B/run_bot.py.bak"
TMP="${TMPDIR:-/tmp}/run_bot_dl.py"
RAW="https://raw.githubusercontent.com/oooo1111oooo/1111bot-scripts/main/run_bot.py"
PY="$B/.venv/bin/python"; [ -x "$PY" ] || PY=python3

svc() { echo "1111bot-$1-normal.service"; }
exists() { systemctl list-unit-files "$(svc "$1")" --no-legend 2>/dev/null | grep -q .; }
ver() { grep -o 'VERSION = "[^"]*"' "$1" 2>/dev/null | head -1 | sed 's/.*"\(.*\)"/\1/'; }
running() {   # 這個帳戶最近一次啟動印的版本（$2＝從什麼時間之後找；省略＝從這個服務最近一次啟動開始找）
    local since="${2:-}"
    if [ -z "$since" ]; then
        local at; at=$(TZ=UTC systemctl show -p ActiveEnterTimestamp --value "$(svc "$1")" 2>/dev/null)
        [ -n "$at" ] && since=$(date -d "$at" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
    fi
    if [ -n "$since" ]; then
        sudo journalctl -u "$(svc "$1")" --since "$since" --no-pager 2>/dev/null
    else
        sudo journalctl -u "$(svc "$1")" -n 5000 --no-pager 2>/dev/null
    fi | grep -o '移動任務已啟動（v[0-9.]*）' | tail -1 | sed 's/.*（\(v[0-9.]*\)）/\1/'
}
restart() {
    local n=0
    for A in "$@"; do
        if exists "$A"; then sudo systemctl restart "$(svc "$A")"; n=$((n+1)); else echo "  $A　（無此服務，略過）"; fi
    done
    echo "  已重啟 $n 個服務，等待啟動…"
    sleep "${WAIT_SEC:-12}"
}
report() {    # $1＝從什麼時間之後找版本（空＝最近一次啟動），其餘＝帳戶
    local since="$1"; shift
    echo "────────── 各帳戶狀態 ──────────"
    for A in "$@"; do
        exists "$A" || { printf "  %-8s %s\n" "$A" "（無此服務）"; continue; }
        local st v
        st=$(systemctl is-active "$(svc "$A")" 2>/dev/null)
        v=$(running "$A" "$since")
        if [ -n "$v" ]; then v="移動任務已啟動（$v）"; else v="（沒有印出版本）"; fi
        printf "  %-8s %-10s %s\n" "$A" "$st" "$v"
    done
    echo "  檔案：正式版 $(ver "$LIVE")（o2222o～o6666o）｜測試版 $(ver "$NEXT")（o1111o）｜上一版備份 $(ver "$BAK" | grep . || echo 沒有)"
    echo "────────────────────────────────"
}
ok_on() {     # 帳戶都在跑、而且 $1 之後印出版本 $2
    local since="$1" want="$2"; shift 2
    for A in "$@"; do
        exists "$A" || continue
        [ "$(systemctl is-active "$(svc "$A")" 2>/dev/null)" = "active" ] || return 1
        [ "$(running "$A" "$since")" = "$want" ] || return 1
    done
    return 0
}
syntax_ok() { "$PY" -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$1" 2>/dev/null; }
now() { date '+%Y-%m-%d %H:%M:%S'; }

case "${1:-}" in
  status)
      report "" $TEST $PROD; exit 0 ;;

  test)
      WANT_L="${2:-}"; WANT_V="${3:-}"
      if [ -z "$WANT_L" ] || [ -z "$WANT_V" ]; then
          echo "用法：bash $0 test <行數> <版本>　例：bash $0 test 4334 v11.2"; exit 1
      fi
      exists "$TEST" || { echo "❌ 找不到 o1111o 的服務，請先完成 o1111o 設定"; exit 1; }
      echo "① 下載 GitHub main…"
      if ! curl -sSL -f -o "$TMP" "$RAW"; then echo "❌ 下載失敗，未更新（現有版本完好）"; exit 1; fi
      GOT_L=$(wc -l < "$TMP"); GOT_V=$(ver "$TMP")
      echo "② 驗證：行數 $GOT_L（需 $WANT_L）｜版本 $GOT_V（需 $WANT_V）"
      if [ "$GOT_L" != "$WANT_L" ] || [ "$GOT_V" != "$WANT_V" ]; then
          echo "❌ 對不上 —— GitHub 可能還沒更新，未更新（現有版本完好）"
          echo "   請確認網頁上 run_bot.py 顯示「now」後重跑"; exit 1
      fi
      if ! syntax_ok "$TMP"; then echo "❌ 語法錯誤，未更新（現有版本完好）"; exit 1; fi
      echo "   語法OK"
      echo "③ 只更新 o1111o（測試版 $(ver "$NEXT") → $GOT_V）；o2222o～o6666o 不動（正式版 $(ver "$LIVE")）"
      cp "$TMP" "$NEXT"
      T0=$(now)
      restart $TEST
      report "$T0" $TEST
      if ok_on "$T0" "$GOT_V" $TEST; then
          echo "✅ o1111o 已經在跑 $GOT_V 測試版"
          echo "   TG 上用 o1111o 測過沒問題 → bash $0 all（推到 o2222o～o6666o）"
          echo "   有問題 → bash $0 rollback（o1111o 退回正式版 $(ver "$LIVE")）"
          exit 0
      fi
      echo "❌ o1111o 沒有正常啟動 $GOT_V → 自動退回正式版 $(ver "$LIVE")"
      cp "$LIVE" "$NEXT"
      T1=$(now); restart $TEST; report "$T1" $TEST
      echo "   請把上面的結果貼給 Claude"
      exit 1 ;;

  all)
      [ -f "$NEXT" ] || { echo "❌ 沒有測試版，請先 bash $0 test <行數> <版本>"; exit 1; }
      NV=$(ver "$NEXT"); LV=$(ver "$LIVE")
      if cmp -s "$NEXT" "$LIVE"; then echo "✅ 正式版已經是 $NV，不用推"; report "" $TEST $PROD; exit 0; fi
      if [ "$(systemctl is-active "$(svc "$TEST")" 2>/dev/null)" != "active" ] || [ "$(running "$TEST")" != "$NV" ]; then
          echo "❌ o1111o 沒有在跑測試版 $NV，未推（正式版 $LV 完好）"
          echo "   請先 bash $0 test <行數> <版本>，在 TG 上測過再 all"; exit 1
      fi
      if ! syntax_ok "$NEXT"; then echo "❌ 測試版語法錯誤，未推"; exit 1; fi
      echo "① 備份正式版 $LV → run_bot.py.bak，推 $NV 到 o2222o～o6666o"
      cp "$LIVE" "$BAK"
      cp "$NEXT" "$LIVE"
      T0=$(now)
      restart $PROD
      report "$T0" $PROD
      if ok_on "$T0" "$NV" $PROD; then
          echo "✅ $NV 部署完成：6 個帳戶都是 $NV（$LV 已備份，回滾：bash $0 rollback）"
          exit 0
      fi
      echo "❌ 有帳戶沒有正常啟動 $NV —— 請把上面的結果貼給 Claude；要退回 $LV：bash $0 rollback"
      exit 1 ;;

  rollback)
      if ! cmp -s "$NEXT" "$LIVE"; then
          echo "⏪ 測試版還沒推：只把 o1111o 退回正式版 $(ver "$LIVE")（o2222o～o6666o 不動）"
          cp "$LIVE" "$NEXT"
          T0=$(now); restart $TEST; report "$T0" $TEST
          echo "✅ o1111o 已退回 $(ver "$LIVE")"
          exit 0
      fi
      [ -f "$BAK" ] || { echo "❌ 找不到 run_bot.py.bak，無法回滾"; exit 1; }
      echo "⏪ 6 個帳戶一起退回上一版 $(ver "$BAK")（現在 $(ver "$LIVE")）"
      cp "$BAK" "$LIVE"; cp "$BAK" "$NEXT"
      T0=$(now); restart $TEST $PROD; report "$T0" $TEST $PROD
      echo "✅ 回滾完成"
      exit 0 ;;

  *)
      echo "用法："
      echo "  bash $0 test <行數> <版本>　只更新 o1111o（測試）"
      echo "  bash $0 all　　　　　　　　 測過的版本推到 o2222o～o6666o"
      echo "  bash $0 rollback　　　　　　退回"
      echo "  bash $0 status　　　　　　　6 個帳戶狀態"
      exit 1 ;;
esac

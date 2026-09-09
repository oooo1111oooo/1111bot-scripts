#!/usr/bin/env bash
# 1111bot 統一備份
# 用法：
#   ./scripts/backup.sh              一般備份
#   ./scripts/backup.sh b62-runha    加上標籤，方便日後辨識
#
# 備份內容（重建系統所需的全部檔案）：
#   run_bot.py / run_ha.py / make_report.py
#   app/            策略與共用模組
#   config/         含 accounts.env、bots.env（機密！）
#   systemd/        專案內的 unit 範本
#   /etc/systemd/system/1111bot-*.service   實際生效的 unit
#   data/strategies_*.json                  策略狀態
#   data/trades_*.json                      交易紀錄
#
# 不備份（可重建或體積大）：
#   .venv/  .git/  __pycache__/  .stage/  *.bak-*  data/*.xlsx  data/ha_market.db
#
# ⚠ 產出的壓縮檔內含 API 金鑰與 Telegram token，
#   權限設為 600，且 archive/ 已列入 .gitignore，絕不可上傳 GitHub。

set -euo pipefail

ROOT="/srv/1111bot"
OUT="$ROOT/archive"
KEEP=30                      # 保留最近幾份，更舊的自動刪除
TAG="${1:-}"
STAMP="$(date +%Y%m%d-%H%M%S)"
[ -n "$TAG" ] && NAME="1111bot-${STAMP}-${TAG}.tar.gz" || NAME="1111bot-${STAMP}.tar.gz"
DEST="$OUT/$NAME"

cd "$ROOT"
mkdir -p "$OUT"

# 把實際生效的 systemd unit 也一併收進來（它們不在專案目錄下）
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/etc-systemd"
cp -a /etc/systemd/system/1111bot-*.service "$TMP/etc-systemd/" 2>/dev/null || true
cp -a /etc/systemd/system/1111bot-*.service.d "$TMP/etc-systemd/" 2>/dev/null || true

# 記錄環境資訊，日後排查用
{
  echo "備份時間: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "主機: $(hostname)"
  echo "Python: $($ROOT/.venv/bin/python -V 2>&1)"
  echo ""
  echo "== 檔案行數 =="
  for f in run_bot.py run_ha.py make_report.py; do
    [ -f "$f" ] && echo "$(wc -l < "$f") $f"
  done
  echo ""
  echo "== git =="
  git rev-parse --short HEAD 2>/dev/null || echo "(無 git)"
  git status --short 2>/dev/null | head -20 || true
  echo ""
  echo "== 服務狀態 =="
  systemctl is-active 1111bot-o3333o-normal.service 2>/dev/null || true
  systemctl is-active 1111bot-o3333o-ha.service 2>/dev/null || true
} > "$TMP/MANIFEST.txt"

tar -czf "$DEST" \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.bak-*' \
  --exclude='data/*.xlsx' \
  --exclude='data/ha_market.db*' \
  -C "$ROOT" \
    run_bot.py run_ha.py make_report.py requirements.txt README.md .gitignore \
    app config systemd scripts \
    $(cd "$ROOT" && ls data/strategies_*.json data/trades_*.json 2>/dev/null || true) \
  -C "$TMP" MANIFEST.txt etc-systemd

chmod 600 "$DEST"

# 保留最近 KEEP 份
cd "$OUT"
ls -1t 1111bot-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

SIZE="$(du -h "$DEST" | cut -f1)"
CNT="$(tar -tzf "$DEST" | wc -l)"
echo "✅ 備份完成"
echo "   檔案：$DEST"
echo "   大小：$SIZE｜項目數：$CNT"
echo "   現存備份：$(ls -1 1111bot-*.tar.gz 2>/dev/null | wc -l) 份（上限 $KEEP）"

#!/usr/bin/env bash
set -e
ROOT=/srv/1111bot
OLD=/srv/tradebot
ARCH=$ROOT/archive
s(){ echo; echo "########## $1 ##########"; }

s "0 確保 archive 目錄存在且排除版控"
mkdir -p $ARCH
grep -q '^archive/' $ROOT/.gitignore 2>/dev/null || echo 'archive/' >> $ROOT/.gitignore
echo "archive 目錄就緒，已加入 .gitignore"

s "1 systemd 單元檔（前次已建，確認存在）"
ls -1 $ROOT/systemd/

s "2 commit systemd 單元檔 + .gitignore 更新"
cd $ROOT
git add systemd/ .gitignore
git commit -q -m "B1-6: systemd 單元骨架" || echo "（無新變更）"
git log --oneline | head -6

s "3 舊系統封存（打包 /srv/tradebot，可能 1-2 分鐘）"
if [ -d "$OLD" ]; then
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  TAR=$ARCH/tradebot-$TS.tar.gz
  echo "打包中：$OLD"
  sudo tar -czf "$TAR" -C /srv tradebot 2>/dev/null
  sudo chown ubuntu:ubuntu "$TAR"
  echo "封存完成：$(du -h "$TAR" | cut -f1)"
  echo "原目錄大小：$(sudo du -sh "$OLD" | cut -f1)"
  echo "$TAR" > $ARCH/latest.txt
else
  echo "舊目錄不存在，略過"
fi

s "4 驗證封存可解開"
TAR=$(cat $ARCH/latest.txt 2>/dev/null)
if [ -f "$TAR" ]; then
  CNT=$(tar -tzf "$TAR" 2>/dev/null | wc -l)
  echo "封存內含 $CNT 個項目"
  if tar -tzf "$TAR" >/dev/null 2>&1; then echo "OK: 封存檔完整可解開"; else echo "DANGER: 封存檔損毀"; fi
fi

s "5 確認 archive 不進版控"
cd $ROOT
if git status -s | grep -q 'archive/'; then echo "DANGER: archive 被追蹤"; else echo "OK: archive 不進版控"; fi

s "6 B1 完成總結"
find $ROOT -maxdepth 2 -type d -not -path '*/.git*' -not -path '*/.venv*' -not -path '*/archive*' | sort
echo "DB 表數：$($ROOT/.venv/bin/python -c "import sqlite3;print(len([r for r in sqlite3.connect('$ROOT/data/1111bot.sqlite3').execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'\")]))")"
echo "git commits：$(cd $ROOT && git rev-list --count HEAD)"
free -m | head -2

s END

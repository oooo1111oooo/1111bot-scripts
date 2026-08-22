#!/usr/bin/env bash
set -e
ROOT=/srv/1111bot
OLD=/srv/tradebot
ARCH=$ROOT/../tradebot-archive
s(){ echo; echo "########## $1 ##########"; }

s "0 建立 systemd 單元檔（只建立，不啟動）"
mkdir -p $ROOT/systemd
for svc in gateway market executor watchdog; do
cat > $ROOT/systemd/1111bot-$svc.service <<UNIT
[Unit]
Description=1111BOT $svc
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -m app.$svc
Restart=on-failure
RestartSec=5
Environment=PYTHONPATH=$ROOT
Environment=TZ=Asia/Taipei

[Install]
WantedBy=multi-user.target
UNIT
echo "已建立 1111bot-$svc.service"
done

s "1 reporter 用 timer（每日排程，非常駐）"
cat > $ROOT/systemd/1111bot-reporter.service <<UNIT
[Unit]
Description=1111BOT reporter (daily)
After=network-online.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=$ROOT
ExecStart=$ROOT/.venv/bin/python -m app.reporter
Environment=PYTHONPATH=$ROOT
Environment=TZ=Asia/Taipei
UNIT
cat > $ROOT/systemd/1111bot-reporter.timer <<UNIT
[Unit]
Description=1111BOT daily report at 00:05 Asia/Taipei

[Timer]
OnCalendar=*-*-* 00:05:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT
echo "已建立 reporter service + timer"

s "2 列出所有單元檔"
ls -la $ROOT/systemd/

s "3 語法檢查（不安裝，只驗證格式）"
for f in $ROOT/systemd/*.service $ROOT/systemd/*.timer; do
  systemd-analyze verify "$f" 2>&1 | grep -v "Cannot find unit\|app\." || echo "OK: $(basename $f)"
done

s "4 commit systemd 單元檔"
cd $ROOT
git add systemd/
git commit -q -m "B1-6: systemd 單元骨架"
git log --oneline | head -6

s "5 舊系統封存（打包 /srv/tradebot，可能需 1-2 分鐘）"
if [ -d "$OLD" ]; then
  mkdir -p $ARCH
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  TAR=$ARCH/tradebot-$TS.tar.gz
  echo "打包中：$OLD -> $TAR"
  sudo tar -czf "$TAR" -C /srv tradebot 2>/dev/null
  echo "封存完成：$(du -h "$TAR" | cut -f1)"
  echo "原目錄大小：$(sudo du -sh "$OLD" | cut -f1)"
else
  echo "舊目錄 $OLD 不存在，略過"
fi

s "6 驗證封存可解開（測試完整性，不實際還原）"
if [ -f "$TAR" ]; then
  CNT=$(tar -tzf "$TAR" 2>/dev/null | wc -l)
  echo "封存內含 $CNT 個項目"
  if tar -tzf "$TAR" >/dev/null 2>&1; then echo "OK: 封存檔完整可解開"; else echo "DANGER: 封存檔損毀"; fi
fi

s "7 B1 完成總結"
echo "新系統目錄結構："
find $ROOT -maxdepth 2 -type d -not -path '*/.git*' -not -path '*/.venv*' | sort
echo ""
echo "DB 表數：$($ROOT/.venv/bin/python -c "import sqlite3;print(len([r for r in sqlite3.connect('$ROOT/data/1111bot.sqlite3').execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'\")]))")"
echo "git commits：$(cd $ROOT && git rev-list --count HEAD)"
echo "記憶體："
free -m | head -2

s END

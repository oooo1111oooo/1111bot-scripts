#!/usr/bin/env bash
set -e
cat > /etc/systemd/system/1111bot-o3333o-normal.service <<'EOF'
[Unit]
Description=1111bot o3333o normal
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory=/srv/1111bot
ExecStart=/srv/1111bot/.venv/bin/python /srv/1111bot/run_bot.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable 1111bot-o3333o-normal.service
systemctl restart 1111bot-o3333o-normal.service
sleep 4
systemctl status 1111bot-o3333o-normal.service --no-pager | head -10

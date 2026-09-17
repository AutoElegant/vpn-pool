#!/usr/bin/env bash
# Обновление кода на VPS (запускать из папки с исходниками, от root)
set -euo pipefail
APP_DIR=/opt/vpnbot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cp -r "$SRC_DIR"/{core,bot,config.yml,requirements.txt} "$APP_DIR"/
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R vpnbot:vpnbot "$APP_DIR"
systemctl restart vpn-bot
echo "Обновлено. Статус:"
systemctl --no-pager status vpn-bot | head -5

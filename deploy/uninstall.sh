#!/usr/bin/env bash
# Полное удаление бота и коллектора с сервера. Запускать от root.
set -uo pipefail
systemctl disable --now vpn-bot.service vpn-collector.timer vpn-collector.service 2>/dev/null
rm -f /etc/systemd/system/vpn-bot.service /etc/systemd/system/vpn-collector.service /etc/systemd/system/vpn-collector.timer
systemctl daemon-reload
rm -rf /opt/vpnbot
rm -f /usr/local/bin/xray
userdel vpnbot 2>/dev/null
echo "Удалено: /opt/vpnbot, юниты systemd, юзер vpnbot, /usr/local/bin/xray"

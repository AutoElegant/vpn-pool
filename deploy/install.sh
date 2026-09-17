#!/usr/bin/env bash
# Установка на чистую Ubuntu 22.04/24.04. Запускать от root:
#   bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/vpnbot
APP_USER=vpnbot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl unzip ca-certificates

echo "==> Пользователь $APP_USER"
id -u "$APP_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "==> Копирую код в $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  cp -r "$SRC_DIR"/{core,bot,config.yml,requirements.txt} "$APP_DIR"/
  [ -f "$SRC_DIR/.env" ] && cp "$SRC_DIR/.env" "$APP_DIR"/ || cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
fi
mkdir -p "$APP_DIR/data"

echo "==> venv + зависимости"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> xray-core (для глубокой проверки конфигов)"
if ! command -v xray &>/dev/null; then
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64)  XR=Xray-linux-64 ;;
    aarch64) XR=Xray-linux-arm64-v8a ;;
    *) echo "!! неизвестная архитектура $ARCH — пропускаю xray"; XR="" ;;
  esac
  if [ -n "$XR" ]; then
    TMP=$(mktemp -d)
    curl -fsSL -o "$TMP/xray.zip" \
      "https://github.com/XTLS/Xray-core/releases/latest/download/${XR}.zip"
    unzip -qo "$TMP/xray.zip" -d "$TMP"
    install -m 0755 "$TMP/xray" /usr/local/bin/xray
    rm -rf "$TMP"
    echo "   xray $(xray version | head -1)"
  fi
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> systemd"
cp "$SRC_DIR"/deploy/vpn-collector.service /etc/systemd/system/
cp "$SRC_DIR"/deploy/vpn-collector.timer   /etc/systemd/system/
cp "$SRC_DIR"/deploy/vpn-bot.service       /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vpn-collector.timer

echo
echo "────────────────────────────────────────────────"
echo " Почти готово. Осталось:"
echo
echo " 1) Впиши токен бота и свой Telegram ID:"
echo "      nano $APP_DIR/.env"
echo
echo " 2) Первый сбор конфигов (5-15 минут):"
echo "      systemctl start vpn-collector.service"
echo "      journalctl -u vpn-collector -f"
echo
echo " 3) Запусти бота:"
echo "      systemctl enable --now vpn-bot"
echo "      journalctl -u vpn-bot -f"
echo "────────────────────────────────────────────────"

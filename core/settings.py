"""Загрузка config.yml + .env."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# VPN_CONFIG позволяет подсунуть другой профиль — например, на раннере
# GitHub Actions, где можно проверять куда агрессивнее, чем со своего VPS.
CONFIG_PATH = Path(os.getenv("VPN_CONFIG") or (ROOT / "config.yml"))
if not CONFIG_PATH.is_absolute():
    CONFIG_PATH = ROOT / CONFIG_PATH

with CONFIG_PATH.open(encoding="utf-8") as fh:
    CONFIG: dict = yaml.safe_load(fh)

DB_PATH = ROOT / CONFIG["database"]
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

COLLECTOR = CONFIG.get("collector", {})
SOURCES = CONFIG.get("sources", [])
BOT = CONFIG.get("bot", {})

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

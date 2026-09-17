"""Состояние коллектора в виде одного JSON-файла.

Нужно, потому что раннер GitHub Actions эфемерный: после прохода машина
исчезает вместе с SQLite. Пул и чёрный список поэтому живут в репозитории,
а БД пересобирается из них в начале каждого запуска.

Этот же файл забирает VPS — ему остаётся только влить пул к себе.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("state")

# Поля конфига, которые переживают проход.
FIELDS = (
    "fingerprint", "link", "protocol", "host", "port", "ip", "security",
    "network", "sni", "country", "country_name", "city", "latency_ms",
    "alive", "verified", "ru_nodes", "ru_checked_at", "risk", "checks",
    "oks", "source", "first_seen", "last_seen", "last_ok",
)

MAX_DEADLIST = 20000  # чтобы файл не рос бесконечно


def export_state(conn: sqlite3.Connection, path: Path | str) -> dict:
    cols = ", ".join(FIELDS)
    pool = [
        dict(zip(FIELDS, row))
        for row in conn.execute(f"SELECT {cols} FROM configs WHERE alive = 1")
    ]
    dead = dict(
        conn.execute(
            "SELECT fingerprint, until FROM deadlist ORDER BY until DESC LIMIT ?",
            (MAX_DEADLIST,),
        ).fetchall()
    )
    geo = [
        dict(zip(("ip", "country", "country_name", "city", "updated_at"), row))
        for row in conn.execute(
            "SELECT ip, country, country_name, city, updated_at FROM geo_cache"
        )
    ]

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pool": pool,
        "deadlist": dead,
        "geo_cache": geo,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Состояние сохранено: %s (пул %d, чёрный список %d)",
             path, len(pool), len(dead))
    return payload


def import_state(conn: sqlite3.Connection, path: Path | str) -> int:
    path = Path(path)
    if not path.exists():
        log.info("Файла состояния нет — стартуем с нуля")
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        log.warning("Файл состояния битый (%s) — стартуем с нуля", exc)
        return 0

    return load_payload(conn, data)


def load_payload(conn: sqlite3.Connection, data: dict) -> int:
    pool = data.get("pool") or []
    placeholders = ", ".join("?" * len(FIELDS))
    conn.executemany(
        f"INSERT OR REPLACE INTO configs ({', '.join(FIELDS)}) VALUES ({placeholders})",
        [tuple(item.get(f) for f in FIELDS) for item in pool],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO deadlist(fingerprint, until) VALUES(?, ?)",
        list((data.get("deadlist") or {}).items()),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO geo_cache(ip, country, country_name, city, updated_at) "
        "VALUES(?,?,?,?,?)",
        [
            (g["ip"], g["country"], g["country_name"], g["city"], g["updated_at"])
            for g in (data.get("geo_cache") or [])
        ],
    )
    conn.commit()
    log.info("Состояние загружено: пул %d, чёрный список %d",
             len(pool), len(data.get("deadlist") or {}))
    return len(pool)

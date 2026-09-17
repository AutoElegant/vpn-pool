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
    "alive", "verified", "ru_nodes", "ru_checked_at", "risk", "fail_streak",
    "checks", "oks", "source", "first_seen", "last_seen", "last_ok",
)

# Что считается рабочим конфигом: отвечает и доступен из России.
# ru_nodes = -1 значит «проверить не удалось» — такие отдаём, но последними.
LIVE = "alive = 1 AND ru_nodes <> 0"

MAX_DEADLIST = 12000  # чтобы файл не рос бесконечно


def export_state(
    conn: sqlite3.Connection, path: Path | str, pool_path: Path | str | None = None
) -> dict:
    """Пишет полное состояние в `path`.

    Если задан `pool_path`, отдельно кладёт туда урезанный файл только с пулом —
    его качает сервер каждые 10 минут, и таскать ради этого чёрный список
    на сотни килобайт незачем.
    """
    cols = ", ".join(FIELDS)
    # В полное состояние идут ВСЕ записи, включая тех, кто на испытательном
    # сроке (alive = 0, но промахов меньше лимита). Иначе следующий проход
    # про них забудет и начнёт проверять заново как незнакомцев.
    everything = [
        dict(zip(FIELDS, row)) for row in conn.execute(f"SELECT {cols} FROM configs")
    ]
    pool = [
        dict(zip(FIELDS, row))
        for row in conn.execute(f"SELECT {cols} FROM configs WHERE {LIVE}")
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
        "pool": everything,
        "deadlist": dead,
        "geo_cache": geo,
    }
    # Компактно, без отступов: файл перезаписывается каждый час,
    # и лишние переводы строк тут превращаются в сотни лишних килобайт.
    dump = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(payload), encoding="utf-8")

    if pool_path:
        slim = {"updated_at": payload["updated_at"], "pool": pool,
                "geo_cache": geo}
        pool_path = Path(pool_path)
        pool_path.parent.mkdir(parents=True, exist_ok=True)
        pool_path.write_text(dump(slim), encoding="utf-8")

    log.info("Состояние сохранено: %s (записей %d, из них рабочих %d, "
             "чёрный список %d, %.0f КБ)",
             path, len(everything), len(pool), len(dead), path.stat().st_size / 1024)
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

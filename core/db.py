"""Схема БД и синхронные помощники (используются коллектором)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .settings import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS configs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT    NOT NULL UNIQUE,
    link          TEXT    NOT NULL,
    protocol      TEXT    NOT NULL DEFAULT 'vless',
    host          TEXT    NOT NULL,
    port          INTEGER NOT NULL,
    ip            TEXT,
    security      TEXT,
    network       TEXT,
    sni           TEXT,
    country       TEXT,            -- ISO-код, напр. DE
    country_name  TEXT,
    city          TEXT,
    latency_ms    INTEGER,
    ru_nodes      INTEGER NOT NULL DEFAULT -1,   -- нод из РФ достучалось (-1 = не проверяли)
    ru_checked_at TEXT,                          -- когда последний раз проверяли из РФ
    risk          INTEGER NOT NULL DEFAULT 0,    -- штраф по эвристикам ТСПУ
    alive         INTEGER NOT NULL DEFAULT 0,
    verified      INTEGER NOT NULL DEFAULT 0,   -- 1 = прошёл глубокую проверку через xray
    fail_streak   INTEGER NOT NULL DEFAULT 0,
    checks        INTEGER NOT NULL DEFAULT 0,
    oks           INTEGER NOT NULL DEFAULT 0,
    source        TEXT,
    source_priority INTEGER NOT NULL DEFAULT 0,  -- насколько доверяем источнику
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    last_ok       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cfg_alive          ON configs(alive, ru_nodes, verified, latency_ms);
CREATE INDEX IF NOT EXISTS idx_cfg_alive_country  ON configs(alive, country, latency_ms);

-- Конфиги, которые не ответили: не берём их в кандидаты N дней,
-- иначе каждый проход тратился бы на перепроверку одного и того же мусора.
CREATE TABLE IF NOT EXISTS deadlist (
    fingerprint TEXT PRIMARY KEY,
    until       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS geo_cache (
    ip           TEXT PRIMARY KEY,
    country      TEXT,
    country_name TEXT,
    city         TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    lang          TEXT,
    joined_at     TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    is_blocked    INTEGER NOT NULL DEFAULT 0,
    configs_taken INTEGER NOT NULL DEFAULT 0,
    source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen);

CREATE TABLE IF NOT EXISTS issued (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    config_id INTEGER NOT NULL,
    country   TEXT,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issued_user ON issued(user_id, ts);

CREATE TABLE IF NOT EXISTS broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    preview     TEXT,
    total       INTEGER NOT NULL DEFAULT 0,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER DEFAULT 0,
    new_configs INTEGER DEFAULT 0,
    checked     INTEGER DEFAULT 0,
    alive       INTEGER DEFAULT 0,
    verified    INTEGER DEFAULT 0,
    dropped     INTEGER DEFAULT 0,
    pool        INTEGER DEFAULT 0
);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# Колонки, добавленные после первого релиза: CREATE TABLE IF NOT EXISTS их не подтянет.
MIGRATIONS = {
    "configs": {
        "ru_nodes": "INTEGER NOT NULL DEFAULT -1",
        "risk": "INTEGER NOT NULL DEFAULT 0",
        "ru_checked_at": "TEXT",
        "source_priority": "INTEGER NOT NULL DEFAULT 0",
    },
    "runs": {
        "pool": "INTEGER DEFAULT 0",
        "ru_ok": "INTEGER DEFAULT 0",
    },
}


def migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()


def init_db(path: Path | str = DB_PATH) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"БД готова: {DB_PATH}")

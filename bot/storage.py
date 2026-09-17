"""Асинхронный слой доступа к БД для бота."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import aiosqlite

from core.settings import BOT, DB_PATH

_db: aiosqlite.Connection | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(str(DB_PATH))
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA busy_timeout=10000")
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ───────────────────────── Пользователи ─────────────────────────

async def upsert_user(user_id: int, username: str | None, first_name: str | None,
                      lang: str | None, source: str | None = None) -> bool:
    """Возвращает True, если пользователь новый."""
    db = await get_db()
    now = iso()
    cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    is_new = await cur.fetchone() is None
    await db.execute(
        """
        INSERT INTO users(user_id, username, first_name, lang, joined_at, last_seen, source)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username   = excluded.username,
            first_name = excluded.first_name,
            lang       = excluded.lang,
            last_seen  = excluded.last_seen,
            is_blocked = 0
        """,
        (user_id, username, first_name, lang, now, now, source),
    )
    await db.commit()
    return is_new


async def mark_blocked(user_id: int) -> None:
    db = await get_db()
    await db.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (user_id,))
    await db.commit()


async def all_user_ids(only_active: bool = True) -> list[int]:
    db = await get_db()
    sql = "SELECT user_id FROM users" + (" WHERE is_blocked = 0" if only_active else "")
    cur = await db.execute(sql)
    return [row[0] for row in await cur.fetchall()]


# ───────────────────────── Конфиги ─────────────────────────

# Аудитория в России, поэтому выдаём только то, до чего из России реально
# достучались: ru_nodes > 0. Ноль — заблокирован, минус один — ещё не
# проверяли, и то и другое пользователю отдавать нельзя.
# ru_min_nodes: сколько российских нод должны пройти TLS-хендшейк.
# Единственный подтверждённо рабочий конфиг дал 3 из 3, а всё, что
# у пользователя не открылось, — 1 или 2. Частичный успех означает,
# что часть маршрутов уже режется, и у конкретного провайдера
# такой конфиг скорее всего не заработает.
_BASE = "alive = 1 AND link LIKE 'vless://%'"

# Reality по TCP с XTLS Vision заметно живучее прочего: обычный TLS, ws и grpc
# ТСПУ режет на хендшейке, даже когда сервер отвечает и трафик через него идёт.
# Поэтому такие конфиги идут первыми, а не просто «побыстрее».
def _rank_sql() -> tuple[str, list]:
    """Порядок выдачи. Близкие страны первыми, дальше — по качеству конфига."""
    near = [str(c).upper() for c in (BOT.get("preferred_countries") or [])]
    head, params = "", []
    if near:
        head = f"(country IN ({','.join('?' * len(near))})) DESC, "
        params = near
    return head + (
        "source_priority DESC, "
        "(security = 'reality') DESC, "
        "(link LIKE '%flow=xtls-rprx-vision%') DESC, "
        "verified DESC, ru_nodes DESC, risk ASC, COALESCE(latency_ms, 9999) ASC"
    ), params


def _alive_sql() -> tuple[str, list]:
    """Условие выдачи и его параметры — география плюс профиль протокола."""
    where, params = [_BASE], []

    where.append("ru_nodes >= ?")
    params.append(int(BOT.get("ru_min_nodes", 3)))

    countries = BOT.get("allowed_countries") or []
    if countries:
        where.append(f"country IN ({','.join('?' * len(countries))})")
        params += [str(c).upper() for c in countries]

    if BOT.get("require_reality", True):
        where.append("security = 'reality'")
    if BOT.get("require_vision", False):
        where.append("network = 'tcp' AND link LIKE '%flow=xtls-rprx-vision%'")

    return " AND ".join(where), params


async def counts() -> tuple[int, int]:
    """(доступных конфигов, стран)."""
    db = await get_db()
    where, params = _alive_sql()
    cur = await db.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT NULLIF(country,'')) FROM configs WHERE {where}",
        params,
    )
    row = await cur.fetchone()
    return (row[0] or 0, row[1] or 0)


async def countries() -> list[tuple[str, str, int]]:
    """[(ISO, название, сколько)] по убыванию количества."""
    db = await get_db()
    where, params = _alive_sql()
    cur = await db.execute(
        f"""
        SELECT country, MAX(country_name) AS name, COUNT(*) AS n
          FROM configs
         WHERE {where} AND country IS NOT NULL AND country <> ''
         GROUP BY country
         ORDER BY n DESC, country ASC
        """,
        params,
    )
    return [(r["country"], r["name"] or r["country"], r["n"]) for r in await cur.fetchall()]


def pick_pool() -> int:
    """Из скольких лучших конфигов тянем случайный.

    Слишком узкая выборка бьёт по чужим бесплатным серверам: вся нагрузка
    ложится на несколько десятков, они быстрее умирают, а пользователи
    видят три страны вместо тридцати. Все кандидаты и так прошли xray
    и проверку из РФ — между ними разница лишь в пинге.
    """
    return max(10, BOT.get("pick_pool", 200))


async def pick_config(user_id: int) -> aiosqlite.Row | None:
    """Случайный рабочий конфиг из пула.

    Приоритет: достучались из РФ → подтверждён xray → низкий риск по ТСПУ →
    низкий пинг. Конфиги, которые юзер уже получал за последние 7 дней,
    стараемся не повторять.
    """
    db = await get_db()
    since = iso(utcnow() - timedelta(days=7))
    where, params = _alive_sql()

    rank, rparams = _rank_sql()
    cur = await db.execute(
        f"""
        SELECT c.* FROM configs c
         WHERE {where}
           AND c.id NOT IN (SELECT config_id FROM issued WHERE user_id = ? AND ts > ?)
         ORDER BY {rank}
         LIMIT ?
        """,
        (*params, user_id, since, *rparams, pick_pool()),
    )
    rows = await cur.fetchall()

    if not rows:  # всё уже выдавали — снимаем ограничение
        cur = await db.execute(
            f"""SELECT * FROM configs WHERE {where}
                ORDER BY {rank} LIMIT ?""",
            (*params, *rparams, pick_pool()),
        )
        rows = await cur.fetchall()

    return random.choice(rows) if rows else None


async def log_issue(user_id: int, config_id: int, country: str | None) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO issued(user_id, config_id, country, ts) VALUES(?,?,?,?)",
        (user_id, config_id, country, iso()),
    )
    await db.execute(
        "UPDATE users SET configs_taken = configs_taken + 1, last_seen = ? WHERE user_id = ?",
        (iso(), user_id),
    )
    await db.commit()


async def issued_today(user_id: int) -> int:
    db = await get_db()
    since = iso(utcnow() - timedelta(days=1))
    cur = await db.execute(
        "SELECT COUNT(*) FROM issued WHERE user_id = ? AND ts > ?", (user_id, since)
    )
    return (await cur.fetchone())[0]


# ───────────────────────── Статистика ─────────────────────────

async def stats() -> dict:
    db = await get_db()

    async def one(sql: str, args: tuple = ()) -> int:
        cur = await db.execute(sql, args)
        row = await cur.fetchone()
        return row[0] if row and row[0] is not None else 0

    d1 = iso(utcnow() - timedelta(days=1))
    d7 = iso(utcnow() - timedelta(days=7))
    midnight = iso(utcnow().replace(hour=0, minute=0, second=0, microsecond=0))

    where, wparams = _alive_sql()
    cur = await db.execute(
        f"""SELECT country, MAX(country_name) AS name, COUNT(*) n FROM configs
            WHERE {where} AND country <> '' GROUP BY country ORDER BY n DESC LIMIT 8""",
        wparams,
    )
    top = await cur.fetchall()

    cur = await db.execute(
        f"""SELECT latency_ms FROM configs WHERE {where} AND latency_ms IS NOT NULL
            ORDER BY latency_ms""",
        wparams,
    )
    lats = [r[0] for r in await cur.fetchall()]

    cur = await db.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    )
    run = await cur.fetchone()

    return {
        "users_total":   await one("SELECT COUNT(*) FROM users"),
        "users_24h":     await one("SELECT COUNT(*) FROM users WHERE last_seen > ?", (d1,)),
        "users_7d":      await one("SELECT COUNT(*) FROM users WHERE last_seen > ?", (d7,)),
        "users_today":   await one("SELECT COUNT(*) FROM users WHERE joined_at > ?", (midnight,)),
        "users_new_7d":  await one("SELECT COUNT(*) FROM users WHERE joined_at > ?", (d7,)),
        "users_blocked": await one("SELECT COUNT(*) FROM users WHERE is_blocked = 1"),
        "cfg_total":     await one("SELECT COUNT(*) FROM configs"),
        "cfg_alive":     await one(f"SELECT COUNT(*) FROM configs WHERE {where}", tuple(wparams)),
        "cfg_verified":  await one("SELECT COUNT(*) FROM configs WHERE verified = 1"),
        "cfg_ru_ok":     await one("SELECT COUNT(*) FROM configs WHERE ru_nodes > 0"),
        "cfg_ru_bad":    await one("SELECT COUNT(*) FROM configs WHERE ru_nodes = 0"),
        "cfg_ru_todo":   await one("SELECT COUNT(*) FROM configs WHERE ru_nodes = -1"),
        "cfg_countries": await one(
            f"SELECT COUNT(DISTINCT country) FROM configs WHERE {where} AND country <> ''",
            tuple(wparams)),
        "cfg_latency":   lats[len(lats) // 2] if lats else 0,
        "issued_total":  await one("SELECT COUNT(*) FROM issued"),
        "issued_24h":    await one("SELECT COUNT(*) FROM issued WHERE ts > ?", (d1,)),
        "top": [(r["country"], r["name"] or r["country"], r["n"]) for r in top],
        "run": dict(run) if run else None,
    }


async def start_broadcast(admin_id: int, preview: str, total: int) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO broadcasts(admin_id, preview, total, started_at) VALUES(?,?,?,?)",
        (admin_id, preview[:300], total, iso()),
    )
    await db.commit()
    return cur.lastrowid


async def finish_broadcast(bid: int, sent: int, failed: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE broadcasts SET sent=?, failed=?, finished_at=? WHERE id=?",
        (sent, failed, iso(), bid),
    )
    await db.commit()

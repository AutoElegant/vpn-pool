"""Забирает готовый пул, собранный на GitHub Actions, и вливает в локальную БД.

Это всё, что VPS делает с внешним миром: один HTTPS-запрос к GitHub раз в
несколько минут. Ни одного коннекта к VPN-серверам — значит хостеру нечего
принять за сканирование портов.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp

from . import rucheck
from .db import connect, init_db
from .parse import parse_link
from .settings import CONFIG
from .state import load_payload

log = logging.getLogger("sync")

SYNC = CONFIG.get("sync", {})


async def fetch_state(url: str, token: str | None) -> dict | None:
    # CDN GitHub отдаёт raw-файлы из кэша до пяти минут и заголовок
    # Cache-Control игнорирует, поэтому уникализируем сам адрес — иначе
    # сервер может влить обратно пул, который уже устарел.
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}_={int(time.time())}"
    headers = {"User-Agent": "vpnbot-sync", "Cache-Control": "no-cache"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = aiohttp.ClientTimeout(total=SYNC.get("timeout", 60))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return json.loads(await resp.text())
    except Exception as exc:
        log.error("не смог забрать пул: %s", exc)
        return None


def replace_pool(conn, data: dict) -> int:
    """Пул с раннера — источник истины: старые записи убираем целиком.

    Иначе конфиги, которые там уже признаны мёртвыми, продолжали бы
    выдаваться из местной базы.
    """
    incoming = data.get("pool") or []
    if not incoming:
        log.warning("в файле пустой пул — оставляю то, что есть")
        return 0

    # Результаты РФ-проверки живут только здесь: раннеру check-host их не
    # отдаёт, режет облачные адреса. Поэтому перед заливкой запоминаем свои
    # и возвращаем на место, иначе каждая синхронизация их обнуляла бы.
    local_ru = {
        r["fingerprint"]: (r["ru_nodes"], r["ru_checked_at"])
        for r in conn.execute(
            "SELECT fingerprint, ru_nodes, ru_checked_at FROM configs WHERE ru_nodes <> -1"
        )
    }

    keep = {item.get("fingerprint") for item in incoming}
    placeholders = ",".join("?" * len(keep))
    conn.execute(f"DELETE FROM configs WHERE fingerprint NOT IN ({placeholders})", tuple(keep))
    conn.commit()

    load_payload(conn, {"pool": incoming, "geo_cache": data.get("geo_cache")})

    restored = [(nodes, ts, fp) for fp, (nodes, ts) in local_ru.items() if fp in keep]
    conn.executemany(
        "UPDATE configs SET ru_nodes = ?, ru_checked_at = ? WHERE fingerprint = ?", restored
    )
    conn.commit()
    if restored:
        log.info("Сохранено прежних результатов РФ-проверки: %d", len(restored))
    total = conn.execute("SELECT COUNT(*) FROM configs WHERE alive = 1").fetchone()[0]
    ru_ok = conn.execute("SELECT COUNT(*) FROM configs WHERE ru_nodes > 0").fetchone()[0]
    log.info("Пул обновлён: %d рабочих, из них доступны из РФ %d (собран %s)",
             total, ru_ok, data.get("updated_at", "?"))
    return total


async def check_russia(conn) -> None:
    """Проверяем доступность из РФ порциями, по кругу.

    Здесь это дёшево и безопасно: несколько десятков HTTPS-запросов
    к одному домену раз в несколько минут — не то же самое, что коннекты
    к сотням разных адресов, за которые хостер присылает abuse.

    Порций за один заход может быть несколько: бот отдаёт только
    подтверждённые конфиги, и после прихода свежего пула нельзя ждать
    полчаса, пока проверка доберётся до всех.
    """
    for _ in range(max(1, SYNC.get("ru_check_rounds", 3))):
        if not await check_russia_round(conn):
            break


async def check_russia_round(conn) -> bool:
    """Одна порция. Возвращает False, если проверять больше нечего."""
    limit = SYNC.get("ru_check_limit", 150)
    stale_hours = SYNC.get("ru_recheck_hours", 3)
    horizon = (
        datetime.now(timezone.utc) - timedelta(hours=stale_hours)
    ).isoformat(timespec="seconds")

    rows = conn.execute(
        "SELECT id, link FROM configs WHERE alive = 1 "
        "  AND (ru_nodes = -1 OR ru_checked_at IS NULL OR ru_checked_at < ?) "
        "ORDER BY (ru_nodes = -1) DESC, ru_checked_at ASC LIMIT ?",
        (horizon, limit),
    ).fetchall()
    if not rows:
        return False

    targets = []
    for r in rows:
        cfg = parse_link(r["link"])
        if cfg:
            targets.append((r["id"], cfg))
    if not targets:
        return False

    results = await rucheck.tcp_from_russia(
        targets, concurrency=SYNC.get("ru_check_concurrency", 4)
    )
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "UPDATE configs SET ru_nodes = ?, ru_checked_at = ? WHERE id = ?",
        [(nodes, ts, cid) for cid, nodes in results.items()],
    )
    conn.commit()

    blocked = conn.execute("SELECT COUNT(*) FROM configs WHERE ru_nodes = 0").fetchone()[0]
    ok = conn.execute("SELECT COUNT(*) FROM configs WHERE ru_nodes > 0").fetchone()[0]
    todo = conn.execute(
        "SELECT COUNT(*) FROM configs WHERE ru_nodes = -1 AND alive = 1"
    ).fetchone()[0]
    log.info("Из РФ: доступно %d, заблокировано %d, ещё не проверено %d", ok, blocked, todo)
    return todo > 0


def record_run(conn, data: dict) -> None:
    """Отмечаем сбор в таблице runs — из неё админка берёт «Последний сбор».

    Сам сбор идёт на раннере, сюда приезжает только результат, поэтому
    запись заводится при синхронизации. На один пул — одна строка: каждые
    десять минут она обновляется свежими данными РФ-проверки, а новая
    появляется, когда раннер опубликует следующий пул.
    """
    collected = data.get("updated_at") or ""
    pool = conn.execute("SELECT COUNT(*) FROM configs").fetchone()[0]
    verified = conn.execute("SELECT COUNT(*) FROM configs WHERE verified = 1").fetchone()[0]
    ru_ok = conn.execute("SELECT COUNT(*) FROM configs WHERE ru_nodes > 0").fetchone()[0]
    ru_bad = conn.execute("SELECT COUNT(*) FROM configs WHERE ru_nodes = 0").fetchone()[0]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    row = conn.execute("SELECT id FROM runs WHERE started_at = ?", (collected,)).fetchone()
    if row:
        conn.execute(
            "UPDATE runs SET finished_at=?, alive=?, verified=?, pool=?, ru_ok=?, dropped=? "
            "WHERE id=?",
            (now, pool, verified, pool, ru_ok, ru_bad, row[0]),
        )
    else:
        conn.execute(
            "INSERT INTO runs(started_at, finished_at, alive, verified, pool, ru_ok, dropped) "
            "VALUES(?,?,?,?,?,?,?)",
            (collected, now, pool, verified, pool, ru_ok, ru_bad),
        )
    conn.commit()


async def run_once() -> int:
    url = os.getenv("POOL_URL") or SYNC.get("url", "")
    if not url:
        log.error("не задан POOL_URL (в .env) или sync.url (в config.yml)")
        return 0
    data = await fetch_state(url, os.getenv("GITHUB_TOKEN"))
    if data is None:
        return 0

    init_db()
    conn = connect()
    try:
        total = replace_pool(conn, data)
        if SYNC.get("ru_check", True):
            await check_russia(conn)
        record_run(conn, data)
        return total
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Синхронизация пула конфигов с GitHub")
    ap.add_argument("--loop", type=int, metavar="MIN", help="крутиться с интервалом в минутах")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        datefmt="%H:%M:%S",
    )

    async def runner() -> None:
        while True:
            try:
                await run_once()
            except Exception:
                log.exception("синхронизация упала")
            if not args.loop:
                return
            await asyncio.sleep(args.loop * 60)

    asyncio.run(runner())


if __name__ == "__main__":
    main()

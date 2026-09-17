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

import aiohttp

from .db import connect, init_db
from .settings import CONFIG
from .state import FIELDS, load_payload

log = logging.getLogger("sync")

SYNC = CONFIG.get("sync", {})


async def fetch_state(url: str, token: str | None) -> dict | None:
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

    keep = {item.get("fingerprint") for item in incoming}
    placeholders = ",".join("?" * len(keep))
    conn.execute(f"DELETE FROM configs WHERE fingerprint NOT IN ({placeholders})", tuple(keep))
    conn.commit()

    load_payload(conn, {"pool": incoming, "geo_cache": data.get("geo_cache")})
    total = conn.execute("SELECT COUNT(*) FROM configs WHERE alive = 1").fetchone()[0]
    ru_ok = conn.execute("SELECT COUNT(*) FROM configs WHERE ru_nodes > 0").fetchone()[0]
    log.info("Пул обновлён: %d рабочих, из них доступны из РФ %d (собран %s)",
             total, ru_ok, data.get("updated_at", "?"))
    return total


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
        return replace_pool(conn, data)
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

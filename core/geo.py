"""Определение страны сервера через ip-api.com (batch, бесплатно, без ключа)."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import aiohttp

log = logging.getLogger("geo")

BATCH_URL = "http://ip-api.com/batch?fields=status,country,countryCode,city,query"
BATCH_SIZE = 100
BATCH_PAUSE = 4.5  # ip-api: 15 batch-запросов в минуту


def flag(code: str | None) -> str:
    """ISO-код страны -> эмодзи флага."""
    if not code or len(code) != 2 or not code.isalpha():
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code.upper())


def load_cache(conn: sqlite3.Connection, ttl_days: int) -> dict[str, tuple]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    rows = conn.execute(
        "SELECT ip, country, country_name, city FROM geo_cache WHERE updated_at > ?",
        (cutoff,),
    ).fetchall()
    return {r["ip"]: (r["country"], r["country_name"], r["city"]) for r in rows}


def save_cache(conn: sqlite3.Connection, results: dict[str, tuple]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO geo_cache(ip, country, country_name, city, updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(ip) DO UPDATE SET country=excluded.country, "
        "country_name=excluded.country_name, city=excluded.city, updated_at=excluded.updated_at",
        [(ip, *vals, now) for ip, vals in results.items()],
    )
    conn.commit()


async def lookup(ips: list[str]) -> dict[str, tuple[str, str, str]]:
    """Возвращает {ip: (ISO, название страны, город)}."""
    out: dict[str, tuple[str, str, str]] = {}
    if not ips:
        return out

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i in range(0, len(ips), BATCH_SIZE):
            chunk = ips[i : i + BATCH_SIZE]
            try:
                async with session.post(BATCH_URL, json=chunk) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(20)
                        continue
                    data = await resp.json(content_type=None)
            except Exception as exc:  # сеть/таймаут — просто пропускаем пачку
                log.warning("geo batch failed: %s", exc)
                await asyncio.sleep(BATCH_PAUSE)
                continue

            for item in data or []:
                if isinstance(item, dict) and item.get("status") == "success":
                    out[item["query"]] = (
                        item.get("countryCode") or "",
                        item.get("country") or "",
                        item.get("city") or "",
                    )
            if i + BATCH_SIZE < len(ips):
                await asyncio.sleep(BATCH_PAUSE)
    return out

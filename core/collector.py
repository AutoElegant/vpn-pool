"""Коллектор: качает источники → парсит → дедуп → гео → проверка → БД."""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import aiohttp

from . import geo, rucheck, state
from .checker import deep_check, quick_check, resolve_many, xray_path
from .db import connect, init_db
from .parse import ProxyConfig, extract_links, parse_link
from .settings import BOT_USERNAME, COLLECTOR, SOURCES

log = logging.getLogger("collector")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/127.0 Safari/537.36"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ───────────────────────── 1. Загрузка источников ─────────────────────────

async def fetch_source(session: aiohttp.ClientSession, src: dict) -> list[ProxyConfig]:
    name, url = src.get("name", src["url"]), src["url"]
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            text = await resp.text(errors="ignore")
    except Exception as exc:
        log.warning("[%s] не скачалось: %s", name, exc)
        return []

    wanted = set(COLLECTOR.get("protocols", ["vless"]))
    limit = COLLECTOR.get("max_links_per_source", 20000)

    configs: list[ProxyConfig] = []
    for link in extract_links(text)[:limit]:
        if link.split("://", 1)[0].lower() not in wanted:
            continue
        cfg = parse_link(link, source=name)
        if cfg:
            configs.append(cfg)
    log.info("[%s] %d конфигов", name, len(configs))
    return configs


async def fetch_all() -> dict[str, ProxyConfig]:
    timeout = aiohttp.ClientTimeout(total=COLLECTOR.get("fetch_timeout", 30))
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA}) as session:
        batches = await asyncio.gather(*(fetch_source(session, s) for s in SOURCES))

    unique: dict[str, ProxyConfig] = {}
    for batch in batches:
        for cfg in batch:
            unique.setdefault(cfg.fingerprint, cfg)
    log.info("Всего уникальных: %d (из %d)", len(unique), sum(len(b) for b in batches))
    return unique


# ───────────────────────── 2. Запись в БД ─────────────────────────

def known_fingerprints(conn: sqlite3.Connection) -> set[str]:
    """Что уже в пуле + что недавно не ответило — таких в кандидаты не берём."""
    rows = conn.execute("SELECT fingerprint FROM configs").fetchall()
    dead = conn.execute(
        "SELECT fingerprint FROM deadlist WHERE until > ?", (now(),)
    ).fetchall()
    return {r[0] for r in rows} | {r[0] for r in dead}


def pick_candidates(conn: sqlite3.Connection, fetched: dict[str, ProxyConfig]) -> dict[str, ProxyConfig]:
    """Свежая порция новых конфигов — ровно столько, сколько потянет сервер."""
    known = known_fingerprints(conn)
    new = [fp for fp in fetched if fp not in known]

    # Дешёвый фильтр до всякой сети: палевный SNI, отсутствие TLS и прочее,
    # что ТСПУ режет на хендшейке, даже когда сервер жив и отвечает.
    threshold = COLLECTOR.get("risk_threshold", 60)
    viable = [fp for fp in new if rucheck.censorship_risk(fetched[fp])[0] < threshold]
    log.info("Новых в источниках: %d, пригодны для РФ по признакам: %d", len(new), len(viable))

    random.shuffle(viable)
    batch = viable[: COLLECTOR.get("candidate_batch", 400)]
    log.info("Берём в проверку: %d", len(batch))
    return {fp: fetched[fp] for fp in batch}


def upsert(conn: sqlite3.Connection, configs: dict[str, ProxyConfig]) -> int:
    ts = now()
    before = conn.execute("SELECT COUNT(*) FROM configs").fetchone()[0]
    conn.executemany(
        """
        INSERT INTO configs(fingerprint, link, protocol, host, port, security,
                            network, sni, source, risk, first_seen, last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(fingerprint) DO UPDATE SET last_seen=excluded.last_seen
        """,
        [
            (fp, c.to_link(""), c.protocol, c.host, c.port, c.security,
             c.network, c.sni, c.source, rucheck.censorship_risk(c)[0], ts, ts)
            for fp, c in configs.items()
        ],
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM configs").fetchone()[0] - before


def touch_seen(conn: sqlite3.Connection, fingerprints: list[str]) -> None:
    """Отмечаем, что конфиг всё ещё публикуется источниками."""
    conn.executemany(
        "UPDATE configs SET last_seen = ? WHERE fingerprint = ?",
        [(now(), fp) for fp in fingerprints],
    )
    conn.commit()


def load_for_check(conn: sqlite3.Connection) -> list[tuple[int, ProxyConfig]]:
    rows = conn.execute("SELECT id, link, source FROM configs").fetchall()
    out = []
    for row in rows:
        cfg = parse_link(row["link"], row["source"] or "")
        if cfg:
            out.append((row["id"], cfg))
    return out


# ───────────────────────── 3. Проверка ─────────────────────────

async def run_quick(
    conn: sqlite3.Connection, items: list[tuple[int, ProxyConfig]]
) -> dict[int, tuple[bool, int | None]]:
    # DNS отдельным этапом: иначе сотни параллельных коннектов по доменам
    # забивают пул потоков getaddrinfo и проход встаёт намертво.
    dns = await resolve_many({cfg.host for _, cfg in items})
    conn.executemany(
        "UPDATE configs SET ip = ? WHERE id = ?",
        [(dns.get(cfg.host), cid) for cid, cfg in items if dns.get(cfg.host)],
    )
    conn.commit()

    timeout = COLLECTOR.get("connect_timeout", 4)
    sem = asyncio.Semaphore(COLLECTOR.get("concurrency", 20))
    results: dict[int, tuple[bool, int | None]] = {}

    # Ограничиваем темп новых назначений. Тысяча коннектов на разные IP
    # за минуту — для netflow-детектора хостера это горизонтальный скан
    # портов со всеми вытекающими. Растянутые во времени — обычный трафик.
    rate = max(1.0, float(COLLECTOR.get("scan_rate", 8)))

    async def worker(index: int, cid: int, cfg: ProxyConfig) -> None:
        await asyncio.sleep(index / rate)
        async with sem:
            results[cid] = await quick_check(cfg, timeout, dns.get(cfg.host))

    log.info("Проверяю %d адресов в темпе %.0f/сек (~%.0f сек)",
             len(items), rate, len(items) / rate)
    await asyncio.gather(*(worker(i, cid, cfg) for i, (cid, cfg) in enumerate(items)))
    alive = sum(1 for ok, _ in results.values() if ok)
    log.info("Быстрая проверка: %d/%d живых", alive, len(results))
    return results


async def run_deep(items: list[tuple[int, ProxyConfig]], xray_bin: str) -> dict[int, tuple[bool, int | None]]:
    url = COLLECTOR.get("deep_check_url", "http://cp.cloudflare.com/generate_204")
    timeout = COLLECTOR.get("deep_check_timeout", 8)
    sem = asyncio.Semaphore(COLLECTOR.get("deep_check_concurrency", 25))
    results: dict[int, tuple[bool, int | None]] = {}
    done = 0

    async def worker(cid: int, cfg: ProxyConfig) -> None:
        nonlocal done
        async with sem:
            results[cid] = await deep_check(cfg, xray_bin, url, timeout)
            done += 1
            if done % 100 == 0:
                log.info("  xray-проверка: %d/%d", done, len(items))

    await asyncio.gather(*(worker(cid, cfg) for cid, cfg in items))
    ok = sum(1 for good, _ in results.values() if good)
    log.info("Глубокая проверка: %d/%d реально работают", ok, len(results))
    return results


async def run_ru_check(
    conn: sqlite3.Connection, items: list[tuple[int, ProxyConfig]]
) -> None:
    """Проверяем с нод в Москве и Питере, доходит ли TCP до сервера.

    Гоняем только тех, кто уже подтверждён xray — их немного, а check-host
    ограничен по частоте запросов.
    """
    if not COLLECTOR.get("ru_check", True):
        return

    # Сначала те, кого ещё не проверяли, затем самые давние — так пул
    # перепроверяется по кругу, а не целиком каждый проход.
    ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM configs WHERE alive = 1 AND verified = 1 "
            "ORDER BY (ru_nodes = -1) DESC, ru_checked_at ASC LIMIT ?",
            (COLLECTOR.get("ru_check_limit", 300),),
        ).fetchall()
    ]
    order = {cid: i for i, cid in enumerate(ids)}
    targets = sorted(
        ((cid, cfg) for cid, cfg in items if cid in order), key=lambda x: order[x[0]]
    )
    if not targets:
        return

    log.info("Проверяю доступность из РФ для %d конфигов…", len(targets))
    results = await rucheck.tcp_from_russia(
        targets, concurrency=COLLECTOR.get("ru_check_concurrency", 4)
    )
    ts = now()
    conn.executemany(
        "UPDATE configs SET ru_nodes = ?, ru_checked_at = ? WHERE id = ?",
        [(nodes, ts, cid) for cid, nodes in results.items()],
    )
    # Недоступность из РФ — такой же промах, как отказ соединения:
    # копим подряд идущие, чтобы не выбрасывать конфиг из-за одного сбоя.
    conn.executemany(
        "UPDATE configs SET fail_streak = fail_streak + 1 WHERE id = ?",
        [(cid,) for cid, nodes in results.items() if nodes == 0],
    )
    conn.executemany(
        "UPDATE configs SET fail_streak = 0 WHERE id = ?",
        [(cid,) for cid, nodes in results.items() if nodes > 0],
    )
    conn.commit()


def apply_results(
    conn: sqlite3.Connection,
    quick: dict[int, tuple[bool, int | None]],
    deep: dict[int, tuple[bool, int | None]],
) -> None:
    ts = now()
    rows = []
    for cid, (q_ok, q_ms) in quick.items():
        if cid in deep:
            ok, ms, verified = deep[cid][0], deep[cid][1] or q_ms, 1 if deep[cid][0] else 0
        else:
            ok, ms, verified = q_ok, q_ms, 0
        rows.append((1 if ok else 0, verified, ms, ts if ok else None, cid))

    conn.executemany(
        """
        UPDATE configs
           SET alive       = ?,
               verified    = ?,
               latency_ms  = COALESCE(?, latency_ms),
               last_ok     = COALESCE(?, last_ok),
               checks      = checks + 1,
               oks         = oks + ?,
               fail_streak = CASE WHEN ? = 1 THEN 0 ELSE fail_streak + 1 END
         WHERE id = ?
        """,
        [(a, v, ms, lok, a, a, cid) for a, v, ms, lok, cid in rows],
    )
    conn.commit()


# ───────────────────────── 4. Гео ─────────────────────────

async def enrich_geo(conn: sqlite3.Connection) -> None:
    """Страна/город по IP. IP уже проставлены на этапе быстрой проверки."""
    if not COLLECTOR.get("geo_enabled", True):
        return

    rows = conn.execute(
        "SELECT id, ip FROM configs WHERE alive = 1 AND ip IS NOT NULL"
    ).fetchall()
    if not rows:
        return

    cache = geo.load_cache(conn, COLLECTOR.get("geo_ttl_days", 30))
    unknown = sorted({r["ip"] for r in rows if r["ip"] not in cache})
    if unknown:
        log.info("Гео-запрос для %d новых IP…", len(unknown))
        fresh = await geo.lookup(unknown)
        if fresh:
            geo.save_cache(conn, fresh)
            cache.update(fresh)

    updates = [(*cache[r["ip"]], r["id"]) for r in rows if r["ip"] in cache]
    conn.executemany(
        "UPDATE configs SET country=?, country_name=?, city=? WHERE id=?", updates
    )
    conn.commit()
    log.info("Гео проставлено для %d конфигов", len(updates))


# ───────────────────────── 5. Уборка ─────────────────────────

def trim_pool(conn: sqlite3.Connection) -> tuple[int, int]:
    """Оставляем pool_size лучших плюс тех, кто оступился разок.

    Ключевое разделение: из выдачи конфиг пропадает сразу, как только
    перестал отвечать (бот берёт только alive = 1), но из базы удаляется
    лишь после grace_fails промахов подряд. Иначе секундное моргание
    сервера или сбой ноды check-host навсегда выкидывали бы рабочий конфиг.
    """
    pool_size = COLLECTOR.get("pool_size", 500)
    grace = COLLECTOR.get("grace_fails", 3)
    ttl_days = COLLECTOR.get("dead_ttl_days", 3)
    until = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat(timespec="seconds")

    # Рабочее ядро: отвечает и доступно из России.
    keep = {
        r[0] for r in conn.execute(
            "SELECT id FROM configs WHERE alive = 1 AND ru_nodes <> 0 "
            "ORDER BY ru_nodes DESC, verified DESC, risk ASC, "
            "COALESCE(latency_ms, 9999) ASC LIMIT ?",
            (pool_size,),
        ).fetchall()
    }
    # Испытательный срок: недавно работали, промахнулись меньше grace раз.
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    grace_ids = {
        r[0] for r in conn.execute(
            "SELECT id FROM configs WHERE fail_streak BETWEEN 1 AND ? "
            "AND last_ok IS NOT NULL AND last_ok > ?",
            (grace - 1, recent),
        ).fetchall()
    }
    keep |= grace_ids

    # В чёрный список — только исчерпавшие попытки.
    dead = conn.execute(
        "SELECT fingerprint FROM configs WHERE checks > 0 AND fail_streak >= ?",
        (grace,),
    ).fetchall()
    conn.executemany(
        "INSERT INTO deadlist(fingerprint, until) VALUES(?,?) "
        "ON CONFLICT(fingerprint) DO UPDATE SET until=excluded.until",
        [(r[0], until) for r in dead],
    )

    if keep:
        placeholders = ",".join("?" * len(keep))
        cur = conn.execute(f"DELETE FROM configs WHERE id NOT IN ({placeholders})", tuple(keep))
    else:
        cur = conn.execute("DELETE FROM configs WHERE checks > 0")

    conn.execute("DELETE FROM deadlist WHERE until < ?", (now(),))
    conn.commit()

    live = conn.execute(
        "SELECT COUNT(*) FROM configs WHERE alive = 1 AND ru_nodes <> 0"
    ).fetchone()[0]
    log.info("Пул: %d рабочих, %d на испытательном сроке, удалено %d (в чёрный список %d)",
             live, len(grace_ids), cur.rowcount, len(dead))
    return live, cur.rowcount


# ───────────────────────── 6. Именование ─────────────────────────

def retag(conn: sqlite3.Connection) -> None:
    """Переименовывает конфиги в человекочитаемый вид для списка серверов в Happ."""
    suffix = f" | @{BOT_USERNAME}" if BOT_USERNAME else ""
    rows = conn.execute(
        "SELECT id, link, country, city FROM configs WHERE alive = 1"
    ).fetchall()
    updates = []
    for row in rows:
        cfg = parse_link(row["link"])
        if not cfg:
            continue
        bits = [geo.flag(row["country"])]
        if row["country"]:
            bits.append(row["country"])
        if row["city"]:
            bits.append(row["city"])
        updates.append((cfg.to_link(" · ".join(bits) + suffix), row["id"]))
    conn.executemany("UPDATE configs SET link = ? WHERE id = ?", updates)
    conn.commit()


# ───────────────────────── main ─────────────────────────

async def run_once(
    skip_deep: bool = False,
    state_path: str | None = None,
    pool_path: str | None = None,
) -> None:
    init_db()
    conn = connect()
    if state_path:
        state.import_state(conn, state_path)
    run_id = conn.execute("INSERT INTO runs(started_at) VALUES(?)", (now(),)).lastrowid
    conn.commit()

    # 1. Источники → 2. свежая порция кандидатов (пул + она и будут проверяться)
    fetched = await fetch_all()
    candidates = pick_candidates(conn, fetched)
    added = upsert(conn, candidates)
    touch_seen(conn, [fp for fp in fetched if fp not in candidates])

    # 3. Проверка: текущий пул + кандидаты
    items = load_for_check(conn)
    log.info("К проверке: %d конфигов (пул + кандидаты)", len(items))
    quick = await run_quick(conn, items)

    deep: dict[int, tuple[bool, int | None]] = {}
    mode = COLLECTOR.get("deep_check", "auto")
    xray = xray_path()
    want_deep = not skip_deep and (mode is True or (mode == "auto" and xray))
    if want_deep and not xray:
        log.warning("deep_check включён, но бинарь xray не найден — пропускаю")
    elif want_deep:
        alive_now = sorted(
            ((cid, cfg) for cid, cfg in items if quick.get(cid, (False, None))[0]),
            key=lambda x: quick[x[0]][1] or 9999,
        )[: COLLECTOR.get("deep_check_limit", 300)]
        log.info("Гоняю через xray %d конфигов…", len(alive_now))
        deep = await run_deep(alive_now, xray)

    apply_results(conn, quick, deep)
    # Порядок важен: сперва отсеиваем недоступных из РФ, и только потом
    # режем пул и зовём гео — иначе тратим их на конфиги, которые всё равно уйдут.
    await run_ru_check(conn, items)
    pool, dropped = trim_pool(conn)
    await enrich_geo(conn)
    retag(conn)

    verified = conn.execute("SELECT COUNT(*) FROM configs WHERE verified=1").fetchone()[0]
    ru_ok = conn.execute("SELECT COUNT(*) FROM configs WHERE ru_nodes > 0").fetchone()[0]
    conn.execute(
        "UPDATE runs SET finished_at=?, fetched=?, new_configs=?, checked=?, "
        "alive=?, verified=?, dropped=?, pool=?, ru_ok=? WHERE id=?",
        (now(), len(fetched), added, len(quick), pool, verified, dropped, pool, ru_ok, run_id),
    )
    conn.commit()
    if state_path:
        state.export_state(conn, state_path, pool_path=pool_path)
    conn.close()
    log.info("ГОТОВО: в пуле %d, подтверждено xray %d, доступно из РФ %d",
             pool, verified, ru_ok)


def main() -> None:
    ap = argparse.ArgumentParser(description="Сборщик бесплатных VLESS-конфигов")
    ap.add_argument("--loop", type=int, metavar="MIN",
                    help="крутиться в цикле с интервалом в минутах")
    ap.add_argument("--skip-deep", action="store_true",
                    help="без проверки через xray (только TCP/TLS)")
    ap.add_argument("--state", metavar="PATH",
                    help="JSON с пулом и чёрным списком: читается в начале, "
                         "пишется в конце (нужно для эфемерных раннеров CI)")
    ap.add_argument("--pool", metavar="PATH",
                    help="куда положить урезанный файл только с пулом (для сервера)")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="брать не больше N ссылок из каждого источника (для тестов)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.limit:
        COLLECTOR["max_links_per_source"] = args.limit

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        datefmt="%H:%M:%S",
    )

    async def runner() -> None:
        while True:
            try:
                await run_once(skip_deep=args.skip_deep, state_path=args.state,
                               pool_path=args.pool)
            except Exception:
                log.exception("проход упал")
            if not args.loop:
                return
            log.info("Следующий проход через %d мин", args.loop)
            await asyncio.sleep(args.loop * 60)

    asyncio.run(runner())


if __name__ == "__main__":
    main()

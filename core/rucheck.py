"""Проверка доступности сервера ИЗ РОССИИ.

Коллектор крутится на европейском VPS, где нет ТСПУ, поэтому его проверки
говорят только «сервер жив», но не «до него дойдут из РФ». Здесь два сигнала:

1. `tcp_from_russia()` — реальная TCP-проверка с нод check-host.net,
   стоящих в Москве и Питере. Ловит блокировку по IP.
2. `censorship_risk()` — эвристики по самой ссылке. Ловит то, что режется
   уже после TCP: палевный SNI, отсутствие маскировки, самоподписанный серт.
"""
from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

from .parse import ProxyConfig

log = logging.getLogger("rucheck")

API = "https://check-host.net"
# Браузерный User-Agent обязателен: без него check-host отвечает 403.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0 Safari/537.36")
RU_NODES = ("ru1.node.check-host.net", "ru2.node.check-host.net", "ru3.node.check-host.net")
HEADERS = {"Accept": "application/json", "User-Agent": UA}

UNKNOWN = -1  # проверить не удалось (лимиты API, сеть) — не путать с 0 «заблокирован»


# ───────────────────── 1. TCP из РФ через check-host ─────────────────────

async def _submit(session: aiohttp.ClientSession, host: str, port: int) -> str | None:
    """Ставим проверку HTTPS, а не голого TCP.

    ТСПУ режет не соединение, а TLS-хендшейк: порт отвечает, а рукопожатие
    не проходит. check-http доводит дело до хендшейка и возвращает HTTP-код,
    если сервер ответил — для Reality это ответ замаскированного сайта.
    Голая TCP-проверка этого различить не может в принципе.
    """
    params = [("host", f"https://{host}:{port}")] + [("node", n) for n in RU_NODES]
    for attempt in range(3):
        try:
            async with session.get(f"{API}/check-http", params=params, headers=HEADERS) as resp:
                if resp.status == 429:          # упёрлись в лимит — подождём и повторим
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                return data.get("request_id")
        except Exception:
            await asyncio.sleep(1)
    return None


async def _result(session: aiohttp.ClientSession, request_id: str) -> tuple[int, int, int]:
    """(достучались, ответили, всего нод). Ноды отвечают не одновременно."""
    try:
        async with session.get(f"{API}/check-result/{request_id}", headers=HEADERS) as resp:
            if resp.status != 200:
                return 0, 0, 0
            data = await resp.json(content_type=None)
    except Exception:
        return 0, 0, 0

    if not isinstance(data, dict):
        return 0, 0, 0

    ok = answered = 0
    for res in data.values():
        if res is None:            # нода ещё считает
            continue
        answered += 1
        # Ответ ноды: [[успех, время, сообщение, http-код, ip]].
        # Хендшейк состоялся тогда и только тогда, когда пришёл http-код;
        # при блокировке там null и «Connection timed out».
        if (isinstance(res, list) and res and isinstance(res[0], list)
                and len(res[0]) >= 4 and res[0][3]):
            ok += 1
    return ok, answered, len(data)


async def tcp_from_russia(
    configs: list[tuple[int, ProxyConfig]],
    concurrency: int = 4,
    max_polls: int = 6,
    poll_interval: float = 6.0,
) -> dict[int, int]:
    """{config_id: сколько нод из РФ достучались (0..3), либо UNKNOWN}.

    Две оптимизации, без которых проверка сотен конфигов занимает десятки минут:

    1. Дедупликация по host:port. Один сервер обычно раздаёт несколько
       конфигов (разные UUID и порты), а доступность у них общая —
       это экономит около трети запросов.
    2. Запросы идут пачками параллельно, а не по одному с паузами.
       Сначала ставим все задачи в очередь check-host, потом забираем
       результаты, опрашивая каждый до тех пор, пока не ответят все три
       ноды. Однократное чтение через фиксированную паузу не годится:
       на пачке в двести адресов ноды попросту не успевают, и почти всё
       возвращается как «не проверено».
    """
    if not configs:
        return {}

    # (host, port) → какие конфиги за ним стоят
    endpoints: dict[tuple[str, int], list[int]] = {}
    for cid, cfg in configs:
        endpoints.setdefault((cfg.host, cfg.port), []).append(cid)

    results: dict[int, int] = {cid: UNKNOWN for cid, _ in configs}
    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def submit(ep: tuple[str, int]) -> tuple[tuple[str, int], str | None]:
            async with sem:
                return ep, await _submit(session, ep[0], ep[1])

        submitted = await asyncio.gather(*(submit(ep) for ep in endpoints))
        pending = [(ep, rid) for ep, rid in submitted if rid]

        if not pending:
            log.warning("check-host не принял ни одной задачи — пропускаю РФ-проверку")
            return results

        # Даём нодам фору, пропорциональную размеру пачки.
        settle = min(30.0, 8.0 + len(pending) / 25)
        log.info("check-host: %d адресов на %d конфигов, жду %.0f с",
                 len(pending), len(configs), settle)
        await asyncio.sleep(settle)

        async def fetch(ep: tuple[str, int], rid: str) -> tuple[tuple[str, int], int]:
            ok = answered = 0
            for attempt in range(max_polls):
                async with sem:
                    ok, answered, total = await _result(session, rid)
                if total and answered >= total:      # ответили все ноды
                    return ep, ok
                await asyncio.sleep(poll_interval)
            # Время вышло: считаем по тем, кто успел ответить.
            return ep, (ok if answered else UNKNOWN)

        for ep, nodes in await asyncio.gather(*(fetch(ep, rid) for ep, rid in pending)):
            for cid in endpoints[ep]:
                results[cid] = nodes

    reachable = sum(1 for v in results.values() if v > 0)
    unknown = sum(1 for v in results.values() if v == UNKNOWN)
    log.info("Доступны из РФ: %d/%d (не удалось проверить: %d)",
             reachable, len(results), unknown)
    return results


# ───────────────────── 2. Эвристики по самой ссылке ─────────────────────

# SNI должен выглядеть как настоящий домен настоящего сайта.
_DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

# Такой SNI ТСПУ отправляет в блок, даже не глядя на остальное.
_BAD_SNI_TOKENS = (
    "rkn", "vpn", "proxy", "shadow", "v2ray", "xray", "trojan", "vless",
    "free", "tunnel", "bypass", "censor", "fuck", "zzz", "test", "example",
)

# TLD, которых в реальном трафике почти не бывает — маскировка не сработает.
_BAD_TLD = ("rkn", "local", "lan", "internal", "invalid", "onion", "test")


def censorship_risk(cfg: ProxyConfig) -> tuple[int, str]:
    """(штрафные очки, причина). 0 — выглядит пригодным для РФ.

    Чем выше очки, тем вероятнее, что ТСПУ прибьёт соединение уже
    на TLS-хендшейке, даже если сам сервер отвечает.
    """
    score, reasons = 0, []
    p = cfg.params
    sec = cfg.security
    sni = (cfg.sni or "").lower().strip()

    # Без TLS через ТСПУ ловить нечего — голый VLESS виден насквозь.
    if sec not in ("tls", "reality"):
        score += 100
        reasons.append("без TLS")

    # Самоподписанный сертификат — классический маркер прокси.
    if p.get("allowInsecure") in ("1", "true") or p.get("insecure") in ("1", "true"):
        score += 100
        reasons.append("allowInsecure")

    if not sni:
        score += 100
        reasons.append("нет SNI")
    else:
        if not _DOMAIN_RE.match(sni):
            score += 100
            reasons.append(f"SNI не домен ({sni})")
        else:
            tld = sni.rsplit(".", 1)[-1]
            if tld in _BAD_TLD:
                score += 100
                reasons.append(f"мусорный TLD (.{tld})")
            hit = next((t for t in _BAD_SNI_TOKENS if t in sni), None)
            if hit:
                score += 100
                reasons.append(f"палевный SNI ({sni})")
        # SNI, совпадающий с адресом сервера, маскировкой не является
        if sni == cfg.host.lower():
            score += 60
            reasons.append("SNI = адрес сервера")

    # Reality держится заметно лучше обычного TLS.
    if sec == "reality":
        score -= 20
    # 443 выглядит как обычный HTTPS; экзотический порт сам по себе приметен.
    if cfg.port != 443:
        score += 10
        reasons.append(f"порт {cfg.port}")

    return max(score, 0), ", ".join(reasons) or "ок"


def is_ru_viable(cfg: ProxyConfig, threshold: int = 60) -> bool:
    return censorship_risk(cfg)[0] < threshold

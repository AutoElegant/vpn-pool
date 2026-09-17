"""Проверка живости конфигов.

Два уровня:
  1. quick  — DNS + TCP-коннект (+ TLS-хендшейк, если security=tls/reality).
              Быстро, отсеивает мёртвые IP и закрытые порты.
  2. deep   — реальный HTTP-запрос через xray-core: поднимаем локальный
              socks5-инбаунд с этим конфигом и ходим за 204. Единственный
              способ убедиться, что UUID ещё валиден и сервер реально проксирует.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import shutil
import socket
import ssl
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .parse import ProxyConfig

log = logging.getLogger("checker")

_TLS_CTX = ssl.create_default_context()
_TLS_CTX.check_hostname = False
_TLS_CTX.verify_mode = ssl.CERT_NONE
with contextlib.suppress(Exception):
    _TLS_CTX.set_alpn_protocols(["h2", "http/1.1"])


# ───────────────────────── DNS ─────────────────────────

def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


async def resolve(host: str, cache: dict[str, str | None]) -> str | None:
    if host in cache:
        return cache[host]
    if _is_ip(host):
        cache[host] = host
        return host

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP), timeout=5
        )
        ip = infos[0][4][0] if infos else None
    except Exception:
        ip = None
    cache[host] = ip
    return ip


async def resolve_many(hosts: set[str], workers: int = 64) -> dict[str, str | None]:
    """Резолвим все хосты разом, до подключений.

    Важно: getaddrinfo выполняется в пуле потоков, и asyncio.wait_for его не
    прерывает — поток висит до системного таймаута. Если резолвить лениво
    из сотен параллельных коннектов, дефолтный пул (5 потоков на 1 CPU)
    намертво забивается мёртвыми доменами и весь проход встаёт.
    Поэтому: отдельный широкий пул и один резолв на уникальный хост.
    """
    cache: dict[str, str | None] = {h: h for h in hosts if _is_ip(h)}
    domains = [h for h in hosts if h not in cache]
    if not domains:
        return cache

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dns")
    previous = getattr(loop, "_default_executor", None)
    loop.set_default_executor(executor)
    sem = asyncio.Semaphore(workers)

    async def one(host: str) -> None:
        async with sem:
            await resolve(host, cache)

    try:
        await asyncio.gather(*(one(h) for h in domains))
    finally:
        loop.set_default_executor(previous) if previous else None
        executor.shutdown(wait=False)

    ok = sum(1 for h in domains if cache.get(h))
    log.info("DNS: разрезолвлено %d/%d доменов", ok, len(domains))
    return cache


# ───────────────────── Быстрая проверка ─────────────────────

async def quick_check(
    cfg: ProxyConfig, timeout: float, ip: str | None = None
) -> tuple[bool, int | None]:
    """Возвращает (жив, задержка в мс). ip — заранее разрезолвленный адрес."""
    if ip is None:
        return False, None
    started = time.perf_counter()
    writer = None
    try:
        use_tls = cfg.security in ("tls", "reality")
        coro = asyncio.open_connection(
            ip,
            cfg.port,
            ssl=_TLS_CTX if use_tls else None,
            server_hostname=cfg.sni if use_tls else None,
        )
        _, writer = await asyncio.wait_for(coro, timeout=timeout)
        return True, int((time.perf_counter() - started) * 1000)
    except Exception:
        return False, None
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


# ───────────────────── Глубокая проверка ─────────────────────

def xray_path() -> str | None:
    return shutil.which("xray") or shutil.which("xray-core")


def build_outbound(cfg: ProxyConfig) -> dict:
    """Собирает xray-outbound из распарсенной vless-ссылки."""
    p = cfg.params
    stream: dict = {"network": cfg.network, "security": cfg.security}

    if cfg.security == "tls":
        tls: dict = {"serverName": cfg.sni, "allowInsecure": False}
        if p.get("fp"):
            tls["fingerprint"] = p["fp"]
        if p.get("alpn"):
            tls["alpn"] = p["alpn"].split(",")
        stream["tlsSettings"] = tls
    elif cfg.security == "reality":
        stream["realitySettings"] = {
            "serverName": cfg.sni,
            "fingerprint": p.get("fp") or "chrome",
            "publicKey": p.get("pbk", ""),
            "shortId": p.get("sid", ""),
            "spiderX": p.get("spx", "/"),
        }

    net = cfg.network
    if net == "ws":
        ws: dict = {"path": p.get("path", "/")}
        if p.get("host"):
            ws["headers"] = {"Host": p["host"]}
        stream["wsSettings"] = ws
    elif net == "grpc":
        stream["grpcSettings"] = {
            "serviceName": p.get("serviceName", ""),
            "multiMode": p.get("mode") == "multi",
        }
    elif net in ("http", "h2"):
        stream["network"] = "http"
        stream["httpSettings"] = {
            "path": p.get("path", "/"),
            "host": [h for h in p.get("host", "").split(",") if h],
        }
    elif net in ("xhttp", "splithttp"):
        stream["network"] = net
        stream[f"{net}Settings"] = {
            "path": p.get("path", "/"),
            "host": p.get("host", ""),
            "mode": p.get("mode", "auto"),
        }
    elif net == "tcp" and p.get("headerType") == "http":
        stream["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {
                    "path": [p.get("path", "/")],
                    "headers": {"Host": [h for h in p.get("host", "").split(",") if h]},
                },
            }
        }

    user: dict = {"id": cfg.uuid, "encryption": p.get("encryption", "none")}
    if p.get("flow"):
        user["flow"] = p["flow"]

    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": cfg.host, "port": cfg.port, "users": [user]}]},
        "streamSettings": stream,
    }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def deep_check(cfg: ProxyConfig, xray_bin: str, url: str, timeout: float) -> tuple[bool, int | None]:
    """Поднимает xray с этим конфигом и делает запрос через socks5."""
    try:
        import aiohttp
        from aiohttp_socks import ProxyConnector
    except ImportError:
        log.error("нет aiohttp_socks — глубокая проверка недоступна")
        return False, None

    port = _free_port()
    doc = {
        "log": {"loglevel": "none"},
        "inbounds": [{
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "socks",
            "settings": {"udp": False},
        }],
        "outbounds": [build_outbound(cfg)],
    }

    tmp = Path(tempfile.mkstemp(prefix="xraychk_", suffix=".json")[1])
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            xray_bin, "run", "-c", str(tmp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.45)  # даём инбаунду подняться
        if proc.returncode is not None:
            return False, None

        started = time.perf_counter()
        connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{port}")
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:
            async with session.get(url, allow_redirects=False) as resp:
                ok = resp.status in (200, 204, 301, 302)
                await resp.read()
        return ok, int((time.perf_counter() - started) * 1000)
    except Exception:
        return False, None
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            with contextlib.suppress(Exception):
                proc.kill()
        with contextlib.suppress(Exception):
            tmp.unlink()

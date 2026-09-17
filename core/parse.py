"""Парсинг/нормализация прокси-ссылок (основной фокус — vless://)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

LINK_RE = re.compile(
    r"(?:vless|vmess|trojan|ss|hysteria2|hy2)://[^\s\"'<>\\`]+",
    re.IGNORECASE,
)

# Параметры, которые реально влияют на подключение — по ним считаем дубликаты.
KEY_PARAMS = (
    "security", "encryption", "type", "flow", "sni", "host", "path",
    "pbk", "sid", "fp", "serviceName", "mode", "headerType", "alpn",
)


@dataclass
class ProxyConfig:
    protocol: str
    uuid: str
    host: str
    port: int
    params: dict[str, str] = field(default_factory=dict)
    tag: str = ""
    source: str = ""

    # ── производные ──
    @property
    def security(self) -> str:
        return (self.params.get("security") or "none").lower()

    @property
    def network(self) -> str:
        return (self.params.get("type") or "tcp").lower()

    @property
    def sni(self) -> str:
        return self.params.get("sni") or self.params.get("host") or self.host

    @property
    def fingerprint(self) -> str:
        parts = [self.protocol, self.uuid, self.host.lower(), str(self.port)]
        parts += [f"{k}={self.params.get(k, '')}" for k in KEY_PARAMS]
        return hashlib.sha1("|".join(parts).encode()).hexdigest()

    def to_link(self, tag: str | None = None) -> str:
        query = urlencode(self.params, safe="/:+*", quote_via=quote)
        host = f"[{self.host}]" if ":" in self.host else self.host
        link = f"{self.protocol}://{self.uuid}@{host}:{self.port}"
        if query:
            link += f"?{query}"
        label = tag if tag is not None else self.tag
        if label:
            link += f"#{quote(label, safe='')}"
        return link


def try_b64_decode(text: str) -> str | None:
    """Источники часто отдают всю подписку одной base64-строкой."""
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 32 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return None
    compact = compact.replace("-", "+").replace("_", "/")
    compact += "=" * (-len(compact) % 4)
    try:
        decoded = base64.b64decode(compact, validate=False).decode("utf-8", "ignore")
    except (binascii.Error, ValueError):
        return None
    return decoded if "://" in decoded else None


def extract_links(text: str) -> list[str]:
    """Достаёт ссылки из текста; если это base64-подписка — сначала декодирует."""
    if "://" not in text:
        decoded = try_b64_decode(text)
        if decoded:
            text = decoded
    links = LINK_RE.findall(text)
    if not links:
        # Иногда base64 завёрнут построчно
        chunks = [try_b64_decode(line) for line in text.splitlines() if len(line) > 40]
        for chunk in filter(None, chunks):
            links.extend(LINK_RE.findall(chunk))
    return links


def parse_vless(link: str, source: str = "") -> ProxyConfig | None:
    # urlsplit разбирает лениво: .port/.hostname кидают ValueError на мусорных
    # ссылках вроде vless://uuid@host:TurboConfigs — ловим всё разом.
    try:
        parts = urlsplit(link.strip())
        if parts.scheme.lower() != "vless":
            return None
        host, port, user = parts.hostname, parts.port, parts.username
    except ValueError:
        return None
    if not host or not port or not 0 < port < 65536:
        return None

    uuid = unquote(user or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{16,60}", uuid):
        return None

    params = {k: v for k, v in parse_qsl(parts.query, keep_blank_values=False) if v}
    params.setdefault("encryption", "none")
    # Reality без publicKey не подключится — отсеиваем битые
    if params.get("security", "").lower() == "reality" and not params.get("pbk"):
        return None

    return ProxyConfig(
        protocol="vless",
        uuid=uuid,
        host=host,
        port=int(port),
        params=params,
        tag=unquote(parts.fragment or "")[:120],
        source=source,
    )


PARSERS = {"vless": parse_vless}


def parse_link(link: str, source: str = "") -> ProxyConfig | None:
    scheme = link.split("://", 1)[0].lower()
    parser = PARSERS.get(scheme)
    return parser(link, source) if parser else None

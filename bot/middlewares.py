"""Антифлуд: отсекаем лишние апдейты до того, как их начнёт обрабатывать бот.

Кулдаун внутри обработчика от флуда не спасает — бот всё равно успевает
разобрать апдейт, сходить в базу и отправить ответ. Настоящая защита
работает раньше: лишний апдейт отбрасывается молча, ещё до хендлера.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger("bot.throttle")


class Throttle(BaseMiddleware):
    """Ведро с токенами на пользователя плюс временный бан за упорство.

    rate      — сколько действий в секунду разрешено в среднем
    burst     — сколько можно потратить разом (нормальный человек кликает пачкой)
    strikes   — сколько отбитых подряд апдейтов терпим до бана
    ban_for   — на сколько секунд перестаём отвечать нарушителю
    """

    def __init__(
        self,
        rate: float = 1.0,
        burst: int = 4,
        strikes: int = 8,
        ban_for: float = 60.0,
        idle_ttl: float = 600.0,
        exempt: set[int] | None = None,
    ) -> None:
        self.rate, self.burst = rate, burst
        self.strikes, self.ban_for = strikes, ban_for
        self.idle_ttl = idle_ttl
        # Админов не трогаем: в админке и при рассылке они кликают часто
        # и по делу, а замолчавший на минуту бот посреди рассылки —
        # это потеря управления как раз тогда, когда оно нужнее всего.
        self.exempt = exempt or set()
        # user_id -> [токены, когда обновляли, промахов подряд, до какого времени в бане]
        self._buckets: dict[int, list[float]] = {}
        self._last_gc = time.monotonic()

    def _gc(self, now: float) -> None:
        """Чистим тех, кто давно не появлялся — иначе словарь растёт вечно."""
        if now - self._last_gc < 60:
            return
        self._last_gc = now
        stale = [
            uid for uid, b in self._buckets.items()
            if now - b[1] > self.idle_ttl and now > b[3]
        ]
        for uid in stale:
            del self._buckets[uid]
        if stale:
            log.debug("антифлуд: забыли %d неактивных", len(stale))

    def _allow(self, user_id: int) -> bool:
        if user_id in self.exempt:
            return True
        now = time.monotonic()
        self._gc(now)

        bucket = self._buckets.get(user_id)
        if bucket is None:
            self._buckets[user_id] = [self.burst - 1, now, 0, 0.0]
            return True

        tokens, last, misses, banned_until = bucket
        if now < banned_until:
            return False

        tokens = min(self.burst, tokens + (now - last) * self.rate)
        bucket[1] = now

        if tokens >= 1:
            bucket[0] = tokens - 1
            bucket[2] = 0
            return True

        bucket[0] = tokens
        bucket[2] = misses + 1
        if bucket[2] >= self.strikes:
            bucket[3] = now + self.ban_for
            bucket[2] = 0
            log.info("антифлуд: %s замолчали на %.0f с", user_id, self.ban_for)
        return False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or self._allow(user.id):
            return await handler(event, data)

        # Апдейт отбит. Кнопке отвечаем, чтобы у клиента не висели «часики»,
        # но обработчик не запускаем и в базу не лезем.
        if isinstance(event, CallbackQuery):
            try:
                await event.answer("Слишком часто, подожди немного", show_alert=False)
            except Exception:
                pass
        elif isinstance(event, Message):
            pass  # на поток сообщений просто молчим — это дешевле всего
        return None

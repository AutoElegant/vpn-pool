"""Точка входа бота."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat

from core.db import init_db
from core.settings import ADMIN_IDS, BOT_TOKEN

from . import admin, handlers, storage, texts

log = logging.getLogger("bot")


async def set_profile(bot: Bot) -> None:
    """Описание в профиле бота и текст на экране до нажатия Start."""
    try:
        await bot.set_my_description(description=texts.BOT_DESCRIPTION)
        await bot.set_my_short_description(short_description=texts.BOT_SHORT_DESCRIPTION)
    except Exception as exc:
        log.warning("не смог обновить описание бота: %s", exc)


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Главное меню"),
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="🚀 Главное меню"),
                    BotCommand(command="admin", description="🛠 Админ-панель"),
                    BotCommand(command="stats", description="📊 Статистика"),
                    BotCommand(command="broadcast", description="📣 Рассылка"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as exc:
            log.warning("не смог поставить команды для %s: %s", admin_id, exc)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not BOT_TOKEN:
        sys.exit("BOT_TOKEN не задан — скопируй .env.example в .env и впиши токен")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — админка будет недоступна")

    init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin.router)     # админ-роутер первым: перехватывает FSM
    dp.include_router(handlers.router)

    await set_commands(bot)
    await set_profile(bot)
    me = await bot.get_me()
    log.info("Бот @%s запущен", me.username)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await storage.close_db()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

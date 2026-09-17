"""Пользовательские хендлеры."""
from __future__ import annotations

import io
import logging
import time

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from core.geo import flag
from core.settings import ADMIN_IDS, BOT, REQUIRED_CHANNEL

from . import keyboards as kb
from . import storage, texts

log = logging.getLogger("bot.user")
router = Router(name="user")

_last_issue: dict[int, float] = {}


# ───────────────────────── помощники ─────────────────────────

async def check_subscribed(bot, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception:
        return True  # канал недоступен — не блокируем людей


def make_qr(link: str) -> bytes | None:
    try:
        import qrcode
    except ImportError:
        return None
    try:
        img = qrcode.make(link)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


async def send_config(target: Message, user_id: int) -> None:
    cooldown = BOT.get("cooldown_seconds", 15)
    left = cooldown - (time.monotonic() - _last_issue.get(user_id, 0))
    if left > 0 and user_id not in ADMIN_IDS:
        await target.answer(texts.COOLDOWN.format(sec=int(left) + 1))
        return

    limit = BOT.get("daily_limit", 0)
    if limit and user_id not in ADMIN_IDS and await storage.issued_today(user_id) >= limit:
        await target.answer(texts.LIMIT_REACHED.format(limit=limit))
        return

    row = await storage.pick_config(user_id)
    if row is None:
        await target.answer(texts.NO_CONFIGS, reply_markup=kb.main_menu(user_id in ADMIN_IDS))
        return

    _last_issue[user_id] = time.monotonic()
    await storage.log_issue(user_id, row["id"], row["country"])

    caption = texts.CONFIG_CAPTION.format(
        flag=flag(row["country"]),
        country=row["country_name"] or row["country"] or "Неизвестно",
        city=f" · {row['city']}" if row["city"] else "",
        link=row["link"],
    )
    markup = kb.after_config()

    if BOT.get("send_qr", True):
        png = make_qr(row["link"])
        if png and len(png) < 9_000_000:
            await target.answer_photo(
                BufferedInputFile(png, filename="happ-config.png"),
                caption=caption,
                reply_markup=markup,
            )
            return
    await target.answer(caption, reply_markup=markup, disable_web_page_preview=True)


# ───────────────────────── хендлеры ─────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    u = message.from_user
    payload = (message.text or "").partition(" ")[2].strip() or None
    is_new = await storage.upsert_user(u.id, u.username, u.first_name, u.language_code, payload)
    if is_new:
        log.info("новый юзер: %s (%s)", u.id, u.username)

    if not await check_subscribed(message.bot, u.id):
        await message.answer(texts.SUBSCRIBE_REQUIRED, reply_markup=kb.subscribe(REQUIRED_CHANNEL))
        return

    await message.answer(texts.START, reply_markup=kb.main_menu(u.id in ADMIN_IDS))


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery) -> None:
    if await check_subscribed(call.bot, call.from_user.id):
        await call.message.edit_text(
            texts.MENU, reply_markup=kb.main_menu(call.from_user.id in ADMIN_IDS)
        )
        await call.answer("Спасибо! 🎉")
    else:
        await call.answer("Подписка не найдена 🤔", show_alert=True)


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery) -> None:
    markup = kb.main_menu(call.from_user.id in ADMIN_IDS)
    text = texts.MENU
    try:
        if call.message.photo:
            await call.message.delete()
            await call.message.answer(text, reply_markup=markup)
        else:
            await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "howto")
async def cb_howto(call: CallbackQuery) -> None:
    try:
        if call.message.photo:
            await call.message.delete()
            await call.message.answer(texts.HOWTO, reply_markup=kb.howto(),
                                      disable_web_page_preview=True)
        else:
            await call.message.edit_text(texts.HOWTO, reply_markup=kb.howto(),
                                         disable_web_page_preview=True)
    except Exception:
        await call.message.answer(texts.HOWTO, reply_markup=kb.howto(),
                                  disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data.startswith("get:"))
async def cb_get(call: CallbackQuery) -> None:
    await storage.upsert_user(
        call.from_user.id, call.from_user.username,
        call.from_user.first_name, call.from_user.language_code,
    )
    if not await check_subscribed(call.bot, call.from_user.id):
        await call.answer("Сначала подпишись на канал 🙂", show_alert=True)
        return

    await call.answer("Ищу рабочий сервер… 🔎")
    await send_config(call.message, call.from_user.id)


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.message(F.text)
async def fallback(message: Message) -> None:
    u = message.from_user
    await storage.upsert_user(u.id, u.username, u.first_name, u.language_code)
    await message.answer(texts.MENU, reply_markup=kb.main_menu(u.id in ADMIN_IDS))

"""Пользовательские хендлеры."""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from core.geo import flag
from core.parse import parse_link
from core.settings import ADMIN_IDS, BOT, BOT_USERNAME, REQUIRED_CHANNEL

from . import keyboards as kb
from . import storage, texts

log = logging.getLogger("bot.user")
router = Router(name="user")

_last_issue: dict[int, float] = {}
_last_gc = 0.0

# Telegram отдаёт file_id на каждое загруженное фото и умеет пересылать его
# повторно без загрузки. Один и тот же конфиг уходит многим, поэтому QR
# рисуется один раз, а дальше отправляется идентификатором — это экономит
# и 75 мс процессорного времени, и трафик.
_qr_cache: dict[str, str] = {}
_QR_CACHE_MAX = 2000


# ───────────────────────── помощники ─────────────────────────

async def check_subscribed(bot, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception:
        return True  # канал недоступен — не блокируем людей


def brand(link: str) -> str:
    """Дописывает имя бота в название конфига — так он подписан в списке
    серверов внутри Happ.

    Делается здесь, а не в коллекторе: пул лежит в публичном репозитории,
    и светить там бота незачем. Заодно имя можно поменять в .env и
    перезапустить бота, не дожидаясь следующего прохода сбора.
    """
    if not BOT_USERNAME:
        return link
    cfg = parse_link(link)
    if cfg is None:
        return link
    # Отрезаем прежнюю подпись, чтобы она не накапливалась при повторной выдаче.
    base = cfg.tag.split(" | @")[0].strip()
    return cfg.to_link(f"{base} | @{BOT_USERNAME}" if base else f"@{BOT_USERNAME}")


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


def qr_key(link: str) -> str:
    return hashlib.sha1(link.encode()).hexdigest()


async def send_qr(target: Message, link: str, caption: str, markup) -> bool:
    """Шлёт QR: готовым file_id, если этот конфиг уже отправляли."""
    key = qr_key(link)
    cached = _qr_cache.get(key)
    if cached:
        try:
            await target.answer_photo(cached, caption=caption, reply_markup=markup)
            return True
        except Exception:
            _qr_cache.pop(key, None)   # file_id протух — перерисуем

    # Рисование блокирующее (~75 мс), поэтому в отдельном потоке:
    # иначе на одном ядре бот замирает и не отвечает остальным.
    png = await asyncio.to_thread(make_qr, link)
    if not png or len(png) > 9_000_000:
        return False
    try:
        msg = await target.answer_photo(
            BufferedInputFile(png, filename="happ-config.png"),
            caption=caption,
            reply_markup=markup,
        )
    except Exception:
        return False

    if msg.photo and len(_qr_cache) < _QR_CACHE_MAX:
        _qr_cache[key] = msg.photo[-1].file_id
    return True


def cooldown_left(user_id: int) -> int:
    """Сколько секунд ещё нельзя брать новый конфиг. 0 — можно."""
    if user_id in ADMIN_IDS:
        return 0
    passed = time.monotonic() - _last_issue.get(user_id, 0.0)
    left = BOT.get("cooldown_seconds", 10) - passed
    return int(left) + 1 if left > 0 else 0


def forget_old_issues(now: float) -> None:
    """Чистим отметки о выдачах, иначе словарь растёт с каждым пользователем."""
    global _last_gc
    if now - _last_gc < 300:
        return
    _last_gc = now
    horizon = BOT.get("cooldown_seconds", 10) * 4
    for uid in [u for u, ts in _last_issue.items() if now - ts > horizon]:
        _last_issue.pop(uid, None)


async def send_config(target: Message, user_id: int) -> None:
    limit = BOT.get("daily_limit", 0)
    if limit and user_id not in ADMIN_IDS and await storage.issued_today(user_id) >= limit:
        await target.answer(texts.LIMIT_REACHED.format(limit=limit))
        return

    row = await storage.pick_config(user_id)
    if row is None:
        await target.answer(texts.NO_CONFIGS, reply_markup=kb.main_menu(user_id in ADMIN_IDS))
        return

    now = time.monotonic()
    _last_issue[user_id] = now
    forget_old_issues(now)
    await storage.log_issue(user_id, row["id"], row["country"])

    link = brand(row["link"])
    caption = texts.CONFIG_CAPTION.format(
        flag=flag(row["country"]),
        country=row["country_name"] or row["country"] or "Неизвестно",
        city=f" · {row['city']}" if row["city"] else "",
        link=link,
    )
    markup = kb.after_config()

    if BOT.get("send_qr", True) and await send_qr(target, link, caption, markup):
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
    # Кулдаун — первым делом, до любых запросов к базе.
    left = cooldown_left(call.from_user.id)
    if left:
        await call.answer(f"Подожди ещё {left} сек ⏳", show_alert=False)
        return

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

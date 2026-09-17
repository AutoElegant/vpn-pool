"""Админ-панель: статистика, рассылка, выгрузка юзеров."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from core.geo import flag
from core.settings import ADMIN_IDS, BOT

from . import keyboards as kb
from . import storage, texts

log = logging.getLogger("bot.admin")
router = Router(name="admin")


class Broadcast(StatesGroup):
    waiting_message = State()
    waiting_confirm = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ───────────────────────── меню ─────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer(texts.NOT_ADMIN)
        return
    await message.answer(texts.ADMIN_MENU, reply_markup=kb.admin_menu())


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer(texts.NOT_ADMIN, show_alert=True)
        return
    await state.clear()
    try:
        await call.message.edit_text(texts.ADMIN_MENU, reply_markup=kb.admin_menu())
    except TelegramBadRequest:
        await call.message.answer(texts.ADMIN_MENU, reply_markup=kb.admin_menu())
    await call.answer()


# ───────────────────────── статистика ─────────────────────────

def render_stats(s: dict) -> str:
    top = "\n".join(
        f"{i}. {flag(code)} {name} — <b>{n}</b>"
        for i, (code, name, n) in enumerate(s["top"], 1)
    ) or "—"

    run = s["run"]
    if run:
        collected = (run["started_at"] or "")[:16].replace("T", " ")
        synced = (run["finished_at"] or "")[:16].replace("T", " ")
        last_run = (
            f"├ Собран на GitHub: {collected} UTC\n"
            f"├ Получен сервером: {synced} UTC\n"
            f"├ Конфигов: <b>{run['pool']}</b>, из них xray-ok <b>{run['verified']}</b>\n"
            f"└ Из РФ: доступно <b>{run['ru_ok']}</b>, заблокировано <b>{run['dropped']}</b>"
        )
    else:
        last_run = "└ пул ещё не приезжал"

    return texts.ADMIN_STATS.format(top_countries=top, last_run=last_run, **{
        k: v for k, v in s.items() if k not in ("top", "run")
    })


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer(texts.NOT_ADMIN)
        return
    await message.answer(render_stats(await storage.stats()), reply_markup=kb.back_to_admin())


@router.callback_query(F.data == "admin:stats")
async def cb_stats(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer(texts.NOT_ADMIN, show_alert=True)
        return
    text = render_stats(await storage.stats())
    try:
        await call.message.edit_text(text, reply_markup=kb.back_to_admin())
        await call.answer("Обновлено")
    except TelegramBadRequest:
        await call.answer("Данные не изменились")


# ───────────────────────── выгрузка ─────────────────────────

@router.callback_query(F.data == "admin:export")
async def cb_export(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer(texts.NOT_ADMIN, show_alert=True)
        return
    await call.answer("Готовлю файл…")

    db = await storage.get_db()
    cur = await db.execute(
        "SELECT user_id, username, first_name, lang, joined_at, last_seen, "
        "is_blocked, configs_taken, source FROM users ORDER BY joined_at"
    )
    rows = await cur.fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "first_name", "lang", "joined_at",
                     "last_seen", "is_blocked", "configs_taken", "source"])
    writer.writerows([tuple(r) for r in rows])

    await call.message.answer_document(
        BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="users.csv"),
        caption=f"👥 Пользователей: <b>{len(rows)}</b>",
    )


# ───────────────────────── рассылка ─────────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast_start(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer(texts.NOT_ADMIN, show_alert=True)
        return
    await state.set_state(Broadcast.waiting_message)
    await call.message.answer(texts.BROADCAST_ASK)
    await call.answer()


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer(texts.NOT_ADMIN)
        return
    await state.set_state(Broadcast.waiting_message)
    await message.answer(texts.BROADCAST_ASK)


@router.message(Command("cancel"), StateFilter(Broadcast))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.BROADCAST_CANCELLED, reply_markup=kb.admin_menu())


@router.message(Broadcast.waiting_message)
async def got_broadcast_message(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    recipients = await storage.all_user_ids(only_active=True)
    await state.update_data(
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        preview=(message.text or message.caption or "[медиа]")[:300],
        total=len(recipients),
    )
    await state.set_state(Broadcast.waiting_confirm)
    await message.answer(
        texts.BROADCAST_CONFIRM.format(total=len(recipients)),
        reply_markup=kb.broadcast_confirm(),
    )


@router.callback_query(F.data == "admin:bc_cancel", StateFilter(Broadcast))
async def cb_broadcast_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(texts.BROADCAST_CANCELLED)
    await call.answer()


@router.callback_query(F.data == "admin:bc_go", StateFilter(Broadcast.waiting_confirm))
async def cb_broadcast_go(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer(texts.NOT_ADMIN, show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    await call.message.edit_text(texts.BROADCAST_STARTED)
    await call.answer()

    asyncio.create_task(
        do_broadcast(
            call.bot,
            admin_id=call.from_user.id,
            status_chat=call.message.chat.id,
            from_chat_id=data["from_chat_id"],
            message_id=data["message_id"],
            preview=data["preview"],
        )
    )


async def do_broadcast(bot: Bot, admin_id: int, status_chat: int,
                       from_chat_id: int, message_id: int, preview: str) -> None:
    user_ids = await storage.all_user_ids(only_active=True)
    bid = await storage.start_broadcast(admin_id, preview, len(user_ids))

    status = await bot.send_message(
        status_chat, texts.BROADCAST_PROGRESS.format(sent=0, total=len(user_ids), failed=0)
    )

    rate = max(1, BOT.get("broadcast_rate", 20))
    delay = 1 / rate
    sent = failed = 0
    started = time.monotonic()

    for i, uid in enumerate(user_ids, 1):
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            await storage.mark_blocked(uid)
            failed += 1
        except Exception as exc:
            log.warning("рассылка %s: %s", uid, exc)
            failed += 1

        if i % 50 == 0:
            try:
                await status.edit_text(
                    texts.BROADCAST_PROGRESS.format(sent=sent, total=len(user_ids), failed=failed)
                )
            except Exception:
                pass
        await asyncio.sleep(delay)

    await storage.finish_broadcast(bid, sent, failed)
    elapsed = int(time.monotonic() - started)
    summary = texts.BROADCAST_DONE.format(
        sent=sent, failed=failed, elapsed=f"{elapsed // 60} мин {elapsed % 60} сек"
    )
    try:
        await status.edit_text(summary, reply_markup=kb.admin_menu())
    except Exception:
        await bot.send_message(status_chat, summary, reply_markup=kb.admin_menu())

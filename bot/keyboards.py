"""Клавиатуры."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.geo import flag

from . import texts


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Получить VPN", callback_data="get:any")
    kb.button(text="📖 Как подключить", callback_data="howto")
    if is_admin:
        kb.button(text="🛠 Админка", callback_data="admin:menu")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def after_config() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎲 Ещё конфиг", callback_data="get:any")
    kb.button(text="📖 Как подключить", callback_data="howto")
    kb.button(text="🏠 В меню", callback_data="menu")
    kb.adjust(1, 2)
    return kb.as_markup()


def howto() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key in ("ios", "android", "apk", "windows", "macos", "linux_deb"):
        title, url = texts.HAPP[key]
        kb.button(text=title, url=url)
    kb.button(text="🚀 Получить VPN", callback_data="get:any")
    kb.button(text="🏠 В меню", callback_data="menu")
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="📣 Рассылка", callback_data="admin:broadcast")
    kb.button(text="📥 Выгрузить юзеров (CSV)", callback_data="admin:export")
    kb.button(text="🏠 В меню", callback_data="menu")
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def back_to_admin() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="admin:stats")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    kb.adjust(2)
    return kb.as_markup()


def broadcast_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data="admin:bc_go")
    kb.button(text="❌ Отмена", callback_data="admin:bc_cancel")
    kb.adjust(2)
    return kb.as_markup()


def subscribe(channel: str) -> InlineKeyboardMarkup:
    url = f"https://t.me/{channel.lstrip('@')}" if not channel.startswith("-") else ""
    kb = InlineKeyboardBuilder()
    if url:
        kb.button(text="📢 Подписаться", url=url)
    kb.button(text="✅ Я подписался", callback_data="check_sub")
    kb.adjust(1)
    return kb.as_markup()

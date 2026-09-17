"""Тексты и ссылки. Меняй под себя — логика бота их не трогает."""
from __future__ import annotations

# Официальные ссылки на Happ (happ.info)
HAPP = {
    "ios":        ("🍎 iPhone / iPad",   "https://apps.apple.com/ru/app/happ-lite/id6799917773"),
    "android":    ("🤖 Android (Google Play)", "https://play.google.com/store/apps/details?id=com.happproxy"),
    "apk":        ("📦 Android (APK)",   "https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk"),
    "windows":    ("🪟 Windows",         "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"),
    "macos":      ("💻 macOS",           "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg"),
    "linux_deb":  ("🐧 Linux (.deb)",    "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.linux.x64.deb"),
    "linux_rpm":  ("🐧 Linux (.rpm)",    "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.linux.x64.rpm"),
}

START = "Это бесплатный VPN-бот.\nВсегда рабочие конфиги!"

MENU = START

# Описание бота (видно до нажатия Start) и короткое описание в профиле
BOT_DESCRIPTION = "Это бесплатный VPN-бот.\nВсегда рабочие конфиги!"
BOT_SHORT_DESCRIPTION = "Это бесплатный VPN-бот. Всегда рабочие конфиги!"

HOWTO = (
    "📖 <b>Как подключиться за 3 шага</b>\n\n"
    "<b>1.</b> Установи <b>Happ</b> — кнопки ниже, выбери своё устройство.\n\n"
    "<b>2.</b> Вернись сюда и нажми «🚀 Получить VPN».\n"
    "Бот пришлёт ссылку вида <code>vless://…</code> и QR-код.\n\n"
    "<b>3.</b> Добавь конфиг в Happ любым способом:\n"
    "• <b>Из буфера:</b> тапни по ссылке в сообщении (она скопируется) → "
    "в Happ нажми <b>«+»</b> в правом верхнем углу → <b>«Из буфера обмена»</b>.\n"
    "• <b>По QR:</b> в Happ <b>«+»</b> → <b>«Сканировать QR»</b> → наведи на картинку "
    "(на телефоне открой QR на другом экране или попроси прислать на второе устройство).\n\n"
    "Дальше жми большую кнопку подключения — готово.\n\n"
    "⚠️ <b>Важно про бесплатные конфиги</b>\n"
    "Это публичные чужие сервера. Они быстро умирают — если перестал работать, "
    "просто возьми новый. И не заходи через них в банк и почту: "
    "владелец сервера технически видит твой трафик."
)

CONFIG_CAPTION = (
    "✅ <b>Рабочий конфиг</b>\n"
    "{flag} {country}{city}\n\n"
    "👇 <b>Тапни по ссылке — она скопируется</b>\n"
    "<code>{link}</code>\n\n"
    "Дальше: Happ → «+» → «Из буфера обмена»"
)

NO_CONFIGS = (
    "😔 Сейчас свободных конфигов нет — идёт обновление.\n"
    "Загляни через пару минут."
)

COOLDOWN = "⏳ Не так быстро. Следующий конфиг можно взять через <b>{sec} сек</b>."

LIMIT_REACHED = (
    "🚧 На сегодня лимит — <b>{limit}</b> конфигов.\n"
    "Приходи завтра, лимит обнулится."
)

SUBSCRIBE_REQUIRED = (
    "🔒 Чтобы пользоваться ботом, подпишись на канал — там новости и запасные конфиги."
)

# ── Админка ──
ADMIN_MENU = "🛠 <b>Админ-панель</b>"

ADMIN_STATS = (
    "📊 <b>Статистика</b>\n\n"
    "<b>Пользователи</b>\n"
    "├ Всего: <b>{users_total}</b>\n"
    "├ Активных за 24ч: <b>{users_24h}</b>\n"
    "├ Активных за 7д: <b>{users_7d}</b>\n"
    "├ Новых сегодня: <b>{users_today}</b>\n"
    "├ Новых за 7д: <b>{users_new_7d}</b>\n"
    "└ Заблокировали бота: <b>{users_blocked}</b>\n\n"
    "<b>Конфиги</b>\n"
    "├ Всего в базе: <b>{cfg_total}</b>\n"
    "├ Живых: <b>{cfg_alive}</b>\n"
    "├ Подтверждено xray: <b>{cfg_verified}</b>\n"
    "├ Доступно из РФ: <b>{cfg_ru_ok}</b>\n"
    "├ Из РФ заблокировано: <b>{cfg_ru_bad}</b>\n"
    "├ Ещё не проверено: <b>{cfg_ru_todo}</b>\n"
    "├ Стран: <b>{cfg_countries}</b>\n"
    "└ Медианный пинг: <b>{cfg_latency} мс</b>\n\n"
    "<b>Выдачи</b>\n"
    "├ Всего: <b>{issued_total}</b>\n"
    "└ За 24ч: <b>{issued_24h}</b>\n\n"
    "<b>Последний сбор</b>\n"
    "{last_run}\n\n"
    "🏆 <b>Топ стран</b>\n{top_countries}"
)

BROADCAST_ASK = (
    "📣 <b>Рассылка</b>\n\n"
    "Пришли сообщение, которое разослать всем. Можно текст, фото, видео — "
    "любое сообщение, оно уйдёт как есть (с форматированием и кнопками, если переслать).\n\n"
    "Для отмены — /cancel"
)

BROADCAST_CONFIRM = "👆 Так будет выглядеть рассылка.\n\nПолучателей: <b>{total}</b>. Отправляем?"
BROADCAST_STARTED = "🚀 Рассылка запущена…"
BROADCAST_PROGRESS = "📣 Рассылка: <b>{sent}</b>/{total} ✅  ошибок: <b>{failed}</b>"
BROADCAST_DONE = (
    "✅ <b>Рассылка завершена</b>\n\n"
    "Доставлено: <b>{sent}</b>\n"
    "Не доставлено: <b>{failed}</b>\n"
    "Заняло: <b>{elapsed}</b>"
)
BROADCAST_CANCELLED = "❌ Рассылка отменена."
NOT_ADMIN = "⛔️ Эта команда только для админов."

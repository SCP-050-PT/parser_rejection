# bot/handlers.py
"""
Обработчики кнопок меню VK-бота.
Парсер отказов zakupki.gov.ru.
Версия: Автоматический режим (упрощённое меню)
"""

from datetime import datetime, timedelta
from loguru import logger

from config import settings, get_user_settings
from bot.keyboards import get_main_menu, Buttons

AUTO_DAYS_BACK = 30


def _is_admin(user_id: int) -> bool:
    """Проверка является ли пользователь админом"""
    return str(user_id) == str(settings.admin_vk_id).strip()


async def handle_text(user_id: int, text: str, send_message):
    """Обработка кнопок меню"""
    from config import user_db

    if not user_id:
        return

    user = user_db.get_user(user_id)
    if not user or user.get("status") != "active":
        await send_message(user_id, "❌ У вас нет доступа. Отправьте /start")
        return

    is_admin = _is_admin(user_id)

    if text == Buttons.SETTINGS:
        await _show_settings(user_id, send_message, is_admin)
    elif text == Buttons.FILE:
        await _show_file_link(user_id, send_message, is_admin)
    elif text == Buttons.NOTIFICATIONS:
        await _toggle_notifications(user_id, send_message, is_admin)
    elif text == Buttons.ADMIN_PANEL and is_admin:
        from bot.admin_handlers import admin_command

        await admin_command(user_id, send_message)
    elif text == Buttons.BACK:
        await send_message(user_id, "Главное меню:", get_main_menu(is_admin=is_admin))
    else:
        await send_message(
            user_id,
            "❓ Неизвестная команда. Используйте меню ниже.",
            get_main_menu(is_admin=is_admin),
        )


async def _show_settings(user_id: int, send_message, is_admin: bool):
    """Показывает автоматически рассчитанные настройки"""
    user_settings = get_user_settings(user_id)
    data = user_settings.get()

    today = datetime.now()
    date_from = (today - timedelta(days=AUTO_DAYS_BACK)).strftime("%d.%m.%Y")
    date_to = today.strftime("%d.%m.%Y")

    notif_status = (
        "🟢 Включены" if data.get("notifications_enabled", True) else "🔴 Выключены"
    )

    text = (
        f"⚙️ Текущие настройки\n\n"
        f"📅 Период поиска:\n"
        f"{date_from} — {date_to} (автоматически)\n\n"
        f"💰 Суммы:\n"
        f"0 — 1 000 000 000 ₽\n\n"
        f"🔔 Уведомления: {notif_status}\n\n"
        f"<i>Парсинг работает автоматически каждые 5 минут.</i>"
    )

    await send_message(user_id, text, get_main_menu(is_admin=is_admin))


async def _toggle_notifications(user_id: int, send_message, is_admin: bool):
    """Переключение уведомлений"""
    user_settings = get_user_settings(user_id)
    data = user_settings.get()

    new_state = not data.get("notifications_enabled", True)
    user_settings.set(notifications_enabled=new_state)

    status_text = "🟢 Включены" if new_state else "🔴 Выключены"
    await send_message(
        user_id,
        f"🔔 Уведомления: {status_text}",
        get_main_menu(is_admin=is_admin),
    )


async def _show_file_link(user_id: int, send_message, is_admin: bool):
    """Показывает ссылку на Яндекс.Диск"""
    admin_id = int(settings.admin_vk_id)
    current_date = datetime.now().strftime("%d.%m.%Y")
    filename = f"reports_{current_date}.xlsx"

    # Прямая ссылка на папку (fallback, всегда работает)
    yandex_url = (
        f"https://disk.yandex.ru/client/disk/parser_users/{admin_id}/{filename}"
    )

    text = (
        f"📁 Отчёт на Яндекс.Диске:\n\n"
        f"🔗 {yandex_url}\n\n"
        f"<i>Файл обновляется автоматически после каждого запуска парсинга.</i>"
    )

    await send_message(user_id, text, get_main_menu(is_admin=is_admin))

# bot/admin_handlers.py
"""
Админ-панель для управления пользователями (VK версия).
Парсер отказов zakupki.gov.ru.
"""

from loguru import logger
from vkbottle import Keyboard, KeyboardButtonColor, Text
from config import settings, user_db

# ============================================================================
# КЛАВИАТУРЫ
# ============================================================================


def get_admin_main_keyboard():
    """Главное меню админ-панели"""
    return (
        Keyboard(inline=True)
        .add(
            Text("⏳ Заявки", payload={"cmd": "admin_pending"}),
            color=KeyboardButtonColor.PRIMARY,
        )
        .row()
        .add(
            Text("✅ Активные", payload={"cmd": "admin_active"}),
            color=KeyboardButtonColor.POSITIVE,
        )
        .row()
        .add(
            Text("👥 Все пользователи", payload={"cmd": "admin_all_users"}),
            color=KeyboardButtonColor.SECONDARY,
        )
        .row()
        .add(
            Text("🔙 Назад", payload={"cmd": "admin_back"}),
            color=KeyboardButtonColor.NEGATIVE,
        )
        .get_json()
    )


def get_back_keyboard(cmd: str = "admin_back"):
    """Клавиатура с кнопкой назад"""
    return (
        Keyboard(inline=True)
        .add(
            Text("🔙 Назад", payload={"cmd": cmd}),
            color=KeyboardButtonColor.SECONDARY,
        )
        .get_json()
    )


def get_delete_confirm_keyboard(user_id: int):
    """Клавиатура подтверждения удаления"""
    return (
        Keyboard(inline=True)
        .add(
            Text("🗑 Да, удалить", payload={"cmd": "delete_execute", "id": user_id}),
            color=KeyboardButtonColor.NEGATIVE,
        )
        .add(
            Text("❌ Отмена", payload={"cmd": "admin_all_users"}),
            color=KeyboardButtonColor.SECONDARY,
        )
        .get_json()
    )


def get_pagination_keyboard(page: int, total_pages: int):
    """Клавиатура навигации по страницам"""
    kb = Keyboard(inline=True)

    if page > 1:
        kb.add(
            Text("⬅️ Назад", payload={"cmd": "admin_users_page", "page": page - 1}),
            color=KeyboardButtonColor.PRIMARY,
        )
    if page < total_pages:
        kb.add(
            Text("Вперёд ➡️", payload={"cmd": "admin_users_page", "page": page + 1}),
            color=KeyboardButtonColor.PRIMARY,
        )

    kb.row()
    kb.add(
        Text("🔙 В админку", payload={"cmd": "admin_back"}),
        color=KeyboardButtonColor.SECONDARY,
    )

    return kb.get_json()


# ============================================================================
# КОМАНДЫ АДМИН-ПАНЕЛИ
# ============================================================================


async def admin_command(user_id: int, send_message):
    """Главная команда /admin"""
    try:
        all_users = user_db.get_all_users() or []
        pending = [u for u in all_users if u.get("status") == "pending"]
        active = [u for u in all_users if u.get("status") == "active"]
        blocked = [u for u in all_users if u.get("status") == "blocked"]

        text = (
            f"🔐 Админ-панель\n\n"
            f"📊 Статистика:\n"
            f"⏳ Ожидают: {len(pending)}\n"
            f"✅ Активны: {len(active)}\n"
            f"❌ Заблокированы: {len(blocked)}\n"
            f"📊 Всего: {len(all_users)}\n\n"
            f"Выберите действие:"
        )

        await send_message(user_id, text, get_admin_main_keyboard())

    except Exception as e:
        logger.error(f"💥 Ошибка в /admin: {e}", exc_info=True)
        await send_message(user_id, f"❌ Ошибка: {str(e)[:100]}")


async def admin_pending_callback(user_id: int, send_message):
    """Показывает заявки на рассмотрении"""
    try:
        pending = user_db.get_pending_users() or []

        if not pending:
            await send_message(
                user_id,
                "⏳ Нет заявок на рассмотрении.\n\n/admin — вернуться",
                get_back_keyboard("admin_back"),
            )
            return

        text = f"📋 Заявки на рассмотрении ({len(pending)}):\n\n"

        for user in pending[:20]:
            uid = user.get("chat_id", "N/A")
            name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            username = user.get("username", "")
            username_str = f" (@{username})" if username else ""

            text += (
                f"👤 {name}{username_str}\n"
                f"   ID: {uid}\n"
                f"   ✅ /approve_{uid}\n"
                f"   ❌ /reject_{uid}\n\n"
            )

        if len(pending) > 20:
            text += f"...и ещё {len(pending) - 20} заявок\n"

        text += "\n/admin — вернуться"
        await send_message(user_id, text, get_back_keyboard("admin_back"))

    except Exception as e:
        logger.error(f"💥 Ошибка в admin_pending: {e}", exc_info=True)
        await send_message(user_id, "❌ Ошибка")


async def admin_active_callback(user_id: int, send_message):
    """Показывает активных пользователей"""
    try:
        active = user_db.get_active_users() or []

        if not active:
            await send_message(
                user_id,
                "✅ Нет активных пользователей.\n\n/admin — вернуться",
                get_back_keyboard("admin_back"),
            )
            return

        text = f"✅ Активные пользователи ({len(active)}):\n\n"

        for user in active[:30]:
            uid = user.get("chat_id", "N/A")
            name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            username = user.get("username", "")
            username_str = f" (@{username})" if username else ""

            text += f"• {name}{username_str} — ID: {uid}\n"

        if len(active) > 30:
            text += f"\n...и ещё {len(active) - 30}\n"

        text += "\n/admin — вернуться"
        await send_message(user_id, text, get_back_keyboard("admin_back"))

    except Exception as e:
        logger.error(f"💥 Ошибка в admin_active: {e}", exc_info=True)
        await send_message(user_id, "❌ Ошибка")


async def admin_all_users_callback(user_id: int, send_message, page: int = 1):
    """Показывает ВСЕХ пользователей с возможностью удаления"""
    try:
        all_users = user_db.get_all_users() or []

        if not all_users:
            await send_message(
                user_id,
                "👥 Нет пользователей в базе.\n\n/admin — вернуться",
                get_back_keyboard("admin_back"),
            )
            return

        users_per_page = 10
        total_pages = max(1, (len(all_users) + users_per_page - 1) // users_per_page)
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * users_per_page
        end_idx = min(start_idx + users_per_page, len(all_users))
        page_users = all_users[start_idx:end_idx]

        text = f"👥 Все пользователи (стр. {page}/{total_pages})\n\n"

        for user in page_users:
            status_icon = {"active": "✅", "pending": "⏳", "blocked": "❌"}.get(
                user.get("status", "unknown"), "❓"
            )

            uid = user.get("chat_id", "N/A")
            name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            username = user.get("username", "")
            username_str = f" (@{username})" if username else ""

            text += (
                f"{status_icon} {name}{username_str}\n"
                f"   ID: {uid} | Статус: {user.get('status', 'unknown')}\n"
                f"   🗑 /delete_{uid}\n\n"
            )

        await send_message(user_id, text, get_pagination_keyboard(page, total_pages))

    except Exception as e:
        logger.error(f"💥 Ошибка в admin_all_users: {e}", exc_info=True)
        await send_message(user_id, "❌ Ошибка")


async def admin_delete_confirm_callback(user_id: int, send_message, target_id: int):
    """Подтверждение удаления пользователя"""
    try:
        admin_id = int(settings.admin_vk_id)

        if target_id == admin_id:
            await send_message(
                user_id,
                "❌ Нельзя удалить самого себя!",
                get_back_keyboard("admin_all_users"),
            )
            return

        user = user_db.get_user(target_id)

        if not user:
            await send_message(user_id, "❌ Пользователь не найден!")
            return

        name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()

        text = (
            f"⚠️ Подтвердите удаление\n\n"
            f"👤 {name}\n"
            f"🆔 ID: {target_id}\n"
            f"Статус: {user.get('status', 'unknown')}\n\n"
            f"❗️ Это действие нельзя отменить!\n"
            f"Будут удалены данные из базы и локальные файлы."
        )

        await send_message(user_id, text, get_delete_confirm_keyboard(target_id))

    except Exception as e:
        logger.error(f"💥 Ошибка подтверждения удаления: {e}", exc_info=True)
        await send_message(user_id, "❌ Ошибка")


async def admin_delete_execute_callback(user_id: int, send_message, target_id: int):
    """Выполнение удаления пользователя"""
    try:
        admin_id = int(settings.admin_vk_id)

        if target_id == admin_id:
            await send_message(user_id, "❌ Нельзя удалить самого себя!")
            return

        success = user_db.delete_user(target_id)

        if success:
            await send_message(
                user_id,
                f"🗑 Пользователь {target_id} удалён!",
                get_back_keyboard("admin_all_users"),
            )
            logger.info(f"✅ Админ удалил пользователя {target_id}")
        else:
            await send_message(
                user_id,
                f"❌ Ошибка при удалении пользователя {target_id}",
                get_back_keyboard("admin_all_users"),
            )

    except Exception as e:
        logger.error(f"💥 Ошибка удаления: {e}", exc_info=True)
        await send_message(user_id, f"❌ Ошибка: {str(e)[:200]}")

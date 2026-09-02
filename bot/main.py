# bot/main.py
"""
Точка входа для VK бота парсера отказов.
Версия: Гибкий автопарсинг — 4 минуты пауза после завершения парсинга.
"""

import asyncio
import signal
import sys
import random
import time
from pathlib import Path
from datetime import datetime, timedelta
from loguru import logger
from typing import Optional, Any

from vkbottle import Bot, Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Message
from vkbottle.tools import Keyboard as VKKeyboard

# Добавляем корень проекта в путь
current_file = Path(__file__).resolve()
project_root = current_file.parent.parent
sys.path.insert(0, str(project_root))

from config import settings, user_db

# ============================================================================
# ГЛОБАЛЬНЫЕ НАСТРОЙКИ
# ============================================================================

PAUSE_AFTER_PARSING = 240  # 4 минуты пауза после завершения парсинга
CHECK_INTERVAL = 30  # Проверка каждые 30 секунд

ADMIN_VK_ID = int(settings.admin_vk_id) if hasattr(settings, "admin_vk_id") else None

# ============================================================================
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# ============================================================================

_parse_task: Optional[asyncio.Task] = None
_last_successful_run: Optional[datetime] = None
_parse_start_time: Optional[datetime] = None
_parse_lock = asyncio.Lock()

# ============================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================================


def setup_logging():
    """Настраивает логирование с ротацией"""
    Path("logs").mkdir(exist_ok=True)
    logger.remove()

    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )

    logger.add(
        "logs/bot.log",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        level=settings.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function}:{line} | {message}",
        backtrace=True,
        diagnose=True,
    )
    logger.info("📋 Логирование настроено")


# ============================================================================
# УТИЛИТЫ VK
# ============================================================================


def _random_id() -> int:
    """Генерирует уникальный random_id для messages.send"""
    return int(time.time() * 1000000) + random.randint(0, 999999)


async def send_message(user_id: int, text: str, keyboard: Optional[str] = None) -> bool:
    """Отправляет сообщение пользователю VK"""
    global _bot
    if not _bot:
        logger.error("❌ Бот не инициализирован")
        return False
    try:
        await _bot.api.messages.send(
            user_id=user_id,
            message=text,
            random_id=_random_id(),
            keyboard=keyboard,
        )
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки сообщения {user_id}: {e}")
        return False


async def notify_admin(text: str) -> bool:
    """Отправляет уведомление админу"""
    if not ADMIN_VK_ID:
        logger.warning("⚠️ ADMIN_VK_ID не настроен")
        return False
    return await send_message(ADMIN_VK_ID, text)


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# ============================================================================

_bot: Optional[Bot] = None


def get_bot() -> Bot:
    """Ленивая инициализация бота"""
    global _bot
    if _bot is None:
        _bot = Bot(token=settings.vk_group_token)
    return _bot


# ============================================================================
# ХЕНДЛЕРЫ СООБЩЕНИЙ
# ============================================================================


async def start_handler(message: Message):
    """Обработка команды /start"""
    user_id = message.from_id
    bot = get_bot()

    # Получаем информацию о пользователе из VK API
    try:
        users_info = await bot.api.users.get(user_ids=[user_id])
        if users_info:
            vk_user = users_info[0]
            first_name = vk_user.first_name or ""
            last_name = vk_user.last_name or ""
            screen_name = getattr(vk_user, "screen_name", "") or ""
        else:
            first_name = str(user_id)
            last_name = ""
            screen_name = ""
    except Exception as e:
        logger.warning(f"⚠️ Не удалось получить info пользователя {user_id}: {e}")
        first_name = str(user_id)
        last_name = ""
        screen_name = ""

    existing = user_db.get_user(user_id)

    # Админ всегда active
    if user_id == ADMIN_VK_ID:
        if not existing:
            user_db.add_request(
                chat_id=user_id,
                username=screen_name,
                first_name=first_name,
                last_name=last_name,
                status="active",
            )
            logger.info(f"✅ Админ {user_id} добавлен как active")
        elif existing.get("status") != "active":
            user_db.approve_user(user_id, admin_id=ADMIN_VK_ID)

        # Админ-панель
        keyboard = (
            VKKeyboard(inline=True)
            .add(
                Text("👥 Пользователи", payload={"cmd": "admin_users"}),
                color=KeyboardButtonColor.PRIMARY,
            )
            .row()
            .add(
                Text("⏳ Заявки", payload={"cmd": "admin_pending"}),
                color=KeyboardButtonColor.SECONDARY,
            )
            .row()
            .add(
                Text("▶️ Запустить парсинг", payload={"cmd": "admin_parse"}),
                color=KeyboardButtonColor.POSITIVE,
            )
            .get_json()
        )

        await message.answer(
            message=(
                f"👋 Привет, админ!\n\n"
                f"🤖 Бот парсера отказов запущен.\n"
                f"⏱ Пауза после парсинга: {PAUSE_AFTER_PARSING // 60} мин\n"
                f"🔍 Проверка каждые: {CHECK_INTERVAL} сек\n"
                f"👥 Активных: {len(user_db.get_active_users() or [])}\n"
                f"⏳ Ожидают: {len(user_db.get_pending_users() or [])}"
            ),
            keyboard=keyboard,
        )
        return

    # Обычный пользователь
    if not existing:
        user_db.add_request(
            chat_id=user_id,
            username=screen_name,
            first_name=first_name,
            last_name=last_name,
            status="pending",
        )
        logger.info(f"⏳ Новая заявка от {user_id} ({first_name} {last_name})")

        # Уведомляем админа
        await notify_admin(
            f"📩 Новая заявка!\n\n"
            f"👤 ID: {user_id}\n"
            f"📝 Имя: {first_name} {last_name}\n"
            f"🔗 VK: @id{user_id}\n\n"
            f"Для одобрения: /approve_{user_id}\n"
            f"Для отклонения: /reject_{user_id}"
        )

        await message.answer(
            message=(
                f"👋 Привет, {first_name}!\n\n"
                f"⏳ Ваша заявка на доступ отправлена администратору.\n"
                f"Ожидайте одобрения — вы получите уведомление."
            )
        )
        return

    if existing.get("status") == "pending":
        await message.answer(
            message=(
                f"⏳ {first_name}, ваша заявка всё ещё на рассмотрении.\n"
                f"Ожидайте одобрения администратора."
            )
        )
        return

    if existing.get("status") == "blocked":
        await message.answer(message="❌ Ваша заявка была отклонена. Доступ запрещён.")
        return

    if existing.get("status") == "active":
        await message.answer(
            message=(
                f"👋 С возвращением, {first_name}!\n\n"
                f"✅ Вы подключены к уведомлениям.\n"
                f"Как только найдутся новые отказы — вы получите сообщение."
            )
        )
        return


async def admin_handler(message: Message):
    """Обработка админ-команд и кнопок"""
    user_id = message.from_id
    if user_id != ADMIN_VK_ID:
        await message.answer(message="❌ У вас нет доступа к админ-панели.")
        return

    text = (message.text or "").lower()

    # Обработка callback-кнопок (payload)
    payload = message.payload
    if payload:
        try:
            import json

            payload_data = json.loads(payload)
            cmd = payload_data.get("cmd", "")

            if cmd == "admin_users" or cmd == "admin_back":
                await _show_admin_menu(user_id)
                return
            elif cmd == "admin_pending":
                await _show_pending(user_id)
                return
            elif cmd == "admin_active":
                await _show_active(user_id)
                return
            elif cmd == "admin_all_users":
                await _show_all_users(user_id, page=1)
                return
            elif cmd == "admin_users_page":
                page = payload_data.get("page", 1)
                await _show_all_users(user_id, page=page)
                return
            elif cmd == "admin_parse":
                await message.answer(message="⏳ Запуск парсинга отказов...")
                asyncio.create_task(auto_parse_job())
                return
            elif cmd == "delete_confirm":
                target_id = payload_data.get("id")
                if target_id:
                    await _delete_confirm(user_id, target_id)
                return
            elif cmd == "delete_execute":
                target_id = payload_data.get("id")
                if target_id:
                    await _delete_execute(user_id, target_id)
                return
        except Exception as e:
            logger.error(f"❌ Ошибка обработки payload: {e}")

    # Текстовые команды
    if text in ["/admin", "👥 пользователи"]:
        await _show_admin_menu(user_id)
    elif text == "⏳ заявки":
        await _show_pending(user_id)
    elif text == "✅ активные":
        await _show_active(user_id)
    elif text == "▶️ запустить парсинг":
        await message.answer(message="⏳ Запуск парсинга отказов...")
        asyncio.create_task(auto_parse_job())


async def _show_admin_menu(user_id: int):
    """Показывает главное меню админа"""
    from bot.admin_handlers import admin_command

    await admin_command(user_id, send_message)


async def _show_pending(user_id: int):
    """Показывает заявки"""
    from bot.admin_handlers import admin_pending_callback

    await admin_pending_callback(user_id, send_message)


async def _show_active(user_id: int):
    """Показывает активных пользователей"""
    from bot.admin_handlers import admin_active_callback

    await admin_active_callback(user_id, send_message)


async def _show_all_users(user_id: int, page: int = 1):
    """Показывает всех пользователей"""
    from bot.admin_handlers import admin_all_users_callback

    await admin_all_users_callback(user_id, send_message, page=page)


async def _delete_confirm(user_id: int, target_id: int):
    """Подтверждение удаления"""
    from bot.admin_handlers import admin_delete_confirm_callback

    await admin_delete_confirm_callback(user_id, send_message, target_id)


async def _delete_execute(user_id: int, target_id: int):
    """Удаление пользователя"""
    from bot.admin_handlers import admin_delete_execute_callback

    await admin_delete_execute_callback(user_id, send_message, target_id)


async def approve_reject_handler(message: Message):
    """Обработка одобрения/отклонения заявок"""
    user_id = message.from_id
    if user_id != ADMIN_VK_ID:
        await message.answer(message="❌ Нет доступа.")
        return

    text = message.text or ""
    if text.startswith("/approve_"):
        try:
            target_id = int(text.split("_")[1])
        except (IndexError, ValueError):
            await message.answer(
                message="❌ Неверный формат. Используйте: /approve_123456"
            )
            return

        if user_db.approve_user(target_id, admin_id=ADMIN_VK_ID):
            await send_message(
                target_id,
                "✅ Ваша заявка одобрена!\n\n"
                "Теперь вы будете получать уведомления о найденных отказах.",
            )
            await message.answer(message=f"✅ Пользователь {target_id} одобрен.")
            logger.info(f"✅ Админ одобрил пользователя {target_id}")
        else:
            await message.answer(
                message=f"❌ Не удалось одобрить пользователя {target_id}."
            )

    elif text.startswith("/reject_"):
        try:
            target_id = int(text.split("_")[1])
        except (IndexError, ValueError):
            await message.answer(
                message="❌ Неверный формат. Используйте: /reject_123456"
            )
            return

        if user_db.reject_user(target_id, admin_id=ADMIN_VK_ID):
            await send_message(
                target_id, "❌ Ваша заявка отклонена.\n\n" "Доступ к боту запрещён."
            )
            await message.answer(message=f"❌ Пользователь {target_id} отклонён.")
            logger.info(f"❌ Админ отклонил пользователя {target_id}")
        else:
            await message.answer(
                message=f"❌ Не удалось отклонить пользователя {target_id}."
            )

    elif text.startswith("/delete_"):
        try:
            target_id = int(text.split("_")[1])
        except (IndexError, ValueError):
            await message.answer(
                message="❌ Неверный формат. Используйте: /delete_123456"
            )
            return

        await _delete_confirm(user_id, target_id)


async def handle_text_handler(message: Message):
    """Обработка всех остальных сообщений (кнопки меню)"""
    user_id = message.from_id
    text = message.text or ""

    if not text.strip():
        return

    if text.startswith("/"):
        return

    # Обработка кнопок меню через handlers.py
    from bot.handlers import handle_text

    await handle_text(user_id, text, send_message)


# ============================================================================
# АВТОПАРСИНГ
# ============================================================================


async def auto_parse_job():
    """Гибкий автопарсинг отказов"""
    global _parse_task, _last_successful_run, _parse_start_time

    from bot.user_manager import get_user_manager

    manager = get_user_manager()
    if not manager:
        logger.error("❌ [AUTO] UserManager не инициализирован!")
        return

    if _parse_lock.locked():
        logger.debug("⏭ [AUTO] Парсинг уже выполняется, пропускаем")
        return

    if _last_successful_run:
        elapsed = (datetime.now() - _last_successful_run).total_seconds()
        if elapsed < PAUSE_AFTER_PARSING:
            remaining = PAUSE_AFTER_PARSING - elapsed
            logger.debug(
                f"⏳ [AUTO] Пауза после парсинга: {int(remaining)//60}м {int(remaining)%60:02d}с осталось"
            )
            return

    async with _parse_lock:
        if _parse_task and not _parse_task.done():
            logger.info("⏳ [AUTO] Парсинг уже выполняется, ждём...")
            return

        _parse_start_time = datetime.now()
        logger.info(f"⏰ [AUTO] Запуск парсинга отказов...")

        async def run_parsing():
            try:
                return await manager.run_for_all_active()
            except asyncio.CancelledError:
                logger.warning("⚠️ [AUTO] Парсинг был отменён")
                raise
            except Exception as e:
                logger.error(f"❌ [AUTO] Ошибка в run_parsing: {e}")
                raise

        _parse_task = asyncio.create_task(run_parsing())

        try:
            result = await _parse_task

            _last_successful_run = datetime.now()
            duration = (_last_successful_run - _parse_start_time).total_seconds()
            duration_min = duration / 60

            total_found = result.get("total_found", 0)
            processed = result.get("processed", 0)
            today_count = result.get("today_count", 0)

            logger.info(
                f"✅ [AUTO] Парсинг отказов завершён за {duration_min:.1f} мин. "
                f"Обработано: {processed}, найдено: {total_found}, сегодня: {today_count}. "
                f"Следующий через {PAUSE_AFTER_PARSING//60} мин"
            )

        except asyncio.CancelledError:
            logger.warning("⚠️ [AUTO] Задача парсинга была отменена")

        except Exception as e:
            logger.error(f"❌ [AUTO] Критическая ошибка: {e}", exc_info=True)
            _last_successful_run = datetime.now()

        finally:
            _parse_task = None


async def auto_parse_loop():
    """Бесконечный цикл автопарсинга"""
    logger.info(f"⏰ Автопарсинг отказов запущен: проверка каждые {CHECK_INTERVAL}с")
    while True:
        try:
            await auto_parse_job()
        except Exception as e:
            logger.error(f"❌ Ошибка в цикле автопарсинга: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ И ЗАПУСК
# ============================================================================


async def init_services():
    """Инициализация сервисов перед запуском бота"""
    # 1. Инициализация Яндекс.Диска
    yandex = None
    try:
        from services.yandex_disk import get_yandex_service

        yandex = get_yandex_service()
        space = await yandex.get_free_space()
        if "free_gb" in space:
            logger.info(f"💾 Яндекс.Диск: {space['free_gb']:.2f} ГБ свободно")
    except Exception as e:
        logger.warning(f"⚠️ Яндекс.Диск: {e}")

    # 2. Инициализация UserManager
    try:
        from src.rejection_parser import RejectionParser
        from bot.user_manager import init_user_manager, get_user_manager

        init_user_manager(RejectionParser, yandex, send_message)
        logger.info("✅ UserManager инициализирован")

        manager = get_user_manager()
        if manager:
            await manager.start_notification_processor()
            logger.info("🔔 Процессор уведомлений об отказах запущен")

    except Exception as e:
        logger.error(f"❌ Ошибка инициализации UserManager: {e}", exc_info=True)

    # 3. Уведомление админа
    try:
        await notify_admin(
            f"🤖 Бот парсера отказов запущен\n\n"
            f"✅ Режим: Гибкий автопарсинг\n"
            f"⏱ Пауза после парсинга: {PAUSE_AFTER_PARSING // 60} мин\n"
            f"🔍 Проверка каждые: {CHECK_INTERVAL} сек\n"
            f"👥 Активных: {len(user_db.get_active_users() or [])}\n"
            f"⏳ Ожидают: {len(user_db.get_pending_users() or [])}"
        )
    except Exception as e:
        logger.error(f"❌ Не удалось уведомить админа: {e}")


async def shutdown():
    """Корректное завершение работы"""
    global _parse_task
    logger.info("🛑 Завершение работы...")

    # Останавливаем процессор уведомлений
    try:
        from bot.user_manager import get_user_manager

        manager = get_user_manager()
        if manager:
            manager.stop_notification_processor()
            logger.info("⏹ Процессор уведомлений остановлен")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка остановки процессора: {e}")

    # Отменяем висящую задачу парсинга
    if _parse_task and not _parse_task.done():
        _parse_task.cancel()
        try:
            await _parse_task
        except asyncio.CancelledError:
            pass

    try:
        await notify_admin("🛑 Бот парсера отказов остановлен")
    except Exception as e:
        logger.error(f"⚠️ Не удалось уведомить об остановке: {e}")


async def main():
    """Точка входа"""
    setup_logging()
    logger.info("🚀 Запуск VK бота парсера отказов...")
    logger.info(f"🔧 Admin VK ID: {ADMIN_VK_ID}")

    # Инициализация бота
    bot = get_bot()
    logger.info("✅ VK бот инициализирован")

    # Регистрация хендлеров
    bot.on.message(text=["/start", "Начать", "Старт", "Start"])(start_handler)
    bot.on.message(
        text=[
            "/admin",
            "👥 Пользователи",
            "⏳ Заявки",
            "✅ Активные",
            "▶️ Запустить парсинг",
        ]
    )(admin_handler)
    bot.on.message(text=["/approve_<uid>", "/reject_<uid>", "/delete_<uid>"])(
        approve_reject_handler
    )
    bot.on.message()(handle_text_handler)

    logger.info("✅ Хендлеры зарегистрированы")

    # Инициализация сервисов
    await init_services()

    # Запуск автопарсинга в фоне
    parse_task = asyncio.create_task(auto_parse_loop())

    # Запуск бота
    try:
        logger.info("🔄 Запуск polling VK...")
        await bot.run_polling()
    except asyncio.CancelledError:
        logger.info("🛑 Polling отменён")
    finally:
        parse_task.cancel()
        try:
            await parse_task
        except asyncio.CancelledError:
            pass
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⌨️ Прервано пользователем")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}", exc_info=True)
    finally:
        logger.info("👋 Завершение работы")

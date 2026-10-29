# bot/user_manager.py
"""
Управление парсингом отказов и уведомлениями (VK версия).
Версия: Автоматический режим, только админ парсит.
ИСПРАВЛЕНО: Ссылка на отчет теперь берется из результата парсера,
а не генерируется повторно (что ломало ссылку для VK).
"""

import asyncio
from pathlib import Path
from typing import Optional, Dict, Callable, Awaitable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from loguru import logger

from config import user_db, get_user_settings, settings
from src.yandex_excel_manager import YandexExcelManager
from src.rejections_cache import RejectionsCache
from src.rejection_parser import DailyStats, create_rejection_parser

# Очередь для моментальных уведомлений из потока парсера
_notification_queue: asyncio.Queue = asyncio.Queue()


class UserManager:
    """Управляет парсингом отказов для админа и уведомлениями для всех активных пользователей"""

    def __init__(
        self,
        parser_class,
        yandex_service,
        send_message_func: Callable[[int, str], Awaitable[bool]],
    ):
        self.parser_class = parser_class
        self.yandex = yandex_service
        self.send_message = send_message_func
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="rejection_parser"
        )
        self._daily_stats = DailyStats()
        self._last_run_time: Optional[datetime] = None
        self._parsing_task: Optional[asyncio.Task] = None
        self._notification_task: Optional[asyncio.Task] = None
        self._reject_seq_today: int = 0
        self._reject_seq_date: Optional[str] = None  # "25.09.2026"
        self._reject_seq_lock = asyncio.Lock()

    def __del__(self):
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False)

    def should_run(self, min_interval_seconds: int = 300) -> bool:
        """Проверяет, можно ли запускать парсинг"""
        if self._last_run_time is None:
            return True
        elapsed = (datetime.now() - self._last_run_time).total_seconds()
        if elapsed < 0:
            return True
        if elapsed < min_interval_seconds:
            remaining = min_interval_seconds - elapsed
            logger.info(f"⏳ [should_run] Осталось {remaining:.0f}с")
            return False
        return True

    def mark_run_start(self):
        """Отмечает начало запуска парсинга"""
        self._last_run_time = datetime.now()
        logger.info(f"📝 [mark_run_start] Время запуска: {self._last_run_time}")

    async def start_notification_processor(self):
        """Запускает обработчик очереди уведомлений об отказах"""
        if self._notification_task is None or self._notification_task.done():
            self._notification_task = asyncio.create_task(
                self._process_notification_queue()
            )
            logger.info(" Процессор уведомлений об отказах запущен")

    async def _next_reject_seq(self) -> int:
        """№ отказа за сегодня (сброс при смене даты)."""
        today = datetime.now().strftime("%d.%m.%Y")
        async with self._reject_seq_lock:
            if self._reject_seq_date != today:
                self._reject_seq_date = today
                self._reject_seq_today = 0
            self._reject_seq_today += 1
            return self._reject_seq_today

    async def _process_notification_queue(self):
        """Обрабатывает очередь уведомлений об отказах из потока парсера"""
        while True:
            try:
                notification = await asyncio.wait_for(
                    _notification_queue.get(), timeout=1.0
                )

                contract_number = notification["contract_number"]
                reject_url = notification["reject_url"]
                reject_date = notification["reject_date"]
                registry_placed = notification.get("registry_placed", "")
                registry_updated = notification.get("registry_updated", "")
                admin_chat_id = notification["admin_chat_id"]
                supplier_name = notification.get("supplier_name", "")
                email = notification.get("email", "")
                email_sent = notification.get("email_sent", False)
                email_status = notification.get("email_status", "")

                # опциональные поля из processing
                contract_date = notification.get("contract_date", "")
                customer_name = notification.get("customer_name", "")
                price = notification.get("price", "")
                region = notification.get("region", "")
                reestr_number = notification.get(
                    "reestr_number", contract_number
                )

                seq = await self._next_reject_seq()
                today_str = datetime.now().strftime("%d.%m.%Y")

                message_lines = [
                    f"🚫 НОВЫЙ ОТКАЗ №{seq} ({today_str})!\n",
                    f"Номер контракта: {contract_number}",
                    f"📅 Дата отказа: {reject_date}",
                ]
                if registry_placed:
                    message_lines.append(f"📌 Размещен в реестре: {registry_placed}")
                if registry_updated:
                    message_lines.append(f"🔄 Обновлен в реестре: {registry_updated}")
                if contract_date:
                    message_lines.append(
                        f"📋 Дата заключения контракта: {contract_date}"
                    )
                if customer_name:
                    message_lines.append(f"🏛 Заказчик: {customer_name[:70]}")
                if supplier_name:
                    message_lines.append(f"🏢 Поставщик: {supplier_name[:60]}")
                if price:
                    message_lines.append(f"💰 Цена: {price}")
                if region:
                    message_lines.append(f"📍 Регион: {region[:40]}")

                if email:
                    if email_sent:
                        message_lines.append(f"📧 Письмо отправлено: {email}")
                    elif email_status == "already_sent":
                        message_lines.append(
                            f"⏭ Письмо уже отправлялось ранее: {email}"
                        )
                    elif email_status == "no_email":
                        message_lines.append("⚠️ Email не найден")
                    elif email_status == "disabled":
                        message_lines.append("⏭ Отправка писем отключена")
                    elif email_status == "queued":
                        message_lines.append(f"✉️ Письмо в очереди: {email}")
                    else:
                        message_lines.append(
                            f"❌ Письмо не отправлено: {email_status}"
                        )
                else:
                    message_lines.append("⚠️ Email не найден")

                message_lines.append(f"\n🔗 Отказ: {reject_url}")
                if reestr_number:
                    card_url = (
                        "https://zakupki.gov.ru/epz/contract/contractCard/"
                        f"common-info.html?reestrNumber={reestr_number}"
                    )
                    message_lines.append(f"📄 Контракт: {card_url}")

                message_lines.append("\n#отказ #закупки")
                message = "\n".join(message_lines)

                # админ
                try:
                    await self.send_message(int(admin_chat_id), message)
                    logger.info(
                        f"✅ Моментальное уведомление админу №{seq}: {contract_number}"
                    )
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки админу: {e}")

                # активные пользователи
                users = user_db.get_active_users()
                for user in users:
                    chat_id = user.get("chat_id")
                    if not chat_id or str(chat_id) == str(admin_chat_id):
                        continue
                    try:
                        await self.send_message(int(chat_id), message)
                        logger.info(
                            f"✅ Уведомление {chat_id} №{seq}: {contract_number}"
                        )
                    except Exception as e:
                        logger.error(f"❌ Ошибка отправки {chat_id}: {e}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.info("⏹ Процессор уведомлений об отказах остановлен")
                break
            except Exception as e:
                logger.error(f"❌ Ошибка в процессоре уведомлений: {e}")
                await asyncio.sleep(1)

    def stop_notification_processor(self):
        """Останавливает обработчик очереди"""
        if self._notification_task and not self._notification_task.done():
            self._notification_task.cancel()
            logger.info("⏹ Процессор уведомлений об отказах остановлен")

    async def run_for_all_active(self) -> Dict:
        """
        Запускает парсинг отказов для админа, остальным отправляет уведомления.
        """
        users = user_db.get_active_users() or []
        admin_chat_id = int(settings.admin_vk_id)

        # Всегда добавляем админа
        admin_found = any(u.get("chat_id") == admin_chat_id for u in users)
        if not admin_found:
            users = list(users) + [{"chat_id": admin_chat_id, "is_admin": True}]
            logger.info(f"👤 Админ {admin_chat_id} добавлен в список активных")

        if not users:
            logger.info("📭 Нет активных пользователей")
            return {
                "total_found": 0,
                "processed": 0,
                "errors": 0,
                "today_count": 0,
                "new_this_session": 0,
                "public_url": None,
            }

        logger.info(f"🚀 Парсинг отказов для {len(users)} пользователей")

        total_found, total_processed, total_errors = 0, 0, 0
        total_new_this_session = 0
        public_url = None

        # 1. Парсинг для админа
        logger.info(f"🚀 Парсинг отказов для админа {admin_chat_id}")
        try:
            result = await self._parse_user_non_blocking(admin_chat_id)
            total_found += result.get("found", 0)
            total_processed += result.get("processed", 0)
            total_errors += result.get("errors", 0)
            total_new_this_session += result.get("new_this_session", 0)

            # Берем ссылку из результата парсера (она уже рабочая)
            if result.get("public_url"):
                public_url = result["public_url"]
                logger.info(f"✅ Получена рабочая ссылка для отчета: {public_url}")
                from bot.handlers import set_last_public_url
                set_last_public_url(public_url)
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга для админа: {e}")
            total_errors += 1

        # 2. Уведомления остальным пользователям
        for i, user in enumerate(users, 1):
            chat_id = user.get("chat_id")
            if not chat_id or int(chat_id) == admin_chat_id:
                continue

            logger.info(f"📨 [{i}/{len(users)}] Уведомление пользователю {chat_id}")

            try:
                if public_url:
                    message = (
                        f"🚫 Автонайдено отказов: {total_new_this_session}\n"
                        f"📊 Обработано контрактов: {total_processed}\n\n"
                        f"📁 Отчёт на Яндекс.Диске:\n🔗 {public_url}"
                    )
                    await self.send_message(int(chat_id), message)
                else:
                    logger.warning(
                        f"⚠️ Нет публичной ссылки для пользователя {chat_id}"
                    )
            except Exception as e:
                logger.error(f"❌ Ошибка уведомления пользователю {chat_id}: {e}")

            if i < len(users):
                await asyncio.sleep(5)

        daily = self._daily_stats.get_stats()

        logger.info(f"✅ Парсинг отказов завершён: {total_new_this_session} отказов")

        return {
            "total_found": total_found,
            "processed": total_processed,
            "errors": total_errors,
            "today_count": daily["today"],
            "new_this_session": total_new_this_session,
            "public_url": public_url,
        }

    async def _parse_user_non_blocking(self, chat_id: int) -> Dict:
        """Выполняет парсинг отказов в отдельном потоке"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self._parse_user_sync, chat_id
        )

    def _parse_user_sync(self, chat_id: int) -> Dict:
        """Синхронная версия парсинга отказов (в потоке)"""
        import asyncio as thread_asyncio

        thread_loop = thread_asyncio.new_event_loop()
        thread_asyncio.set_event_loop(thread_loop)

        user_settings = get_user_settings(chat_id)
        is_admin = int(chat_id) == int(settings.admin_vk_id)

        if not user_settings.is_configured and not is_admin:
            logger.warning(f"⚠️ Пользователь {chat_id} не настроен")
            return {
                "found": 0,
                "processed": 0,
                "errors": 0,
                "new_this_session": 0,
                "public_url": None,
            }

        local_data_dir = Path(f"data/users/{chat_id}")
        local_temp_dir = Path(f"temp/{chat_id}")
        local_data_dir.mkdir(parents=True, exist_ok=True)
        local_temp_dir.mkdir(parents=True, exist_ok=True)

        cache_file = local_data_dir / "rejections_cache.json"

        rejections_cache = RejectionsCache(cache_file)

        excel_manager = YandexExcelManager(
            yandex_service=self.yandex,
            chat_id=chat_id,
            local_temp_dir=local_temp_dir,
        )

        try:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as init_executor:
                future = init_executor.submit(
                    lambda: thread_asyncio.run(excel_manager.initialize())
                )
                future.result(timeout=10)
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации ExcelManager: {e}")
            return {
                "found": 0,
                "processed": 0,
                "errors": 1,
                "new_this_session": 0,
                "public_url": None,
            }

        old_stats = excel_manager.get_stats()
        old_count = old_stats.get("total", 0)

        # ИСПРАВЛЕНИЕ: Берем ссылку напрямую из результата парсера,
        # так как run_parallel() уже вызывает force_save() внутри себя.
        # Повторный вызов force_save() здесь ломал ссылку.
        public_url = None

        try:
            parser = create_rejection_parser(
                excel_manager=excel_manager,
                rejections_cache=rejections_cache,
                admin_vk_id=settings.admin_vk_id,
            )

            # run_parallel() сам сохраняет файл и возвращает excel_url
            result = parser.run_parallel(
                max_pages=settings.max_parse_pages_rejections,
                max_workers=settings.max_workers_rejections,
            )

            # ИЗВЛЕКАЕМ ССЫЛКУ ИЗ РЕЗУЛЬТАТА ПАРСЕРА
            public_url = result.get("excel_url")

            if public_url:
                logger.info(f"🔗 Использована ссылка из парсера: {public_url}")
            else:
                logger.warning("⚠️ Парсер не вернул ссылку (excel_url=None)")

            new_stats = excel_manager.get_stats()
            new_count = new_stats.get("total", 0)
            new_this_session = max(0, new_count - old_count)

            self._daily_stats.add_found(new_this_session)

            logger.info(
                f"✅ Парсинг отказов для {chat_id} завершён. Новых: {new_this_session}"
            )
            return {
                "found": result.get("total_found", 0),
                "processed": result.get("processed", 0),
                "errors": result.get("errors", 0),
                "new_this_session": new_this_session,
                "public_url": public_url,
            }

        except Exception as e:
            logger.error(
                f"❌ Ошибка парсинга отказов для {chat_id}: {e}", exc_info=True
            )
            return {
                "found": 0,
                "processed": 0,
                "errors": 1,
                "new_this_session": 0,
                "public_url": None,
            }
        finally:
            try:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=1
                ) as close_executor:
                    future = close_executor.submit(
                        lambda: thread_asyncio.run(excel_manager.close())
                    )
                    close_url = future.result(timeout=10)
                    # Используем close_url только если у нас еще нет рабочей ссылки
                    if close_url and not public_url:
                        public_url = close_url
                        logger.info(f" Использована ссылка из close(): {public_url}")
            except Exception as e:
                logger.error(f"❌ Ошибка при закрытии ExcelManager: {e}")
            finally:
                thread_loop.close()


# ============================================================================
# ГЛОБАЛЬНЫЙ МЕНЕДЖЕР
# ============================================================================

user_manager: Optional[UserManager] = None


def init_user_manager(
    parser_class,
    yandex_service,
    send_message_func: Callable[[int, str], Awaitable[bool]],
) -> UserManager:
    """Инициализирует глобальный менеджер"""
    global user_manager
    user_manager = UserManager(parser_class, yandex_service, send_message_func)
    logger.info("🔧 UserManager для отказов инициализирован")
    return user_manager


def get_user_manager() -> Optional[UserManager]:
    """Возвращает глобальный экземпляр"""
    return user_manager

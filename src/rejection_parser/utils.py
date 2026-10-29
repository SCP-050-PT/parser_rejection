"""
utils.py
Утилиты для парсера отказов.
VK версия — убраны Telegram-зависимости.
ИСПРАВЛЕНИЕ v6.3: Убрана ротация UA (не используется)
"""

import asyncio
from threading import Lock
from datetime import datetime
from typing import Dict, Any, Optional
from loguru import logger


class DailyStats:
    """Статистика за текущий день (сброс в 00:00)"""

    def __init__(self):
        self._date = datetime.now().date()
        self._today_count = 0
        self._session_count = 0
        self._lock = Lock()

    def _check_reset(self):
        current_date = datetime.now().date()
        with self._lock:
            if current_date != self._date:
                self._date = current_date
                self._today_count = 0
                logger.info("📅 Новый день! Счётчик 'сегодня' сброшен")

    def add_found(self, count: int = 1):
        self._check_reset()
        with self._lock:
            self._today_count += count
            self._session_count += count

    def reset_session(self):
        with self._lock:
            self._session_count = 0

    def get_stats(self) -> Dict[str, int]:
        self._check_reset()
        with self._lock:
            return {"today": self._today_count, "session": self._session_count}


def run_async_in_thread(coro) -> Any:
    """Запускает корутину в новом event loop (для потоков)"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(coro)
        loop.close()
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка в run_async_in_thread: {e}")
        return None


def create_rejection_parser(
    excel_manager,
    rejections_cache,
    admin_vk_id: Optional[str] = None,
):
    """Фабрика парсера отказов (VK версия — без bot)"""
    from src.rejection_parser.core import RejectionParser

    return RejectionParser(
        excel_manager=excel_manager,
        rejections_cache=rejections_cache,
        admin_vk_id=admin_vk_id,
    )

"""
Утилиты для парсера отказов.
VK версия — убраны Telegram-зависимости.
"""

import random
import asyncio
from threading import Lock
from datetime import datetime
from typing import Dict, Any, Optional
from loguru import logger

# === РОТАЦИЯ USER-AGENT ===
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def get_platform_from_ua(user_agent: str) -> str:
    if "Windows" in user_agent:
        return "Windows"
    elif "Macintosh" in user_agent:
        return "macOS"
    elif "Linux" in user_agent:
        return "Linux"
    return "Windows"


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

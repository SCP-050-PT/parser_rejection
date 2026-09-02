# bot/parsing_state.py
"""
Глобальное состояние парсинга.
"""

from threading import Lock
from datetime import datetime
from loguru import logger


class ParsingState:
    """Глобальное состояние парсинга"""

    def __init__(self):
        self._lock = Lock()
        self._is_running = False
        self._started_by = None
        self._started_at = None

    @property
    def is_running(self) -> bool:
        """Проверяет, запущен ли парсинг"""
        with self._lock:
            return self._is_running

    @is_running.setter
    def is_running(self, value: bool):
        """Устанавливает статус"""
        with self._lock:
            self._is_running = value

    def start(self, admin_chat_id: int) -> bool:
        """Запускает парсинг"""
        with self._lock:
            if self._is_running:
                return False
            self._is_running = True
            self._started_by = admin_chat_id
            self._started_at = datetime.now()
            logger.info(f"🚀 Парсинг запущен админом {admin_chat_id}")
            return True

    def stop(self, admin_chat_id: int) -> bool:
        """Останавливает парсинг"""
        with self._lock:
            if not self._is_running:
                return False
            self._is_running = False
            duration = datetime.now() - self._started_at if self._started_at else None
            logger.info(
                f"⏹ Парсинг остановлен админом {admin_chat_id}"
                f"{' (длился ' + str(duration) + ')' if duration else ''}"
            )
            return True


# Глобальный экземпляр
_parsing_state = ParsingState()


def get_parsing_state() -> ParsingState:
    """Получает глобальное состояние"""
    return _parsing_state

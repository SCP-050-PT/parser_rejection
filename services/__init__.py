"""
Сервисы для работы с внешними API.

Этот модуль предоставляет сервисы для интеграции со сторонними сервисами:
- YandexDiskService: работа с Яндекс.Диском для хранения файлов пользователей

Пример использования:
    from services import get_yandex_service
    
    # Получение глобального экземпляра (рекомендуется)
    yandex = get_yandex_service()  # ← НЕ await!
    url = await yandex.upload_for_user(chat_id, file_path)
    
    # Или создание нового экземпляра (для тестов)
    from services import YandexDiskService
    yandex = YandexDiskService(token="your_token")
"""

# Импортируем основные классы и функции
from .yandex_disk import (
    YandexDiskService,
    get_yandex_service,
    reset_yandex_service,
)

# ============================================================================
# ПУБЛИЧНЫЙ API МОДУЛЯ
# ============================================================================

__all__ = [
    # Сервис Яндекс.Диска
    "YandexDiskService",
    "get_yandex_service",
    "reset_yandex_service",
    
    # Метаданные
    "__version__",
    "check_yandex_health",
]

# ============================================================================
# МЕТАДАННЫЕ
# ============================================================================

__version__ = "1.0.0"
"""Версия модуля сервисов"""


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

async def check_yandex_health() -> dict:
    """
    Проверяет доступность и состояние Яндекс.Диска.
    
    Returns:
        dict с информацией о состоянии:
        - ok: bool (работоспособен ли сервис)
        - free_gb: float (свободное место в ГБ)
        - used_percent: float (процент занятого места)
        - error: str (текст ошибки если есть)
    
    Пример:
        status = await check_yandex_health()
        if status["ok"]:
            logger.info(f"✅ Диск: {status['free_gb']} ГБ свободно")
        else:
            logger.error(f"❌ Ошибка: {status['error']}")
    """
    try:
        yandex = get_yandex_service()  # ← Без await!
        space = await yandex.get_free_space()
        
        if space.get("total_bytes", 0) == 0:
            return {
                "ok": False,
                "free_gb": 0,
                "used_percent": 0,
                "error": "Failed to get disk info"
            }
        
        used_percent = (space["used_bytes"] / space["total_bytes"] * 100) if space["total_bytes"] > 0 else 0
        
        return {
            "ok": True,
            "free_gb": round(space.get("free_gb", 0), 2),
            "used_percent": round(used_percent, 2),
            "error": None
        }
        
    except Exception as e:
        return {
            "ok": False,
            "free_gb": 0,
            "used_percent": 0,
            "error": str(e)
        }


# ============================================================================
# АВТОПРОВЕРКА ПРИ ИМПОРТЕ (только в dev-режиме)
# ============================================================================

def _on_import():
    """Выполняется при импорте модуля (для отладки)"""
    import os
    import sys
    from loguru import logger
    
    # Не выполняем проверки в production или при сборке
    if os.getenv("SKIP_CONFIG_CHECK") or "pytest" in sys.modules:
        return
    
    # Проверяем наличие токена через переменные окружения
    token = os.getenv("YANDEX_DISK_TOKEN")
    if not token or token == "YANDEX_DISK_TOKEN":
        logger.warning(
            "⚠️ YANDEX_DISK_TOKEN не настроен!\n"
            "Проверьте файл .env в корне проекта."
        )
    else:
        logger.debug("🔧 YandexDiskService готов к работе")


# Запускаем проверку при импорте
_on_import()
"""
База данных пользователей и настройки.
Вынесено из settings.py для чистоты архитектуры.
ИСПРАВЛЕНИЕ: Параметры поиска как на zakupki.gov.ru
ИСПРАВЛЕНИЕ 2: add_request() теперь принимает status (админ активируется сразу)
"""

import os
import json
import sqlite3
import shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Union, Any

from loguru import logger

# ============================================================================
# БАЗОВЫЕ ПУТИ
# ============================================================================

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
USERS_DIR = DATA_DIR / "users"

# Создаём директорию при импорте
USERS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# БАЗА ДАННЫХ ПОЛЬЗОВАТЕЛЕЙ (SQLite)
# ============================================================================


class UserDatabase:
    """
    SQLite база данных пользователей.
    Все методы потокобезопасны (новое соединение на каждый вызов).
    """

    DB_PATH = DATA_DIR / "users.db"

    def __init__(self):
        self._init_db()

    def _init_db(self):
        """Создаёт таблицы и настраивает SQLite для многопоточности"""
        with sqlite3.connect(self.DB_PATH, check_same_thread=False) as conn:
            cursor = conn.cursor()

            # Включаем WAL-режим для лучшей конкурентности
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")

            # Таблица пользователей
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    approved_at TIMESTAMP,
                    approved_by INTEGER,
                    settings_json TEXT DEFAULT '{}',
                    yandex_folder TEXT,
                    total_records INTEGER DEFAULT 0,
                    last_activity TIMESTAMP
                )
            """)

            # Таблица истории заявок
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    processed_by INTEGER,
                    processed_at TIMESTAMP,
                    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
                )
            """)

            # Индексы для ускорения поиска
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_chat ON requests(chat_id, status)"
            )

            conn.commit()

    def update_user_stats(self, chat_id: int, records_added: int = 0, **kwargs) -> bool:
        """Обновляет статистику пользователя"""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                cursor = conn.cursor()
                if records_added > 0:
                    cursor.execute(
                        """
                        UPDATE users
                        SET total_records = total_records + ?,
                            last_activity = ?
                        WHERE chat_id = ?
                    """,
                        (records_added, datetime.now(timezone.utc), chat_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE users SET last_activity = ? WHERE chat_id = ?
                    """,
                        (datetime.now(timezone.utc), chat_id),
                    )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления статистики {chat_id}: {e}")
            return False

    # ========================================================================
    # МЕТОДЫ РЕГИСТРАЦИИ И УПРАВЛЕНИЯ
    # ========================================================================

    def add_request(
        self,
        chat_id: int,
        username: str,
        first_name: str,
        last_name: str,
        status: str = "pending",
    ) -> bool:
        """Добавляет заявку на доступ. status='active' для админа — сразу активен."""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO users
                    (chat_id, username, first_name, last_name, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        chat_id,
                        username,
                        first_name,
                        last_name,
                        status,
                        datetime.now(timezone.utc),
                    ),
                )
                cursor.execute("INSERT INTO requests (chat_id) VALUES (?)", (chat_id,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка добавления заявки {chat_id}: {e}")
            return False

    def approve_user(self, chat_id: int, admin_id: Optional[int] = None) -> bool:
        """Одобряет заявку пользователя"""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                cursor = conn.cursor()
                yandex_folder = f"parser_users/{chat_id}/"
                cursor.execute(
                    """
                    UPDATE users
                    SET status = 'active', approved_at = ?, approved_by = ?, yandex_folder = ?
                    WHERE chat_id = ?
                """,
                    (datetime.now(timezone.utc), admin_id, yandex_folder, chat_id),
                )
                cursor.execute(
                    """
                    UPDATE requests SET status = 'approved', processed_by = ?, processed_at = ?
                    WHERE chat_id = ? AND status = 'pending'
                """,
                    (admin_id, datetime.now(timezone.utc), chat_id),
                )
                conn.commit()

                # Создаём директорию и дефолтные настройки
                user_dir = USERS_DIR / str(chat_id)
                user_dir.mkdir(parents=True, exist_ok=True)
                default_settings = {
                    "date_from": "",
                    "date_to": "",
                    "price_from": 0,
                    "price_to": 1_000_000_000,
                    "notifications_enabled": True,
                    "search_mode": "updates",
                }
                with open(user_dir / "settings.json", "w", encoding="utf-8") as f:
                    json.dump(default_settings, f, indent=2, ensure_ascii=False)
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка одобрения {chat_id}: {e}")
            return False

    def reject_user(self, chat_id: int, admin_id: Optional[int] = None) -> bool:
        """Отклоняет заявку пользователя"""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET status = 'blocked' WHERE chat_id = ?", (chat_id,)
                )
                cursor.execute(
                    """
                    UPDATE requests SET status = 'rejected', processed_by = ?, processed_at = ?
                    WHERE chat_id = ? AND status = 'pending'
                """,
                    (admin_id, datetime.now(timezone.utc), chat_id),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка отклонения {chat_id}: {e}")
            return False

    def delete_user(self, chat_id: int) -> bool:
        """Полностью удаляет пользователя (БД + локальные файлы)"""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM requests WHERE chat_id = ?", (chat_id,))
                cursor.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
                conn.commit()
            user_dir = USERS_DIR / str(chat_id)
            if user_dir.exists():
                shutil.rmtree(user_dir)
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка удаления {chat_id}: {e}")
            return False

    # ========================================================================
    # МЕТОДЫ ПОЛУЧЕНИЯ ДАННЫХ
    # ========================================================================

    def get_user(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Получает данные пользователя"""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"❌ Ошибка получения пользователя {chat_id}: {e}")
            return None

    def get_pending_users(self) -> List[Dict[str, Any]]:
        """Получает список пользователей со статусом pending"""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM users WHERE status = 'pending' ORDER BY created_at DESC"
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"❌ Ошибка получения pending-пользователей: {e}")
            return []

    def get_active_users(self) -> List[Dict[str, Any]]:
        """Получает список активных пользователей"""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM users WHERE status = 'active' ORDER BY created_at DESC"
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"❌ Ошибка получения активных пользователей: {e}")
            return []

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Получает всех пользователей"""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM users ORDER BY created_at DESC")
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"❌ Ошибка получения всех пользователей: {e}")
            return []

    def is_approved(self, chat_id: int) -> bool:
        """Проверяет, одобрен ли пользователь"""
        user = self.get_user(chat_id)
        return user is not None and user.get("status") == "active"

    # ========================================================================
    # МЕТОДЫ НАСТРОЕК
    # ========================================================================

    def get_settings(self, chat_id: int) -> Dict[str, Any]:
        """Получает настройки пользователя. Приоритет: файл > БД > дефолтные"""
        default_settings = {
            "date_from": "",
            "date_to": "",
            "price_from": 0,
            "price_to": 1_000_000_000,
            "notifications_enabled": True,
            "search_mode": "updates",
        }
        user_dir = USERS_DIR / str(chat_id)
        settings_file = user_dir / "settings.json"

        if settings_file.exists():
            try:
                with open(settings_file, "r", encoding="utf-8") as f:
                    return {**default_settings, **json.load(f)}
            except Exception as e:
                logger.warning(f"⚠️ Ошибка чтения {settings_file}: {e}")

        user = self.get_user(chat_id)
        if user and user.get("settings_json"):
            try:
                return {**default_settings, **json.loads(user["settings_json"])}
            except Exception:
                pass
        return default_settings.copy()

    def update_settings(self, chat_id: int, settings: Dict[str, Any]) -> bool:
        """Обновляет настройки пользователя (файл + БД)"""
        try:
            settings_json = json.dumps(settings, ensure_ascii=False)
            with sqlite3.connect(self.DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE users SET settings_json = ? WHERE chat_id = ?",
                    (settings_json, chat_id),
                )
                conn.commit()
            user_dir = USERS_DIR / str(chat_id)
            user_dir.mkdir(parents=True, exist_ok=True)
            with open(user_dir / "settings.json", "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обновления настроек {chat_id}: {e}")
            return False

    def update_user(self, chat_id: int, **kwargs) -> bool:
        """Обновляет произвольные поля пользователя"""
        try:
            with sqlite3.connect(self.DB_PATH) as conn:
                cursor = conn.cursor()
                fields, values = [], []
                for key, value in kwargs.items():
                    if key == "settings_json" and isinstance(value, dict):
                        value = json.dumps(value, ensure_ascii=False)
                    fields.append(f"{key} = ?")
                    values.append(value)
                if not fields:
                    return True
                values.append(chat_id)
                cursor.execute(
                    f"UPDATE users SET {', '.join(fields)} WHERE chat_id = ?", values
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"❌ Ошибка обновления пользователя {chat_id}: {e}")
            return False


# ============================================================================
# НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ (удобный интерфейс)
# ============================================================================


class UserSettings:
    """Удобный интерфейс для работы с настройками конкретного пользователя"""

    def __init__(self, chat_id: int, db: UserDatabase):
        self.chat_id = chat_id
        self.db = db
        self._user_dir = USERS_DIR / str(chat_id)
        self._user_dir.mkdir(parents=True, exist_ok=True)
        self._refresh()

    def _refresh(self):
        """Перезагружает настройки из БД/файла"""
        self._settings = self.db.get_settings(self.chat_id)

    # ========================================================================
    # СВОЙСТВА ДЛЯ ПАРСЕРА
    # ========================================================================

    @property
    def have_file(self) -> Path:
        """Путь к файлу have.txt"""
        return self._user_dir / "have.txt"

    @property
    def excel_file(self) -> Path:
        """Путь к Excel-файлу с результатами"""
        return self._user_dir / "reports.xlsx"

    @property
    def cache_file(self) -> Path:
        """Путь к файлу кэша парсера"""
        return self._user_dir / "cache.json"

    @property
    def is_configured(self) -> bool:
        """Проверяет, настроены ли обязательные параметры"""
        return bool(self._settings.get("date_from"))

    # ========================================================================
    # МЕТОДЫ ДОСТУПА К НАСТРОЙКАМ
    # ========================================================================

    def get(self, key: Optional[str] = None) -> Union[Any, Dict[str, Any]]:
        """Получает настройку по ключу или все настройки"""
        if key:
            return self._settings.get(key)
        return self._settings.copy()

    def set(self, **kwargs) -> bool:
        """Обновляет настройки и сохраняет в БД + файл"""
        self._settings.update(kwargs)
        return self.db.update_settings(self.chat_id, self._settings)

    def reset(self) -> Dict[str, Any]:
        """Сбрасывает настройки к дефолтным"""
        self._settings = {
            "date_from": "",
            "date_to": "",
            "price_from": 0,
            "price_to": 1_000_000_000,
            "notifications_enabled": True,
            "search_mode": "updates",
        }
        self.db.update_settings(self.chat_id, self._settings)
        return self._settings.copy()

    # ========================================================================
    # МЕТОДЫ ДЛЯ ПАРСЕРА
    # ========================================================================

    def get_search_params(self) -> Dict[str, str]:
        """Возвращает параметры для построения поискового URL"""
        from datetime import datetime

        today = datetime.now().strftime("%d.%m.%Y")

        # Только режим updates — ручной ввод (range) удалён
        return {
            "search-filter": "updateDate",
            "publishDateFrom": self._settings.get("date_from", ""),
            "publishDateTo": "",
            "updateDateFrom": today,
            "updateDateTo": "",
            "fz44": "on",
            "fz223": "on",
            "pc": "on",
            "pa": "on",
            "currencyIdGeneral": "-1",
            "morphology": "on",
            "sortBy": "UPDATE_DATE",
            "sortDirection": "false",
            "recordsPerPage": "_10",
            "showLotsInfoHidden": "false",
            "priceFromGeneral": str(self._settings.get("price_from", 0)),
            "priceToGeneral": str(self._settings.get("price_to", 1_000_000_000)),
        }

    def get_strategy(self):
        """Возвращает стратегию поиска на основе настроек (заглушка)"""
        # Стратегии удалены — URL строится напрямую в parser.py
        return None


# ============================================================================
# ГЛОБАЛЬНЫЕ ЭКЗЕМПЛЯРЫ И ФАБРИКИ
# ============================================================================

# Глобальный экземпляр БД (создаётся при импорте модуля)
user_db = UserDatabase()


def get_user_settings(chat_id: int) -> UserSettings:
    """Фабрика для получения настроек пользователя"""
    return UserSettings(chat_id, user_db)


def reset_db_for_tests():
    """Сбрасывает БД для тестов — только для тестов!"""
    if UserDatabase.DB_PATH.exists():
        UserDatabase.DB_PATH.unlink()
    global user_db
    user_db = UserDatabase()

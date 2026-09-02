"""
Кэширование найденных отказов для избежания дубликатов.
Добавлено: supplier_name, email, email_sent, email_status
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List, Any
from dataclasses import dataclass, asdict, field
from loguru import logger


@dataclass
class RejectionState:
    """Состояние отказа для кэширования"""

    reestr_number: str
    contract_number: str
    reject_date: str  # Дата отказа
    reject_url: str
    first_found_date: str  # Когда впервые найден
    last_check: str  # ISO формат
    # Новые поля для email
    supplier_name: str = ""  # Название поставщика
    customer_name: str = ""
    email: str = ""  # Email поставщика
    email_sent: bool = False  # Отправлено ли письмо
    email_status: str = ""  # Статус отправки (sent, error, blacklist и т.д.)
    email_sent_at: str = ""  # Когда отправлено письмо (ISO)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RejectionState":
        # Обратная совместимость со старыми записями без новых полей
        return cls(
            reestr_number=data["reestr_number"],
            contract_number=data["contract_number"],
            reject_date=data["reject_date"],
            reject_url=data["reject_url"],
            first_found_date=data["first_found_date"],
            last_check=data["last_check"],
            supplier_name=data.get("supplier_name", ""),
            email=data.get("email", ""),
            email_sent=data.get("email_sent", False),
            email_status=data.get("email_status", ""),
            email_sent_at=data.get("email_sent_at", ""),
        )


class RejectionsCache:
    """
    Потокобезопасный кэш отказов.
    """

    DEFAULT_TTL_DAYS = 90
    SAVE_DELAY_SECONDS = 5

    def __init__(self, cache_file: Path, ttl_days: int = DEFAULT_TTL_DAYS):
        self.cache_file = Path(cache_file)
        self.ttl_days = ttl_days

        self._data: Dict[str, RejectionState] = {}  # reestr_number -> state
        self._lock = threading.RLock()
        self._pending_save = False
        self._last_save_time = 0
        self._today_count = 0
        self._session_count = 0

        self._load()
        self._check_and_reset_if_new_day()

    def _get_today_str(self) -> str:
        return datetime.now().strftime("%d.%m.%Y")

    def _check_and_reset_if_new_day(self):
        """Сбрасывает счётчик 'сегодня' при смене даты"""
        current_date = self._get_today_str()
        if hasattr(self, "_cache_date") and self._cache_date != current_date:
            self._today_count = 0
            logger.info(f"📅 Новый день! Счётчик 'сегодня' сброшен")
        self._cache_date = current_date

    def _load(self):
        """Загружает кэш из файла"""
        if not self.cache_file.exists():
            logger.info(f"📂 Новый кэш отказов: {self.cache_file}")
            self._data = {}
            return

        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Проверяем структуру
            if "rejections" in data:
                self._data = {
                    k: RejectionState.from_dict(v)
                    for k, v in data["rejections"].items()
                }
                self._today_count = data.get("stats", {}).get("total_found", 0)
            else:
                self._data = {}

            self._check_and_reset_if_new_day()

            logger.info(f"📂 Кэш отказов загружен: {len(self._data)} записей")

        except Exception as e:
            logger.error(f"❌ Ошибка загрузки кэша отказов: {e}")
            self._data = {}

    def _save(self):
        """Сохраняет кэш в файл"""
        with self._lock:
            try:
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)

                data = {
                    "rejections": {k: v.to_dict() for k, v in self._data.items()},
                    "stats": {
                        "total_found": len(self._data),
                        "today_found": self._today_count,
                        "last_updated": datetime.now().isoformat(),
                    },
                    "date": self._get_today_str(),
                }

                temp_file = self.cache_file.with_suffix(".tmp")
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

                temp_file.replace(self.cache_file)
                self._last_save_time = time.time()
                self._pending_save = False

                logger.debug(f"💾 Кэш отказов сохранён: {len(self._data)} записей")

            except Exception as e:
                logger.error(f"❌ Ошибка сохранения кэша: {e}")

    def _schedule_save(self):
        """Планирует отложенное сохранение"""
        if not self._pending_save:
            self._pending_save = True
            threading.Timer(self.SAVE_DELAY_SECONDS, self._save).start()

    def is_processed(self, reestr_number: str) -> bool:
        """Проверяет, обрабатывался ли уже этот контракт"""
        with self._lock:
            self._check_and_reset_if_new_day()
            return reestr_number in self._data

    def add(
        self,
        reestr_number: str,
        contract_number: str,
        reject_date: str,
        reject_url: str,
        supplier_name: str = "",
        customer_name: str = "",
        email: str = "",
        email_sent: bool = False,
        email_status: str = "",
    ) -> bool:
        """
        Добавляет отказ в кэш.
        Возвращает True если это новый отказ, False если уже был.
        """
        with self._lock:
            self._check_and_reset_if_new_day()

            is_new = reestr_number not in self._data
            now_iso = datetime.now().isoformat()

            if is_new:
                # Новый отказ
                self._data[reestr_number] = RejectionState(
                    reestr_number=reestr_number,
                    contract_number=contract_number,
                    reject_date=reject_date,
                    reject_url=reject_url,
                    first_found_date=self._get_today_str(),
                    last_check=now_iso,
                    supplier_name=supplier_name,
                    email=email,
                    email_sent=email_sent,
                    email_status=email_status,
                    email_sent_at=now_iso if email_sent else "",
                )
                self._today_count += 1
                self._session_count += 1
                logger.info(f"🆕 Новый отказ в кэше: {contract_number}")
            else:
                # Существующий — обновляем last_check и email-данные если нужно
                existing = self._data[reestr_number]
                existing.last_check = now_iso
                # Обновляем email-данные если они были пустыми
                if supplier_name and not existing.supplier_name:
                    existing.supplier_name = supplier_name
                if email and not existing.email:
                    existing.email = email
                if email_sent and not existing.email_sent:
                    existing.email_sent = email_sent
                    existing.email_status = email_status
                    existing.email_sent_at = now_iso
                logger.debug(f"⏭ Отказ {contract_number} уже в кэше")

            self._schedule_save()
            return is_new

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику"""
        with self._lock:
            self._check_and_reset_if_new_day()
            return {
                "total_rejections": len(self._data),
                "today": self._today_count,
                "session": self._session_count,
            }

    def get_all_rejections(self) -> List[Dict[str, Any]]:
        """Возвращает все отказы как список dict"""
        with self._lock:
            return [v.to_dict() for v in self._data.values()]

    def clear(self):
        """Очищает кэш"""
        with self._lock:
            self._data.clear()
            self._today_count = 0
            self._save()
        logger.info("🗑 Кэш отказов очищен")

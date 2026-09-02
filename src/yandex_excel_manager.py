"""
Менеджер для работы с Excel-файлами на Яндекс.Диске для парсера отказов.
Версия для заказчицы Арины Еремян (rnp-help.ru).
Новые колонки: Реестровый номер, Ссылка на контракт, Цена, Регион, Заказчик,
Поставщик, ИНН, Телефон, Почта, Дата заключения, Дата решения о расторжении.
"""

import pandas as pd
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, date, timedelta
from loguru import logger
import asyncio
import threading
import pytz
import json
import time

from config.settings import settings


class YandexExcelManager:
    """Управление Excel-файлом на Яндекс.Диске с немедленным сохранением"""

    COLUMNS = [
        "№ п/п",
        "Реестровый номер контракта",
        "Ссылка на контракт",
        "Цена контракта",
        "Регион Заказчика",
        "Наименование Заказчика",
        "Наименование поставщика",
        "ИНН Поставщика",
        "Телефон Поставщика",
        "Почта поставщика",
        "Дата заключения контракта",
        "Дата размещения решения о расторжении",
    ]

    def __init__(self, yandex_service, chat_id: int, local_temp_dir: Path):
        self.yandex = yandex_service
        self.chat_id = chat_id
        self.temp_dir = Path(local_temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self.timezone = pytz.timezone(settings.timezone)
        self.current_date = self._get_current_date()

        self._df: Optional[pd.DataFrame] = None
        self._local_file: Optional[Path] = None
        self._initialized = False
        self._lock = threading.Lock()
        self._pending_saves: int = 0

        self._rejections_cache_file = Path(
            f"data/users/{chat_id}/rejections_cache.json"
        )

        # Таймер сохранения
        self._save_timer: Optional[asyncio.TimerHandle] = None
        self._save_interval_sec = 60
        self._last_save_time: Optional[float] = None

    def _get_current_date(self) -> date:
        """Возвращает актуальную дату с учётом часового пояса"""
        return datetime.now(self.timezone).date()

    def _get_filename(self, file_date: date) -> str:
        """Возвращает имя файла для даты"""
        return f"rejections_{file_date.strftime('%d.%m.%Y')}.xlsx"

    def _get_remote_path(self, file_date: date, subfolder: str = None) -> str:
        """Возвращает путь на Яндекс.Диске (всегда в папку админа)"""
        filename = self._get_filename(file_date)
        admin_vk_id = getattr(settings, "admin_vk_id", str(self.chat_id))
        base = f"{self.yandex.BASE_FOLDER}/{admin_vk_id}"
        if subfolder:
            return f"{base}/{subfolder}/{filename}"
        return f"{base}/{filename}"

    def _reset_rejections_cache(self):
        """Сбрасывает кэш отказов при смене даты"""
        try:
            if self._rejections_cache_file.exists():
                today = date.today().isoformat()
                empty_cache = {
                    "rejections": {},
                    "stats": {
                        "total_found": 0,
                        "today_found": 0,
                        "last_updated": datetime.now().isoformat(),
                    },
                    "date": today,
                }
                self._rejections_cache_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._rejections_cache_file, "w", encoding="utf-8") as f:
                    json.dump(empty_cache, f, indent=2, ensure_ascii=False)
                logger.info(
                    f"🗑️ Кэш отказов сброшен при смене даты: {self._rejections_cache_file}"
                )
        except Exception as e:
            logger.error(f"❌ Ошибка сброса кэша отказов: {e}")

    def _check_date_change(self) -> bool:
        """Проверяет смену даты. При смене — новый Excel и сброс кэша."""
        actual_date = self._get_current_date()
        if actual_date != self.current_date:
            logger.info(f"📅 Смена даты: {self.current_date} -> {actual_date}")
            self.current_date = actual_date
            self._df = pd.DataFrame(columns=self.COLUMNS)
            self._local_file = None
            self._reset_rejections_cache()
            logger.info(f"✅ Создан новый Excel и сброшен кэш для {actual_date}")
            return True
        return False

    async def initialize(self):
        """Асинхронная инициализация. ВЫЗЫВАТЬ ЯВНО перед использованием!"""
        if self._initialized:
            return

        try:
            self._check_date_change()
            await self._archive_if_needed()
            await self._load_from_yandex()
            self._initialized = True
            logger.info(
                f"✅ ExcelManager инициализирован для {self.chat_id} (дата: {self.current_date})"
            )
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации: {e}")
            self._df = pd.DataFrame(columns=self.COLUMNS)
            self._initialized = True

    async def _archive_if_needed(self):
        """Архивирует вчерашний файл"""
        try:
            yesterday = self.current_date - timedelta(days=1)
            old_remote = self._get_remote_path(yesterday)
            archive_remote = self._get_remote_path(yesterday, "archive")

            if await self.yandex.exists(old_remote):
                archive_folder = f"{self.yandex.BASE_FOLDER}/{self.chat_id}/archive"
                if not await self.yandex.exists(archive_folder):
                    await self.yandex.mkdir(archive_folder)

                await self.yandex.move(old_remote, archive_remote)
                logger.info(
                    f"📦 Архивирован вчерашний файл: {self._get_filename(yesterday)}"
                )

        except Exception as e:
            logger.error(f"❌ Ошибка архивации: {e}")

    async def _load_from_yandex(self):
        """Загружает существующий файл с Диска или создаёт новый"""
        remote_path = self._get_remote_path(self.current_date)
        self._local_file = self.temp_dir / self._get_filename(self.current_date)

        try:
            if await self.yandex.exists(remote_path):
                logger.info(
                    f"📥 Скачиваем с Диска: {self._get_filename(self.current_date)}"
                )

                success = await self.yandex.download(remote_path, str(self._local_file))

                if success and self._local_file.exists():
                    self._df = pd.read_excel(self._local_file, engine="openpyxl")

                    if self._validate_columns(self._df):
                        logger.info(f"📂 Загружено {len(self._df)} записей")
                        self._filter_only_today_records()
                        return
                    else:
                        logger.warning("⚠️ Некорректные колонки, создаём новый")

            self._df = pd.DataFrame(columns=self.COLUMNS)
            logger.info(f"✅ Создан новый Excel для {self.current_date}")

        except Exception as e:
            logger.error(f"❌ Ошибка загрузки с Диска: {e}")
            self._df = pd.DataFrame(columns=self.COLUMNS)

    def _filter_only_today_records(self):
        """Фильтрует записи, оставляя только за сегодняшний день"""
        if self._df is None or self._df.empty:
            return

        try:
            today_str = self.current_date.strftime("%d.%m.%Y")
            original_count = len(self._df)

            if "Дата размещения решения о расторжении" in self._df.columns:
                mask = (
                    self._df["Дата размещения решения о расторжении"]
                    .astype(str)
                    .str.contains(today_str, na=False)
                )
                self._df = self._df[mask].copy()
                filtered_count = len(self._df)

                if filtered_count < original_count:
                    logger.warning(
                        f"⚠️ Отфильтрованы старые записи: {original_count} -> {filtered_count} "
                        f"(только {today_str})"
                    )
                    self._renumber_rows()
        except Exception as e:
            logger.error(f"❌ Ошибка фильтрации записей: {e}")

    def _validate_columns(self, df: pd.DataFrame) -> bool:
        """Проверяет наличие всех необходимых колонок"""
        missing = set(self.COLUMNS) - set(df.columns)
        if missing:
            logger.error(f"❌ Отсутствуют колонки: {missing}")
            return False
        return True

    def _renumber_rows(self):
        """Перенумеровывает строки"""
        if self._df is None or self._df.empty:
            return
        self._df["№ п/п"] = range(1, len(self._df) + 1)

    def _is_duplicate(self, contract_url: str) -> bool:
        """Проверяет дубликат по URL контракта"""
        if not contract_url or self._df is None or self._df.empty:
            return False

        if "Ссылка на контракт" in self._df.columns:
            return contract_url in self._df["Ссылка на контракт"].values
        return False

    async def save_to_yandex(self) -> Optional[str]:
        """Сохраняет DataFrame на Яндекс.Диск. Возвращает короткую публичную ссылку.
        Перед сохранением скачивает существующий файл и объединяет записи."""
        try:
            with self._lock:
                if self._df is None or self._df.empty:
                    logger.debug("⚠️ Нет данных для сохранения")
                    return None

                date_changed = self._check_date_change()
                if date_changed:
                    await self._archive_if_needed()

                self._local_file = self.temp_dir / self._get_filename(self.current_date)

                # Скачиваем существующий файл с Диска
                admin_vk_id = int(getattr(settings, "admin_vk_id", self.chat_id))
                remote_path = self._get_remote_path(self.current_date)

                existing_df = None
                try:
                    if await self.yandex.exists(remote_path):
                        temp_existing = (
                            self.temp_dir
                            / f"existing_{self._get_filename(self.current_date)}"
                        )
                        if await self.yandex.download(remote_path, str(temp_existing)):
                            existing_df = pd.read_excel(
                                temp_existing, engine="openpyxl"
                            )
                            if self._validate_columns(existing_df):
                                logger.info(
                                    f"📥 Загружен существующий файл: {len(existing_df)} записей"
                                )
                            else:
                                logger.warning(
                                    f"⚠️ Старый файл имеет неверные колонки {set(existing_df.columns)}. "
                                    f"Пересоздаём с правильными: {set(self.COLUMNS)}"
                                )
                                existing_df = None
                                try:
                                    await self.yandex.delete(remote_path)
                                    logger.info(f"🗑️ Старый файл удалён: {remote_path}")
                                except Exception as del_e:
                                    logger.warning(
                                        f"⚠️ Не удалось удалить старый файл: {del_e}"
                                    )
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось скачать существующий файл: {e}")

                # Объединяем с учётом дубликатов
                if existing_df is not None and not existing_df.empty:
                    combined = pd.concat([existing_df, self._df], ignore_index=True)
                    combined = combined.drop_duplicates(
                        subset=["Ссылка на контракт"], keep="first"
                    )
                    self._df = combined.copy()
                    logger.info(
                        f"🔄 Объединено: {len(existing_df)} старых + "
                        f"{len(self._df) - len(existing_df)} новых = {len(self._df)} записей"
                    )

                self._renumber_rows()
                self._df.to_excel(self._local_file, index=False, engine="openpyxl")
                self._pending_saves += 1

                # Загрузка на Диск
                public_url = await self.yandex.upload_for_user(
                    admin_vk_id, self._local_file, remote_subfolder=None
                )

                self._pending_saves = max(0, self._pending_saves - 1)
                self._last_save_time = time.time()

                if public_url:
                    short_url = self._convert_to_short_url(public_url)
                    logger.info(
                        f"✅ Сохранено на Диск: {self._get_filename(self.current_date)} "
                        f"({len(self._df)} записей)"
                    )
                    logger.info(f"🔗 Короткая ссылка: {short_url}")
                    return short_url
                else:
                    logger.error(
                        f"❌ Не удалось получить ссылку на файл после загрузки"
                    )
                    return None

        except Exception as e:
            with self._lock:
                self._pending_saves = max(0, self._pending_saves - 1)
            logger.error(f"❌ Ошибка сохранения на Диск: {e}")
            return None

    def _convert_to_short_url(self, long_url: str) -> str:
        """Преобразует длинную ссылку Яндекс.Диска в короткую yadi.sk/d/XXXX"""
        try:
            if "yadi.sk/d/" in long_url:
                return long_url

            import re

            match = re.search(r"/d/([a-zA-Z0-9_-]+)", long_url)
            if match:
                public_key = match.group(1)
                return f"https://yadi.sk/d/{public_key}"

            if "disk.yandex.ru/client" in long_url:
                logger.warning(
                    f"⚠️ Ссылка не публичная (client URL): {long_url[:50]}..."
                )

            return long_url

        except Exception as e:
            logger.error(f"❌ Ошибка преобразования ссылки: {e}")
            return long_url

    def _schedule_timer_save(self):
        """Запускает/перезапускает таймер сохранения на 60 секунд."""
        try:
            if self._save_timer is not None:
                try:
                    self._save_timer.cancel()
                except Exception:
                    pass
                self._save_timer = None

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("⏱️ Вызов из потока — таймер не запущен")
                return

            self._save_timer = loop.call_later(
                self._save_interval_sec,
                lambda: asyncio.create_task(self._timer_save_callback()),
            )
            logger.debug(f"⏱️ Таймер сохранения запущен: {self._save_interval_sec}с")

        except Exception as e:
            logger.debug(f"⏱️ Ошибка запуска таймера: {e}")

    async def _timer_save_callback(self):
        """Callback таймера — принудительное сохранение"""
        logger.info(f"⏱️ Таймер сработал! Принудительное сохранение на Я.Диск...")
        await self.force_save()
        self._save_timer = None

    def add_record(self, record: Dict[str, Any]) -> bool:
        """
        Добавляет запись отказа и планирует сохранение.
        Синхронный метод — не блокирует.
        """
        try:
            if self._check_date_change():
                logger.info("📅 Новый день! Создан новый Excel и сброшен кэш.")

            reestr_number = record.get("Реестровый номер контракта", "")
            contract_url = record.get("Ссылка на контракт", "")
            price = record.get("Цена контракта", "")
            region = record.get("Регион Заказчика", "")
            customer = record.get("Наименование Заказчика", "")
            supplier = record.get("Наименование поставщика", "")
            inn = record.get("ИНН Поставщика", "")
            phone = record.get("Телефон Поставщика", "")
            email = record.get("Почта поставщика", "")
            contract_date = record.get("Дата заключения контракта", "")
            reject_date = record.get("Дата размещения решения о расторжении", "")

            if not all([reestr_number, contract_url, reject_date]):
                logger.warning(f"  ⚠️ Не все обязательные поля заполнены")
                return False

            today_str = self.current_date.strftime("%d.%m.%Y")
            if today_str not in reject_date:
                logger.warning(f"  ⚠️ Дата отказа не совпала! Пропускаем.")
                return False

            if self._is_duplicate(contract_url):
                logger.warning(f"  ⚠️ ДУБЛИКАТ найден: {contract_url[:40]}...")
                return False

            new_record = {
                "№ п/п": None,
                "Реестровый номер контракта": reestr_number,
                "Ссылка на контракт": contract_url,
                "Цена контракта": price,
                "Регион Заказчика": region,
                "Наименование Заказчика": customer,
                "Наименование поставщика": supplier,
                "ИНН Поставщика": inn,
                "Телефон Поставщика": phone,
                "Почта поставщика": email,
                "Дата заключения контракта": contract_date,
                "Дата размещения решения о расторжении": reject_date,
            }

            with self._lock:
                new_row = pd.DataFrame([new_record])
                self._df = pd.concat([self._df, new_row], ignore_index=True)
                assigned_number = len(self._df)

            logger.info(
                f"✅ Добавлен отказ №{assigned_number} | "
                f"Контракт: {reestr_number} | "
                f"Заказчик: {customer[:30] if customer else 'не указан'}... | "
                f"Поставщик: {supplier[:30] if supplier else 'не указан'}... | "
                f"Email: {email or 'не найден'} | "
                f"Дата отказа: {reject_date}"
            )

            self._schedule_timer_save()
            return True

        except Exception as e:
            logger.error(f"  ❌ Ошибка в add_record: {e}")
            import traceback

            logger.error(f"  ❌ Трейс: {traceback.format_exc()}")
            return False

    async def force_save(self) -> Optional[str]:
        """Принудительное немедленное сохранение. Возвращает публичную ссылку."""
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        return await self.save_to_yandex()

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику"""
        try:
            self._check_date_change()

            if self._df is None:
                return {"total": 0, "today": 0, "file": "unknown", "public_url": None}

            today_str = self.current_date.strftime("%d.%m.%Y")
            filename = self._get_filename(self.current_date)

            count_today = 0
            if "Дата размещения решения о расторжении" in self._df.columns:
                mask = (
                    self._df["Дата размещения решения о расторжении"]
                    .astype(str)
                    .str.contains(today_str, na=False)
                )
                count_today = self._df[mask].shape[0]

            return {
                "total": len(self._df),
                "today": count_today,
                "file": filename,
                "pending_saves": self._pending_saves,
                "admin_vk_id": int(getattr(settings, "admin_vk_id", self.chat_id)),
                "current_date": self.current_date.isoformat(),
            }
        except Exception as e:
            logger.error(f"❌ Ошибка статистики: {e}")
            return {"total": 0, "today": 0, "file": "unknown", "error": str(e)}

    def get_current_file(self) -> Path:
        """Возвращает путь к локальному временному файлу"""
        if self._local_file:
            return self._local_file
        return self.temp_dir / self._get_filename(self.current_date)

    def get_dataframe(self) -> pd.DataFrame:
        """Возвращает копию DataFrame"""
        if self._df is None:
            return pd.DataFrame(columns=self.COLUMNS)
        return self._df.copy()

    async def close(self) -> Optional[str]:
        """Закрывает менеджер, принудительно сохраняя данные. Возвращает публичную ссылку."""
        if self._pending_saves > 0 or (self._df is not None and len(self._df) > 0):
            logger.info("💾 Принудительное сохранение при закрытии...")
            return await self.force_save()
        return None

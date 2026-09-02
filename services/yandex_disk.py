"""
Сервис для работы с Яндекс.Диском.
Версия: Автоматическая архивация при смене дня, без папки backup.
"""

import yadisk
from pathlib import Path
from typing import Optional, List
from datetime import datetime, date, timedelta
from loguru import logger
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import time
from dotenv import load_dotenv

load_dotenv()

_executor = ThreadPoolExecutor(max_workers=3)


def _run_sync(func, *args, **kwargs):
    """Запускает синхронную функцию yadisk в отдельном потоке"""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


class YandexDiskService:
    """Сервис для работы с Яндекс.Диском с автоматической ротацией файлов"""

    BASE_FOLDER = "/parser_users"

    def __init__(self, token: str):
        self.token = token
        self.client = yadisk.YaDisk(token=token)
        self._last_check_date: Optional[date] = None
        self._folder_cache: set = set()  # Кэш созданных папок

    async def check_token(self) -> bool:
        """Проверяет валидность токена"""
        try:
            return await _run_sync(self.client.check_token)
        except Exception as e:
            logger.error(f"Ошибка проверки токена: {e}")
            return False

    async def get_free_space(self) -> dict:
        """Возвращает свободное место на диске"""
        try:
            disk_info = await _run_sync(self.client.get_disk_info)

            total_bytes = getattr(disk_info, "total_space", 0)
            used_bytes = getattr(disk_info, "used_space", 0)
            free_bytes = total_bytes - used_bytes

            return {
                "free_bytes": free_bytes,
                "total_bytes": total_bytes,
                "used_bytes": used_bytes,
                "free_gb": free_bytes / (1024**3),
                "total_gb": total_bytes / (1024**3),
            }

        except Exception as e:
            logger.error(f"❌ Ошибка получения свободного места: {e}")
            return {
                "free_bytes": 0,
                "total_bytes": 0,
                "used_bytes": 0,
                "free_gb": 0,
                "total_gb": 0,
            }

    async def exists(self, remote_path: str) -> bool:
        """АСИНХРОННАЯ проверка существования"""
        try:
            return await _run_sync(self.client.exists, remote_path)
        except Exception:
            return False

    async def move(self, src: str, dst: str) -> bool:
        """Перемещает файл на Диске"""
        try:
            await _run_sync(self.client.move, src, dst)
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка перемещения {src} -> {dst}: {e}")
            return False

    async def mkdir(self, path: str) -> bool:
        """
        Создаёт папку на Диске.
        Игнорирует ошибку 409 (папка уже существует).
        """
        if path in self._folder_cache:
            return True

        try:
            await _run_sync(self.client.mkdir, path)
            self._folder_cache.add(path)
            logger.info(f"📁 Создана папка: {path}")
            return True
        except yadisk.exceptions.PathExistsError:
            self._folder_cache.add(path)
            return True
        except Exception as e:
            if await self.exists(path):
                self._folder_cache.add(path)
                return True
            logger.error(f"❌ Ошибка создания папки {path}: {e}")
            return False

    async def mkdirs(self, path: str) -> bool:
        """
        Создаёт папку и все родительские папки рекурсивно.
        """
        if path in self._folder_cache:
            return True

        parts = path.replace("disk:/", "").strip("/").split("/")
        current_path = "disk:/"

        for part in parts:
            if not part:
                continue
            current_path = f"{current_path}{part}/"

            if current_path.rstrip("/") in self._folder_cache:
                continue

            success = await self.mkdir(current_path.rstrip("/"))
            if not success:
                return False

        return True

    async def download(self, remote_path: str, local_path: str) -> bool:
        """Скачивает файл с Диска"""
        try:
            await _run_sync(self.client.download, remote_path, local_path)
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка скачивания {remote_path}: {e}")
            return False

    async def _archive_yesterday_if_exists(self, chat_id: int, today_date: date):
        """Проверяет и архивирует вчерашний файл на Диске"""
        yesterday = today_date - timedelta(days=1)
        yesterday_filename = f"reports_{yesterday.strftime('%d.%m.%Y')}.xlsx"

        remote_yesterday = f"{self.BASE_FOLDER}/{chat_id}/{yesterday_filename}"
        remote_archive = f"{self.BASE_FOLDER}/{chat_id}/archive/{yesterday_filename}"

        if await self.exists(remote_yesterday):
            archive_folder = f"{self.BASE_FOLDER}/{chat_id}/archive"
            await self.mkdirs(archive_folder)

            success = await self.move(remote_yesterday, remote_archive)
            if success:
                logger.info(f"📦 Архивирован вчерашний файл: {yesterday_filename}")

        await self._cleanup_old_archives(chat_id, keep_days=30)

    async def _cleanup_old_archives(self, chat_id: int, keep_days: int = 30):
        """Удаляет архивы старше keep_days дней"""
        try:
            archive_folder = f"{self.BASE_FOLDER}/{chat_id}/archive"
            if not await self.exists(archive_folder):
                return

            files = await _run_sync(self.client.listdir, archive_folder)
            cutoff_date = datetime.now() - timedelta(days=keep_days)

            deleted_count = 0
            for file in files:
                if file.type != "file":
                    continue
                try:
                    date_str = file.name.replace("reports_", "").replace(".xlsx", "")
                    file_date = datetime.strptime(date_str, "%d.%m.%Y")

                    if file_date < cutoff_date:
                        await _run_sync(self.client.remove, file.path)
                        deleted_count += 1
                        logger.debug(f"🗑 Удалён старый архив: {file.name}")
                except ValueError:
                    continue

            if deleted_count > 0:
                logger.info(f"🧹 Очищено старых архивов: {deleted_count}")

        except Exception as e:
            logger.warning(f"⚠️ Ошибка очистки архивов: {e}")

    async def upload_for_user(
        self, chat_id: int, local_file: Path, remote_subfolder: str = None
    ) -> str:
        """Загружает файл для пользователя с автоматической ротацией"""
        file_name = local_file.name

        try:
            date_str = file_name.replace("reports_", "").replace(".xlsx", "")
            file_date = datetime.strptime(date_str, "%d.%m.%Y").date()
        except ValueError:
            file_date = datetime.now().date()

        today = datetime.now().date()

        if file_date == today and not remote_subfolder:
            if self._last_check_date != today:
                await self._archive_yesterday_if_exists(chat_id, today)
                self._last_check_date = today

        if remote_subfolder:
            remote_path = f"{self.BASE_FOLDER}/{chat_id}/{remote_subfolder}/{file_name}"
            folder_path = f"{self.BASE_FOLDER}/{chat_id}/{remote_subfolder}"
        else:
            remote_path = f"{self.BASE_FOLDER}/{chat_id}/{file_name}"
            folder_path = f"{self.BASE_FOLDER}/{chat_id}"

        await self.mkdirs(folder_path)

        public_url = await self.upload_file(local_file, remote_path, make_public=True)

        if not public_url:
            public_url = f"https://disk.yandex.ru/client/disk/parser_users/{chat_id}"
            logger.warning(f"⚠️ Public URL не получен. Файл доступен по: {public_url}")
            logger.info(f"📂 Реальный путь на Диске: {remote_path}")

        logger.info(f"✅ Файл загружен: {file_name}")
        return public_url

    async def upload_file(
        self, local_file: Path, remote_path: str, make_public: bool = True
    ) -> Optional[str]:
        """Загружает файл на Яндекс.Диск и возвращает публичную ссылку.
        При ошибке 423 (Resource locked) удаляет старый файл и повторяет."""

        # === СОЗДАЁМ ПАПКИ ПЕРЕД ЗАГРУЗКОЙ ===
        folder_path = str(Path(remote_path).parent).replace("\\", "/")
        if folder_path and folder_path != "disk:":
            await self.mkdirs(folder_path)

        # === УДАЛЯЕМ СТАРЫЙ ФАЙЛ, ЕСЛИ СУЩЕСТВУЕТ (чтобы избежать 423) ===
        if await self.exists(remote_path):
            try:
                await _run_sync(self.client.remove, remote_path)
                logger.info(f"🗑️ Старый файл удалён перед загрузкой: {remote_path}")
                await asyncio.sleep(1)  # Пауза на освобождение блокировки
            except Exception as e:
                logger.warning(f"⚠️ Не удалось удалить старый файл: {e}")

        # === ЗАГРУЗКА С RETRY ===
        upload_success = False
        for attempt in range(3):
            try:
                logger.info(
                    f"📤 Загрузка файла (попытка {attempt+1}/3): {local_file.name} → {remote_path}"
                )
                await _run_sync(
                    self.client.upload, str(local_file), remote_path, overwrite=True
                )
                logger.info(f"✅ Файл загружен на Диск: {remote_path}")
                upload_success = True
                break
            except Exception as e:
                error_str = str(e)
                if (
                    "423" in error_str
                    or "locked" in error_str.lower()
                    or "Resource is locked" in error_str
                ):
                    wait = 2 * (attempt + 1)
                    logger.warning(
                        f"⏳ Файл заблокирован (423), ждём {wait}с... (попытка {attempt+1}/3)"
                    )
                    time.sleep(wait)
                    if attempt == 2:
                        logger.error(f"❌ Ошибка загрузки файла после 3 попыток: {e}")
                        return None
                else:
                    logger.error(f"❌ Ошибка загрузки файла: {e}")
                    return None


        if not upload_success:
            return None

        # === ПУБЛИЧНАЯ ССЫЛКА ===
        if make_public:
            try:
                await _run_sync(self.client.publish, remote_path)
                logger.info(f"📢 Файл опубликован: {remote_path}")

                for attempt in range(5):
                    public_info = await _run_sync(self.client.get_meta, remote_path)

                    public_url = None
                    if hasattr(public_info, "public_url") and public_info.public_url:
                        public_url = public_info.public_url
                    elif hasattr(public_info, "public_key") and public_info.public_key:
                        public_url = (
                            f"https://disk.yandex.ru/i/{public_info.public_key}"
                        )

                    if public_url:
                        logger.info(f"🔗 Публичная ссылка: {public_url}")
                        return public_url

                    logger.debug(f"⏳ Ожидание public_url... попытка {attempt+1}/5")
                    time.sleep(0.5)

                logger.warning(
                    "⚠️ Файл опубликован, но public_url не получен после 5 попыток"
                )

            except Exception as e:
                logger.warning(f"⚠️ Не удалось создать публичную ссылку: {e}")

        return None

    async def download_for_user(
        self, chat_id: int, file_name: str, local_dir: Path
    ) -> bool:
        """Скачивает файл пользователя с Диска"""
        try:
            remote_path = f"{self.BASE_FOLDER}/{chat_id}/{file_name}"
            local_path = local_dir / file_name

            if not await self.exists(remote_path):
                return False

            return await self.download(remote_path, str(local_path))

        except Exception as e:
            logger.error(f"❌ Ошибка скачивания для {chat_id}: {e}")
            return False

    async def ensure_folder_structure(self, chat_id: int):
        """Создаёт полную структуру папок пользователя"""
        folders = [
            f"{self.BASE_FOLDER}/{chat_id}",
            f"{self.BASE_FOLDER}/{chat_id}/archive",
        ]

        for folder in folders:
            await self.mkdirs(folder)

    async def get_user_files(self, chat_id: int) -> List[dict]:
        """Возвращает список файлов пользователя"""
        try:
            user_folder = f"{self.BASE_FOLDER}/{chat_id}"
            if not await self.exists(user_folder):
                return []

            files = await _run_sync(self.client.listdir, user_folder)
            result = []

            for item in files:
                result.append(
                    {
                        "name": item.name,
                        "path": item.path,
                        "type": item.type,
                        "size": getattr(item, "size", 0),
                        "modified": getattr(item, "modified", None),
                    }
                )

            return result

        except Exception as e:
            logger.error(f"❌ Ошибка получения списка файлов: {e}")
            return []


_yandex_service_instance: Optional[YandexDiskService] = None


def get_yandex_service(token: str = None) -> YandexDiskService:
    """Фабричная функция для создания экземпляра YandexDiskService"""
    global _yandex_service_instance

    if _yandex_service_instance is None:
        if token is None:
            token = os.getenv("YANDEX_DISK_TOKEN")
            if not token:
                raise ValueError("YANDEX_DISK_TOKEN не найден в .env")

        _yandex_service_instance = YandexDiskService(token=token)

    return _yandex_service_instance


def reset_yandex_service():
    """Сбрасывает глобальный экземпляр сервиса"""
    global _yandex_service_instance
    _yandex_service_instance = None
    logger.debug("🔄 YandexDiskService сброшен")

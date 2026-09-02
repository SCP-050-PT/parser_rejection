"""
Ядро парсера отказов — сессии, главный запуск, финальная проверка.
VK версия — убраны Telegram-зависимости.
"""

import time
import random
import asyncio
from threading import Lock
from typing import Dict, Any, Optional, List
from loguru import logger

from config.settings import settings

from src.rejection_parser.utils import (
    DailyStats, get_random_user_agent, get_platform_from_ua, run_async_in_thread
)
from src.rejection_parser.search import SearchManager
from src.rejection_parser.processing import ContractProcessor

# === ИМПОРТ CURL_CFFI ===
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as curl_requests
    HAS_CURL_CFFI = False


class SessionManager:
    """Управление пулом сессий curl_cffi"""

    def __init__(self, max_sessions: int = 7):
        self._sessions: List[Any] = []
        self.max_sessions = max_sessions
        self._init_session_pool()
        self.session = self._sessions[0] if self._sessions else self._create_session()

    def _init_session_pool(self):
        for i in range(self.max_sessions):
            session = self._create_session()
            self._sessions.append(session)
        logger.info(f"🔌 Пул сессий: {self.max_sessions} шт. создан")

    def get_session(self, index: int = 0) -> Any:
        if not self._sessions:
            return self._create_session()
        return self._sessions[index % len(self._sessions)]

    def _create_session(self):
        proxy = getattr(settings, "rejections_proxy_url", getattr(settings, "proxy_url", None))
        user_agent = get_random_user_agent()
        platform = get_platform_from_ua(user_agent)

        try:
            if HAS_CURL_CFFI:
                session = curl_requests.Session(impersonate="chrome124", proxy=proxy)
            else:
                session = curl_requests.Session()
                if proxy:
                    session.proxies = {"http": str(proxy), "https": str(proxy)}
        except Exception as e:
            logger.error(f"Ошибка создания сессии: {e}. Пробуем без прокси.")
            session = curl_requests.Session()

        self._update_session_headers(session, user_agent, platform)
        session.verify = False
        return session

    def _update_session_headers(self, session, user_agent: str, platform: str):
        sec_ch_ua = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        if "Firefox" in user_agent:
            sec_ch_ua = '"Not/A)Brand";v="8", "Firefox";v="125"'
        elif "Safari" in user_agent and "Chrome" not in user_agent:
            sec_ch_ua = '"Safari";v="17.4"'

        session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "Referer": "https://zakupki.gov.ru/",
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{platform}"',
        })

    def rotate_user_agent(self):
        user_agent = get_random_user_agent()
        platform = get_platform_from_ua(user_agent)
        self._update_session_headers(self.session, user_agent, platform)
        logger.debug(f"🎭 User-Agent изменён: {user_agent[:50]}...")

    def make_request_fast(self, url: str, session, timeout: int = 10, max_retries: int = 2) -> Optional[Any]:
        for attempt in range(max_retries):
            try:
                response = session.get(url, timeout=timeout)
                if response.status_code == 429:
                    wait = 2 * (attempt + 1)
                    logger.warning(f"  🚫 429 в потоке, ждём {wait}с...")
                    time.sleep(wait)
                    continue
                if response.status_code == 200:
                    return response
                logger.warning(f"  ⚠️ Статус {response.status_code}")
            except Exception as e:
                logger.error(f"  ❌ Ошибка запроса: {e}")
                time.sleep(1)
        return None


class RejectionParser:
    """
    Парсер отказов от zakupki.gov.ru
    Версия 6.0 VK — модульная архитектура, без Telegram-зависимостей
    """

    def __init__(self, excel_manager, rejections_cache, admin_vk_id: Optional[str] = None):
        self.excel_manager = excel_manager
        self.rejections_cache = rejections_cache
        self.admin_vk_id = admin_vk_id
        self._lock = Lock()

        self.daily_stats = DailyStats()
        self.session_processed = 0
        self.session_found = 0
        self.found_rejections: List[Dict] = []

        self._records_cache: Dict[str, int] = {}
        self._save_counter = 0
        self._file_created_on_disk = False

        self.max_workers = getattr(settings, "max_workers_rejections", 1)
        self.max_check_workers = getattr(settings, "max_check_workers", 5)

        # Инициализация менеджеров
        self.session_manager = SessionManager(max_sessions=self.max_check_workers + 2)
        self.search_manager = SearchManager(self.session_manager, self._records_cache, self._lock)
        self.contract_processor = ContractProcessor(
            excel_manager, rejections_cache, self.session_manager, admin_vk_id
        )

        logger.info(
            f"📝 RejectionParser v6.0 VK (модульная, {self.max_workers} workers, {self.max_check_workers} check)"
        )

    def run_parallel(self, max_pages: Optional[int] = None, max_workers: Optional[int] = None) -> Dict[str, Any]:
        max_pages_val = max_pages or getattr(settings, "max_parse_pages_rejections", 100)
        if max_workers is not None:
            self.max_workers = max_workers
            self.contract_processor.max_workers = max_workers

        logger.info("🚀 === ПАРСИНГ ОТКАЗОВ v6.0 VK ===")
        logger.info(f"   Диапазон цен: 100,000 - 1,000,000,000")
        logger.info(f"   Страниц на окно: {max_pages_val}")
        logger.info(f"   Потоков reject-info: {self.max_workers}")
        logger.info(f"   Потоков check_records: {self.max_check_workers}")

        self.found_rejections = []
        self.session_processed = 0
        self.session_found = 0
        self.daily_stats.reset_session()

        start_time = time.time()
        total_found = 0
        idx = 0

        for window in self.search_manager._generate_windows_aggregated():
            idx += 1
            contract_date_from, contract_date_to, price_from, price_to = window
            window_label = f"{contract_date_from} – {contract_date_to}, {price_from:,}-{price_to or '∞'}"

            logger.info(f"{'='*60}")
            logger.info(f"📅 [{idx}] Окно: {window_label}")
            logger.info(f"{'='*60}")

            contracts = self.contract_processor.search_contracts_with_rejections(
                search_manager=self.search_manager,
                contract_date_from=contract_date_from,
                contract_date_to=contract_date_to,
                price_from=price_from,
                price_to=price_to,
                max_pages=max_pages_val,
                max_workers=self.max_workers,
            )

            total_found += len(contracts)
            logger.info(f"   📊 Окно {window_label}: {len(contracts)} сегодняшних отказов")

            pause = random.uniform(1.0, 3.0)
            logger.info(f"⏳ Пауза между окнами: {pause:.1f}с")
            time.sleep(pause)

        search_time = time.time() - start_time

        logger.info(f"{'='*60}")
        logger.info("📊 ИТОГИ ПОИСКА:")
        logger.info(f"   Проверено окон: {idx}")
        logger.info(f"   Сегодняшних отказов: {total_found}")
        logger.info(f"   Время: {search_time:.1f}с")
        logger.info(f"{'='*60}")

        # Финальное сохранение
        public_url = None
        try:
            public_url = run_async_in_thread(self.excel_manager.force_save())
            if public_url:
                self._file_created_on_disk = True
                self._save_counter = 0
                logger.info(f"🔗 Публичная ссылка: {public_url}")
        except Exception as e:
            logger.error(f"❌ Ошибка финального сохранения: {e}")

        # Финальная проверка
        self._verify_excel_integrity(total_found)

        cache_stats = self.rejections_cache.get_stats()
        total_time = time.time() - start_time

        logger.info(f"🏁 ЗАВЕРШЕНО: {self.session_processed} проверено, {total_found} отказов")
        logger.info(f"   Время: {total_time:.1f}с")

        return {
            "total_found": total_found,
            "processed": self.session_processed,
            "new_today": cache_stats.get("new_today", 0),
            "excel_file": self.excel_manager.get_stats().get("file", "unknown"),
            "excel_url": public_url,
            "search_time_sec": search_time,
            "total_time_sec": total_time,
        }

    def _verify_excel_integrity(self, expected_count: int):
        """Проверка целостности Excel"""
        try:
            df = self.excel_manager.get_dataframe()
            excel_count = len(df)

            if excel_count != expected_count:
                logger.warning(
                    f"⚠️ РАСХОЖДЕНИЕ: найдено {expected_count} отказов, в Excel {excel_count} записей!"
                )
            else:
                logger.info(f"✅ ПРОВЕРКА: все {expected_count} отказов записаны в Excel")

            if "Ссылка на отказ" in df.columns and not df.empty:
                duplicates = df["Ссылка на отказ"].duplicated().sum()
                if duplicates > 0:
                    logger.warning(f"⚠️ Обнаружено {duplicates} дубликатов в Excel!")
                else:
                    logger.info(f"✅ Дубликатов в Excel не обнаружено")
        except Exception as e:
            logger.error(f"❌ Ошибка финальной проверки Excel: {e}")
            
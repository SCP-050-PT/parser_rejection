"""
core.py
Ядро парсера отказов — сессии, главный запуск, финальная проверка.
VK версия v7.7 Adapted for Arina (rnp-help.ru)
v7.7 изменения:
1. Graceful Shutdown — корректная обработка SIGINT/SIGTERM
2. Улучшенный Circuit Breaker для email-каналов
3. Прогрев сессии — имитация поведения перед стартом
4. Разделение IP на "горячие" (поиск) и "холодные" (карточки) - опционально
"""

import time
import random
import signal
import sys
import json
import os
from threading import Lock, Event
from typing import Dict, Any, Optional, List
from loguru import logger
from datetime import datetime
from config.settings import settings
from src.rejection_parser.utils import DailyStats, run_async_in_thread
from src.rejection_parser.search import SearchManager, BROWSER_PROFILES
from src.rejection_parser.processing import ContractProcessor

try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
except ImportError:
    import requests as curl_requests

    HAS_CURL_CFFI = False
    logger.warning("curl_cffi не найдена, используем обычный requests")

# === КОНФИГУРАЦИЯ МУЛЬТИ-IP v7.7 (Гибридная схема) ===
MULTI_IP_CONFIG = settings.multi_ip_config

# Разделение IP по ролям для оптимизации нагрузки
HOT_IP_ROLES = {"main", "primary", "local"}  # Для параллельного поиска (быстрые)
COLD_IP_ROLES = {
    "residential",
    "proxy",
    "backup",
}  # Для карточек и писем (медленные, дорогие)


class SessionManager:
    """Управление ротацией и пулом сессий с независимой привязкой к IP/Proxy"""

    def __init__(self, max_sessions: int = None):
        self._sessions: List[Any] = []
        self._ip_info: List[Dict[str, str]] = []
        self.max_sessions = len(MULTI_IP_CONFIG)
        self._shutdown_event = Event()
        self._active_requests = 0
        self._requests_lock = Lock()

        # Адаптивные задержки для каждого канала (базовые значения)
        self.ip_delays: Dict[str, float] = {
            ip_cfg["ip"]: 45.0 for ip_cfg in MULTI_IP_CONFIG
        }

        # Для "горячих" IP (основной сервер) — меньше задержка
        # Для "холодных" (резидентские прокси) — больше
        for ip_cfg in MULTI_IP_CONFIG:
            role = ip_cfg.get("role", "main")
            if role in COLD_IP_ROLES:
                self.ip_delays[ip_cfg["ip"]] = 120.0  # 2 минуты для резидентских
            else:
                self.ip_delays[ip_cfg["ip"]] = 45.0  # 45 сек для локальных

        self.banned_ips: Dict[str, float] = {}
        self._lock = Lock()

        # Circuit breaker для email-каналов
        self.email_channel_health: Dict[str, Dict[str, Any]] = {}
        self._init_email_channels()
        self._init_session_pool()
        self._setup_signal_handlers()

    def _init_email_channels(self):
        """Инициализация Circuit Breaker для email-каналов"""
        for i in range(3):  # 3 попытки по умолчанию
            channel_id = f"smtp_{i}"
            self.email_channel_health[channel_id] = {
                "failures": 0,
                "last_failure": 0,
                "cooldown_until": 0,
                "in_cooldown": False,
            }

    def _setup_signal_handlers(self):
        """Настройка обработчиков сигналов для graceful shutdown"""
        import threading

        if threading.current_thread() is not threading.main_thread():
            logger.warning(
                "⚠️ Сигналы нельзя зарегистрировать в дочернем потоке. Graceful shutdown отключен."
            )
            return

        def signal_handler(signum, frame):
            logger.warning(f"🛑 Получен сигнал {signum}, начинаем graceful shutdown...")
            self._shutdown_event.set()
            time.sleep(5)
            logger.info("✅ Graceful shutdown завершён")
            sys.exit(0)

        try:
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
            logger.debug("✅ Обработчики сигналов установлены успешно")
        except ValueError as e:
            logger.warning(f"⚠️ Не удалось установить сигналы: {e}")

    def _init_session_pool(self):
        """Инициализация пула сессий при старте"""
        for i, ip_config in enumerate(MULTI_IP_CONFIG):
            session = self._create_session(
                ip_address=ip_config["ip"],
                proxy_url=ip_config.get("proxy"),
                role=ip_config.get("role", "main"),
            )
            self._sessions.append(session)
            self._ip_info.append(ip_config)
            logger.info(
                f"🔌 Сессия #{i}: {ip_config['name']} ({ip_config['ip']}) "
                f"[{ip_config.get('role', 'main')}]"
            )

    def is_shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    @property
    def session(self) -> Any:
        return self._sessions[0] if self._sessions else None

    def get_session(self, index: int = 0) -> Any:
        if not self._sessions:
            return self._create_session()
        valid_idx = index % len(self._sessions)
        return self._sessions[valid_idx]

    def get_session_info(self, index: int = 0) -> Dict[str, str]:
        if not self._ip_info:
            return {"ip": "unknown", "name": "unknown"}
        return self._ip_info[index % len(self._ip_info)]

    def get_ip_name(self, ip_address: str) -> str:
        for info in self._ip_info:
            if info["ip"] == ip_address:
                return info["name"]
        return ip_address

    def get_session_by_ip(self, ip_address: str) -> Optional[Any]:
        with self._lock:
            for i, info in enumerate(self._ip_info):
                if info["ip"] == ip_address:
                    return self._sessions[i]
        return None

    def get_ip_role(self, ip_address: str) -> str:
        for info in self._ip_info:
            if info["ip"] == ip_address:
                return info.get("role", "main")
        return "unknown"

    def is_hot_ip(self, ip_address: str) -> bool:
        return self.get_ip_role(ip_address) in HOT_IP_ROLES

    def is_cold_ip(self, ip_address: str) -> bool:
        return self.get_ip_role(ip_address) in COLD_IP_ROLES

    def get_hot_ips(self) -> List[str]:
        return [
            info["ip"]
            for info in self._ip_info
            if not self.is_ip_banned(info["ip"]) and self.is_hot_ip(info["ip"])
        ]

    def get_cold_ips(self) -> List[str]:
        return [
            info["ip"]
            for info in self._ip_info
            if not self.is_ip_banned(info["ip"]) and self.is_cold_ip(info["ip"])
        ]

    def _recreate_session_for_ip(self, ip_address: str) -> Optional[Any]:
        with self._lock:
            for i, info in enumerate(self._ip_info):
                if info["ip"] == ip_address:
                    try:
                        if hasattr(self._sessions[i], "close"):
                            self._sessions[i].close()
                    except Exception:
                        pass
                    new_session = self._create_session(
                        ip_address=info["ip"],
                        proxy_url=info.get("proxy"),
                        role=info.get("role", "main"),
                    )
                    self._sessions[i] = new_session
                    logger.info(
                        f"🔄 Сессия для {info['name']} ({ip_address}) пересоздана!"
                    )
                    return new_session
        return None

    def get_current_delay(self, ip_address: str) -> float:
        base_delay = self.ip_delays.get(ip_address, 45.0)
        if self.is_cold_ip(ip_address):
            return base_delay * 1.5
        return base_delay

    def report_success(self, ip_address: str):
        with self._lock:
            current = self.ip_delays.get(ip_address, 45.0)
            min_delay = 20.0 if self.is_hot_ip(ip_address) else 60.0
            self.ip_delays[ip_address] = max(min_delay, current * 0.95)

    def report_rate_limit(self, ip_address: str):
        with self._lock:
            current = self.ip_delays.get(ip_address, 45.0)
            new_delay = min(300.0, current * 2.0)
            self.ip_delays[ip_address] = new_delay
            logger.warning(
                f"🛡️ [Adaptive] {ip_address} rate-limited. Задержка: {new_delay:.1f}с"
            )

    def report_ban(self, ip_address: str):
        with self._lock:
            self.banned_ips[ip_address] = time.time() + settings.ip_ban_duration_seconds
            duration_min = settings.ip_ban_duration_seconds / 60
            logger.error(
                f"🚫 IP {ip_address} заблокирован WAF (403) на {duration_min:.1f} мин!"
            )

    def is_ip_banned(self, ip_address: str) -> bool:
        unban_time = self.banned_ips.get(ip_address, 0)
        return time.time() < unban_time

    def get_healthy_ip_info(self, preferred_index: int = 0) -> Optional[Dict[str, str]]:
        total_ips = len(MULTI_IP_CONFIG)
        for offset in range(total_ips):
            candidate_idx = (preferred_index + offset) % total_ips
            info = self.get_session_info(candidate_idx)
            if not self.is_ip_banned(info["ip"]):
                return info
        return None

    def get_healthy_ips(self) -> List[str]:
        return [
            info["ip"] for info in self._ip_info if not self.is_ip_banned(info["ip"])
        ]

    def _create_session(
        self, ip_address: str = None, proxy_url: str = None, role: str = "main"
    ):
        try:
            if HAS_CURL_CFFI:
                session_kwargs = {"impersonate": "chrome131"}
                if proxy_url:
                    session_kwargs["proxy"] = proxy_url
                elif ip_address:
                    session_kwargs["interface"] = ip_address
                session = curl_requests.Session(**session_kwargs)
                self._warmup_session(session, ip_address or proxy_url, role)
            else:
                session = curl_requests.Session()
        except Exception as e:
            logger.error(f"Ошибка создания сессии [{ip_address}]: {e}")
            session = curl_requests.Session()

        self._update_headers(session)
        session.verify = False
        return session

    def _warmup_session(self, session, identifier: str, role: str):
        try:
            warmup_urls = ["https://zakupki.gov.ru/epz/main/public/home.html"]
            if role in COLD_IP_ROLES:
                warmup_delay = random.uniform(5.0, 10.0)
            else:
                warmup_delay = random.uniform(2.0, 4.0)

            time.sleep(warmup_delay)
            for url in warmup_urls[:1]:
                try:
                    resp = session.get(url, timeout=15)
                    logger.debug(f"🔥 Прогрев [{identifier}]: {resp.status_code}")
                    time.sleep(random.uniform(1.0, 3.0))
                except Exception as e:
                    logger.debug(f"⚠️ Ошибка прогрева [{identifier}]: {e}")
        except Exception as e:
            logger.debug(f"⚠️ Ошибка прогрева сессии: {e}")

    def _update_headers(self, session):
        profile = random.choice(BROWSER_PROFILES)
        session.headers.update(
            {
                "User-Agent": profile["ua"],
                "sec-ch-ua": profile["sec_ch_ua"],
                "sec-ch-ua-platform": profile["platform"],
                "Accept-Language": profile["lang"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Connection": "keep-alive",
                "Referer": "https://zakupki.gov.ru/epz/contract/search/results.html",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }
        )

    def make_request_fast(
        self,
        url: str,
        session,
        timeout: int = 30,
        max_retries: int = 5,
        current_ip: str = "unknown",
    ) -> Optional[Any]:
        if self.is_ip_banned(current_ip):
            logger.warning(f"Пропуск запроса: IP {current_ip} в бане.")
            return None

        if self._shutdown_event.is_set():
            logger.info("🛑 Запрос отменён: идёт shutdown")
            return None

        active_session = session
        is_cold = self.is_cold_ip(current_ip)
        if is_cold:
            max_retries = min(max_retries, 3)

        u = (url or "").lower()
        is_search = "search/results" in u or "quick-search" in u
        is_heavy = (
            "reject-info" in u or "common-info" in u or "contractcard" in u
        )

        with self._requests_lock:
            self._active_requests += 1

        try:
            for attempt in range(1, max_retries + 1):
                if self._shutdown_event.is_set():
                    return None

                if is_cold:
                    base = random.uniform(5.0, 9.0)
                elif is_heavy:
                    base = random.uniform(2.5, 4.5)
                elif is_search:
                    base = random.uniform(1.5, 3.2)
                else:
                    base = random.uniform(2.0, 4.0)

                delay = base + random.uniform(0, 0.8)
                time.sleep(delay)

                start_time = time.time()
                try:
                    response = active_session.get(url, timeout=timeout)
                    duration = time.time() - start_time

                    if response.status_code == 403:
                        logger.error(
                            f"🚫 [403] IP: {current_ip} | {duration:.2f}с | URL: {url[:80]}"
                        )
                        self.report_ban(current_ip)
                        self._recreate_session_for_ip(current_ip)
                        return None

                    if response.status_code == 429:
                        self.report_rate_limit(current_ip)
                        backoff = (15.0 * (2 ** (attempt - 1))) + random.uniform(
                            2.0, 6.0
                        )
                        logger.warning(
                            f"⚠️ [429] IP: {current_ip}. Сон: {backoff:.1f}с"
                        )
                        time.sleep(backoff)
                        continue

                    if response.status_code == 200:
                        self.report_success(current_ip)
                        return response

                    # любой другой код — явно в лог
                    logger.warning(
                        f"⚠️ HTTP {response.status_code} [{current_ip}] "
                        f"попытка {attempt}/{max_retries} за {duration:.2f}с | "
                        f"URL: {url[:80]}"
                    )
                    time.sleep(random.uniform(2.0, 5.0))

                except Exception as e:
                    duration = time.time() - start_time
                    logger.warning(
                        f"Ошибка [{current_ip}] (Попытка {attempt}) "
                        f"за {duration:.2f}с: {e}"
                    )
                    new_sess = self._recreate_session_for_ip(current_ip)
                    if new_sess:
                        active_session = new_sess
                    time.sleep(random.uniform(2.0, 4.0))

            logger.error(
                f"❌ Лимит попыток ({max_retries}) исчерпан для: {url[:80]}"
            )
            return None
        finally:
            with self._requests_lock:
                self._active_requests -= 1

    def get_active_requests_count(self) -> int:
        with self._requests_lock:
            return self._active_requests

    def check_email_channel_health(self, channel_id: str) -> bool:
        health = self.email_channel_health.get(channel_id, {})
        if health.get("in_cooldown", False):
            if time.time() < health.get("cooldown_until", 0):
                return False
            else:
                health["in_cooldown"] = False
                health["failures"] = 0
                self.email_channel_health[channel_id] = health
        return True

    def report_email_channel_failure(self, channel_id: str):
        health = self.email_channel_health.get(
            channel_id, {"failures": 0, "in_cooldown": False}
        )
        health["failures"] += 1
        if health["failures"] >= 3:
            health["in_cooldown"] = True
            health["cooldown_until"] = time.time() + 600
            logger.error(
                f"🚫 Email-канал {channel_id} переведён в cooldown на 10 минут"
            )
        self.email_channel_health[channel_id] = health

    def report_email_channel_success(self, channel_id: str):
        if channel_id in self.email_channel_health:
            self.email_channel_health[channel_id]["failures"] = 0
            self.email_channel_health[channel_id]["in_cooldown"] = False


class RejectionParser:
    """Главный класс парсера отказов v7.7 Adapted for Arina"""

    def __init__(
        self, excel_manager, rejections_cache, admin_vk_id: Optional[str] = None
    ):
        self.excel_manager = excel_manager
        self.rejections_cache = rejections_cache
        self.admin_vk_id = admin_vk_id
        self._lock = Lock()
        self.daily_stats = DailyStats()
        self.session_processed = 0
        self.session_found = 0
        self.found_rejections: List[Dict] = []
        self._records_cache: Dict[str, int] = {}

        self.max_workers = getattr(settings, "max_workers_rejections", 2)
        self.max_check_workers = getattr(settings, "max_check_workers", 3)

        self.session_manager = SessionManager(max_sessions=self.max_check_workers)
        self.search_manager = SearchManager(
            self.session_manager, self._records_cache, self._lock
        )
        self.contract_processor = ContractProcessor(
            excel_manager, rejections_cache, self.session_manager, admin_vk_id
        )

    def run_parallel(
        self, max_pages: Optional[int] = None, max_workers: Optional[int] = None
    ) -> Dict[str, Any]:
        max_pages_val = max_pages or getattr(
            settings, "max_parse_pages_rejections", 100
        )
        if max_workers is not None:
            self.max_workers = max_workers
            self.contract_processor.max_workers = max_workers

        logger.info(
            "=== ПАРСИНГ ОТКАЗОВ v7.7 VK ARINA (Hybrid Multi-IP + WAF-адаптация) ==="
        )
        logger.info(f"   Страниц на окно: {max_pages_val}")
        logger.info(f"   Активных каналов: {len(MULTI_IP_CONFIG)}")

        hot_ips = self.session_manager.get_hot_ips()
        cold_ips = self.session_manager.get_cold_ips()
        logger.info(f"   🌶️ Горячих IP (поиск): {len(hot_ips)}")
        logger.info(f"   ❄️ Холодных IP (карточки): {len(cold_ips)}")

        self.found_rejections = []
        self.session_processed = 0
        self.session_found = 0
        self.daily_stats.reset_session()

        start_time = time.time()
        total_found = 0
        idx = 0

        try:
            for window in self.search_manager._generate_windows_aggregated():
                if self.session_manager.is_shutdown_requested():
                    logger.warning("🛑 Shutdown requested, прерываем обработку окон")
                    break

                idx += 1
                contract_date_from, contract_date_to, price_from, price_to = window
                window_label = f"{contract_date_from} – {contract_date_to}, {price_from:,}-{price_to or '∞'}"

                # Для поиска используем только "горячие" IP, если они есть
                hot_pool = self.session_manager.get_hot_ips()
                if not hot_pool:
                    hot_pool = self.session_manager.get_cold_ips()

                if not hot_pool:
                    logger.error(
                        f"❌ ВСЕ IP ЗАБЛОКИРОВАНЫ! Пропуск окна {window_label}"
                    )
                    continue

                ip_names = ", ".join(
                    self.session_manager.get_ip_name(ip) for ip in hot_pool
                )
                logger.info(f"📅 [{idx}] Окно: {window_label} | 🌐 Пул: {ip_names}")

                contracts = self.contract_processor.search_contracts_with_rejections(
                    search_manager=self.search_manager,
                    contract_date_from=contract_date_from,
                    contract_date_to=contract_date_to,
                    price_from=price_from,
                    price_to=price_to,
                    max_pages=max_pages_val,
                    max_workers=self.max_workers,
                    ip_pool=hot_pool,
                )

                count = len(contracts)
                logger.info(
                    f"📊 Окно [{window_label}] завершено: найдено {count} отказов"
                )
                total_found += count

                avg_delay = sum(
                    self.session_manager.get_current_delay(ip) for ip in hot_pool
                ) / len(hot_pool)
                if any(self.session_manager.is_cold_ip(ip) for ip in hot_pool):
                    sleep_time = random.uniform(3.0, 6.0)
                else:
                    sleep_time = random.uniform(avg_delay * 0.4, avg_delay * 0.7)

                logger.info(f"⏳ Пауза между окнами: {sleep_time:.1f}с")
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.warning("🛑 Прервано пользователем (KeyboardInterrupt)")
        except Exception as e:
            logger.error(f"❌ Критическая ошибка в главном цикле: {e}")
            raise
        finally:
            # Корректная остановка процессора (сохранение pending emails)
            self.contract_processor.shutdown()

            logger.info("💾 Финальное сохранение результатов...")
            total_time = time.time() - start_time

            logger.info("=" * 60)
            logger.info("📊 ИТОГИ ПОИСКА:")
            logger.info(f"   Обработано окон: {idx}")
            logger.info(f"   Найдено отказов: {total_found}")
            logger.info(f"   Общее время: {total_time:.1f}с")
            logger.info("=" * 60)

            public_url = None
            try:
                public_url = run_async_in_thread(self.excel_manager.force_save())
            except Exception as e:
                logger.error(f"❌ Ошибка сохранения: {e}")

            self._verify_excel_integrity(total_found)
            cache_stats = self.rejections_cache.get_stats()

            return {
                "total_found": total_found,
                "processed": self.session_processed,
                "new_today": cache_stats.get("new_today", 0),
                "excel_file": self.excel_manager.get_stats().get("file", "unknown"),
                "excel_url": public_url,
                "total_time_sec": total_time,
            }

    def _verify_excel_integrity(self, expected_count: int):
        try:
            df = self.excel_manager.get_dataframe()
            excel_count = len(df)
            if excel_count != expected_count:
                logger.warning(
                    f"⚠️ РАСХОЖДЕНИЕ: найдено {expected_count}, в Excel {excel_count}!"
                )
            else:
                logger.info(f"✅ ПРОВЕРКА: все {expected_count} отказов в Excel")
        except Exception as e:
            logger.error(f"❌ Ошибка проверки Excel: {e}")

"""
Поиск и генерация окон для парсера отказов.
VK версия.
"""

import random
import time
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Any, Generator
from collections import deque
from urllib.parse import urlencode
from loguru import logger
from config.settings import settings
from bs4 import BeautifulSoup
import re

from src.rejection_parser.utils import get_random_user_agent, get_platform_from_ua
from config.settings import settings

# === ИМПОРТ CURL_CFFI ===
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as curl_requests
    HAS_CURL_CFFI = False


class SearchManager:
    """Управление поиском: окна, URL, запросы, дробление"""

    BASE_SEARCH_URL = "https://zakupki.gov.ru/epz/contract/search/results.html"

    MAX_RECORDS_PER_WINDOW = 5000
    DEFAULT_PRICE_FROM = 100_000
    MAX_PRICE_TO = 1_000_000_000
    MIN_PRICE_RANGE = 1000

    def __init__(self, session_manager, records_cache: Dict, lock: Lock):
        self.session_manager = session_manager
        self._records_cache = records_cache
        self._lock = lock
        self._consecutive_429 = 0
        self._base_delay = 5

    def _calculate_delay(self) -> float:
        if self._consecutive_429 > 0:
            delay = self._base_delay * (2 ** (self._consecutive_429 - 1))
            delay = min(delay, 300)
            logger.warning(f"⏳ Backoff: {delay}с (429 подряд: {self._consecutive_429})")
            return delay
        return random.uniform(
            getattr(settings, "delay_between_pages_min", 0.05),
            getattr(settings, "delay_between_pages_max", 0.15),
        )

    def _handle_429(self):
        self._consecutive_429 += 1
        self.session_manager.rotate_user_agent()
        delay = self._calculate_delay()
        logger.warning(f"🚫 429. Ждём {delay}с...")
        time.sleep(delay)

    def _reset_429_counter(self):
        if self._consecutive_429 > 0:
            logger.info(f"✅ Сброс 429 (было: {self._consecutive_429})")
            self._consecutive_429 = 0

    def _make_request(self, url: str, timeout: int = 30, max_retries: int = 3) -> Optional[Any]:
        for attempt in range(max_retries):
            delay = self._calculate_delay()
            if attempt > 0:
                logger.info(f"  ⏳ Попытка {attempt + 1}, задержка {delay:.1f}с...")
            time.sleep(delay)

            try:
                response = self.session_manager.session.get(url, timeout=timeout)
                if response.status_code == 429:
                    self._handle_429()
                    continue
                if response.status_code == 200:
                    self._reset_429_counter()
                    return response
                logger.warning(f"  ⚠️ Статус {response.status_code}")
            except Exception as e:
                logger.error(f"  ❌ Ошибка запроса: {e}")
                time.sleep(5)
        return None

    def _make_request_fast(self, url: str, session, timeout: int = 10, max_retries: int = 2) -> Optional[Any]:
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

    def _build_search_url(
        self,
        page: int,
        records_per_page: Optional[str] = None,
        contract_date_from: Optional[str] = None,
        contract_date_to: Optional[str] = None,
        price_from: Optional[int] = None,
        price_to: Optional[int] = None,
    ) -> str:
        if records_per_page is None:
            records_per_page = getattr(settings, "records_per_page", "50")
        if records_per_page is not None and not records_per_page.startswith("_"):
            records_per_page = f"_{records_per_page}"

        params = {
            "morphology": "on",
            "search-filter": "Дате размещения",
            "fz44": "on",
            "contractStageList_0": "on",
            "contractStageList": "0",
            "budgetLevelsIdNameHidden": "{}",
            "sortBy": "UPDATE_DATE",
            "pageNumber": str(page),
            "sortDirection": "false",
            "recordsPerPage": records_per_page,
            "showLotsInfoHidden": "false",
        }

        if contract_date_from:
            params["contractDateFrom"] = contract_date_from
        if contract_date_to:
            params["contractDateTo"] = contract_date_to
        if price_from is not None:
            params["contractPriceFrom"] = str(price_from)
        if price_to is not None:
            params["contractPriceTo"] = str(price_to)

        return f"{self.BASE_SEARCH_URL}?{urlencode(params, safe='{}')}"

    def _parse_total_records(self, html_content: str) -> int:
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            
            # === 1. Точное попадание по твоей верстке: div.search-results__total ===
            total_elem = soup.find("div", class_="search-results__total")
            if total_elem:
                text = total_elem.get_text(strip=True)
                # Убираем "более", "около" и т.д.
                text_clean = re.sub(
                    r"^(более|около|примерно|свыше)\s+",
                    "",
                    text,
                    flags=re.IGNORECASE,
                )
                # Ищем число с любыми пробелами (включая &nbsp;)
                match = re.search(r"([\d\s\u00a0\u202f]+)", text_clean)
                if match:
                    num_str = (
                        match.group(1)
                        .replace(" ", "")
                        .replace("\xa0", "")
                        .replace("\u202f", "")
                    )
                    if num_str.isdigit():
                        return int(num_str)

            # === 2. Fallback: поиск по всему тексту ===
            text = soup.get_text()
            # Явно указываем \u00a0 и \u202f в паттерне
            match = re.search(
                r"(\d[\d\s\u00a0\u202f]*)\s*запис",
                text,
                re.IGNORECASE,
            )
            if match:
                num_str = (
                    match.group(1)
                    .replace(" ", "")
                    .replace("\xa0", "")
                    .replace("\u202f", "")
                )
                return int(num_str)

            # === 3. Fallback: span.search-results__total (старая верстка) ===
            count_elem = soup.find("span", class_=re.compile(r"search-results__total"))
            if count_elem:
                num_str = (
                    count_elem.get_text(strip=True)
                    .replace(" ", "")
                    .replace("\xa0", "")
                    .replace("\u202f", "")
                )
                if num_str.isdigit():
                    return int(num_str)

            return 0

        except Exception as e:
            logger.debug(f"🔍 Не удалось определить количество записей: {e}")
            return 0

    # === ГЕНЕРАЦИЯ ОКОН ===

    def _generate_windows_aggregated(self) -> Generator[Tuple[str, str, int, Optional[int]], None, None]:
        today = datetime.now().date()
        start_date = today - timedelta(days=1095)
        yesterday = today - timedelta(days=1)

        logger.info(f"📅 Агрегированная генерация: {start_date.strftime('%d.%m.%Y')} – {yesterday.strftime('%d.%m.%Y')}")

        current = start_date
        buffer_date_from = None
        buffer_date_to = None
        buffer_records = 0

        while current <= yesterday:
            week_end = min(current + timedelta(days=6), yesterday)
            week_records = self._check_records(current, week_end, self.DEFAULT_PRICE_FROM, self.MAX_PRICE_TO)

            if week_records >= self.MAX_RECORDS_PER_WINDOW:
                if buffer_records > 0:
                    yield (buffer_date_from.strftime("%d.%m.%Y"), buffer_date_to.strftime("%d.%m.%Y"), self.DEFAULT_PRICE_FROM, self.MAX_PRICE_TO)
                    buffer_date_from = None
                    buffer_date_to = None
                    buffer_records = 0
                yield from self._split_date_range_aggregated(current, week_end)
                current = week_end + timedelta(days=1)
                continue

            if buffer_records == 0:
                buffer_date_from = current
                buffer_date_to = week_end
                buffer_records = week_records
            elif buffer_records + week_records < self.MAX_RECORDS_PER_WINDOW:
                buffer_date_to = week_end
                buffer_records += week_records
            else:
                yield (buffer_date_from.strftime("%d.%m.%Y"), buffer_date_to.strftime("%d.%m.%Y"), self.DEFAULT_PRICE_FROM, self.MAX_PRICE_TO)
                buffer_date_from = current
                buffer_date_to = week_end
                buffer_records = week_records

            current = week_end + timedelta(days=1)

        if buffer_records > 0:
            yield (buffer_date_from.strftime("%d.%m.%Y"), buffer_date_to.strftime("%d.%m.%Y"), self.DEFAULT_PRICE_FROM, self.MAX_PRICE_TO)

    def _check_records(self, date_from: datetime.date, date_to: datetime.date, price_from: int, price_to: Optional[int] = None) -> int:
        key = f"{date_from.isoformat()}|{date_to.isoformat()}|{price_from}|{price_to or 'inf'}"
        if key in self._records_cache:
            return self._records_cache[key]

        url = self._build_search_url(
            page=1,
            contract_date_from=date_from.strftime("%d.%m.%Y"),
            contract_date_to=date_to.strftime("%d.%m.%Y"),
            price_from=price_from,
            price_to=price_to,
        )
        response = self._make_request(url, timeout=25, max_retries=3)
        if response and response.status_code == 200:
            result = self._parse_total_records(response.text)
            self._records_cache[key] = result
            return result

        logger.warning(f"⚠️ Не удалось проверить окно {date_from}-{date_to}")
        return 9999

    def _split_date_range_aggregated(self, date_from: datetime.date, date_to: datetime.date, price_from: int = DEFAULT_PRICE_FROM, price_to: Optional[int] = None) -> Generator[Tuple[str, str, int, Optional[int]], None, None]:
        if price_to is None:
            price_to = self.MAX_PRICE_TO

        records = self._check_records(date_from, date_to, price_from, price_to)
        if records < self.MAX_RECORDS_PER_WINDOW:
            yield (date_from.strftime("%d.%m.%Y"), date_to.strftime("%d.%m.%Y"), price_from, price_to)
            return

        if date_from == date_to:
            yield from self._split_by_price_binary_parallel(date_from, price_from, price_to)
            return

        days_diff = (date_to - date_from).days
        mid_offset = days_diff // 2
        mid_date = date_from + timedelta(days=mid_offset)

        logger.info(f"📊 {date_from}-{date_to}: {records} записей → дробим дату")
        yield from self._split_date_range_aggregated(date_from, mid_date, price_from, price_to)
        yield from self._split_date_range_aggregated(mid_date + timedelta(days=1), date_to, price_from, price_to)

    def _split_by_price_binary_parallel(self, date: datetime.date, price_from: int, price_to: Optional[int] = None) -> Generator[Tuple[str, str, int, Optional[int]], None, None]:
        if price_to is None:
            price_to = self.MAX_PRICE_TO
        if price_from >= price_to or (price_to - price_from) < self.MIN_PRICE_RANGE:
            yield (date.strftime("%d.%m.%Y"), date.strftime("%d.%m.%Y"), price_from, price_to)
            return

        total_records = self._check_records(date, date, price_from, price_to)
        if total_records < self.MAX_RECORDS_PER_WINDOW:
            yield (date.strftime("%d.%m.%Y"), date.strftime("%d.%m.%Y"), price_from, price_to)
            return

        logger.info(f"📊 {date}: {price_from:,}-{price_to:,} = {total_records} → бинарное дробление")

        queue = deque()
        queue.append((price_from, price_to, 0))

        while queue:
            level_items = []
            current_depth = queue[0][2]
            while queue and queue[0][2] == current_depth:
                level_items.append(queue.popleft())
            if not level_items:
                break

            windows_to_check = []
            for pf, pt, _ in level_items:
                key = f"{date.isoformat()}|{date.isoformat()}|{pf}|{pt or 'inf'}"
                if key in self._records_cache:
                    records = self._records_cache[key]
                    if records < self.MAX_RECORDS_PER_WINDOW:
                        yield (date.strftime("%d.%m.%Y"), date.strftime("%d.%m.%Y"), pf, pt)
                        continue
                    elif (pt - pf) <= self.MIN_PRICE_RANGE:
                        logger.warning(f"⚠️ {date}: {pf:,}-{pt:,} = {records}, мин. диапазон достигнут")
                        yield (date.strftime("%d.%m.%Y"), date.strftime("%d.%m.%Y"), pf, pt)
                        continue
                    else:
                        mid = pf + (pt - pf) // 2
                        queue.append((pf, mid, current_depth + 1))
                        queue.append((mid, pt, current_depth + 1))
                        continue
                else:
                    windows_to_check.append((pf, pt))

            if windows_to_check:
                results = self._check_windows_parallel(date, windows_to_check)
                for (pf, pt), records in results:
                    if records < self.MAX_RECORDS_PER_WINDOW:
                        yield (date.strftime("%d.%m.%Y"), date.strftime("%d.%m.%Y"), pf, pt)
                    elif (pt - pf) <= self.MIN_PRICE_RANGE:
                        logger.warning(f"⚠️ {date}: {pf:,}-{pt:,} = {records}, мин. диапазон достигнут")
                        yield (date.strftime("%d.%m.%Y"), date.strftime("%d.%m.%Y"), pf, pt)
                    else:
                        logger.info(f"📊 {date}: {pf:,}-{pt:,} = {records} → дробим")
                        mid = pf + (pt - pf) // 2
                        queue.append((pf, mid, current_depth + 1))
                        queue.append((mid, pt, current_depth + 1))

    def _check_windows_parallel(self, date: datetime.date, windows: List[Tuple[int, int]]) -> List[Tuple[Tuple[int, int], int]]:
        if not windows:
            return []

        results = []
        max_workers = min(getattr(settings, "max_check_workers", 5), len(windows))

        def check_one(args):
            window, session = args
            pf, pt = window
            key = f"{date.isoformat()}|{date.isoformat()}|{pf}|{pt or 'inf'}"
            if key in self._records_cache:
                return (pf, pt, self._records_cache[key])

            url = self._build_search_url(
                page=1,
                contract_date_from=date.strftime("%d.%m.%Y"),
                contract_date_to=date.strftime("%d.%m.%Y"),
                price_from=pf,
                price_to=pt,
            )
            for attempt in range(2):
                try:
                    response = session.get(url, timeout=25)
                    if response.status_code == 429:
                        time.sleep(2 * (attempt + 1))
                        continue
                    if response.status_code == 200:
                        result = self._parse_total_records(response.text)
                        with self._lock:
                            self._records_cache[key] = result
                        return (pf, pt, result)
                except Exception as e:
                    logger.error(f"  ❌ Ошибка проверки окна {pf}-{pt}: {e}")
                    time.sleep(1)
            return (pf, pt, 9999)

        args = [(w, self.session_manager.get_session(i)) for i, w in enumerate(windows)]

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(check_one, a): a for a in args}
            for future in as_completed(futures):
                pf, pt, records = future.result()
                results.append(((pf, pt), records))
                logger.debug(f"  📊 Проверено: {pf:,}-{pt:,} = {records} записей")

        return results

    def _fetch_page_batch(self, urls: List[str], batch_size: int = 3) -> List[Optional[Any]]:
        results = []
        for i in range(0, len(urls), batch_size):
            batch = urls[i:i + batch_size]
            batch_results = [None] * len(batch)
            logger.info(f"  ⚡ Пачка {i//batch_size + 1}: страницы {i+1}-{min(i+batch_size, len(urls))}")

            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                future_to_idx = {executor.submit(self._make_request, url): idx for idx, url in enumerate(batch)}
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        response = future.result(timeout=30)
                        batch_results[idx] = response
                        if response:
                            self._reset_429_counter()
                    except Exception as e:
                        logger.error(f"  ❌ Ошибка загрузки страницы: {e}")

            results.extend(batch_results)
            if i + batch_size < len(urls):
                if self._consecutive_429 >= 2:
                    pause = random.uniform(1.0, 2.0)
                    logger.warning(f"   ⏳ Пауза после 429: {pause:.1f}с")
                    time.sleep(pause)
                else:
                    time.sleep(random.uniform(0.0, 0.2))
        return results

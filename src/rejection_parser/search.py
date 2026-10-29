"""
search.py
Поиск и генерация окон для парсера отказов.
VK версия v7.7 Adapted for Arina.
FIX: _check_records → None при ошибке; один _split_window_recursive; старые→новые.
"""

import random
import time
import re
from collections import deque
from threading import Lock
from datetime import datetime, timedelta, date
from typing import List, Dict, Optional, Tuple, Any, Generator
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger
from config.settings import settings
from bs4 import BeautifulSoup

BROWSER_PROFILES = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not?A_Brand";v="24"',
        "platform": '"Windows"',
        "lang": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "platform": '"Windows"',
        "lang": "ru-RU,ru;q=0.9",
    },
]


class SearchManager:
    BASE_SEARCH_URL = "https://zakupki.gov.ru/epz/contract/search/results.html"
    MAX_RECORDS_PER_WINDOW = 4800
    DEFAULT_PRICE_FROM = 100_000
    MAX_PRICE_TO = None
    MIN_PRICE_RANGE = 1000
    START_DATE = date(2026, 2, 1)

    def __init__(self, session_manager, records_cache: Dict, lock: Lock):
        self.session_manager = session_manager
        self._records_cache = records_cache
        self._lock = lock
        self._request_counter = 0

    def _make_request_with_ip(
        self,
        url: str,
        timeout: int = 30,
        max_retries: int = 5,
        target_ip: Optional[str] = None,
    ) -> Tuple[Optional[Any], str]:
        with self._lock:
            self._request_counter += 1
            counter = self._request_counter

        if target_ip and not self.session_manager.is_ip_banned(target_ip):
            current_ip = target_ip
            session = self.session_manager.get_session_by_ip(current_ip)
        else:
            ip_info = self.session_manager.get_healthy_ip_info(preferred_index=counter)
            if not ip_info:
                logger.error("❌ Нет доступных не забаненных IP для запроса!")
                return None, "unknown"
            current_ip = ip_info["ip"]
            session = self.session_manager.get_session_by_ip(current_ip)

        if not session:
            session = self.session_manager.get_session(counter)

        response = self.session_manager.make_request_fast(
            url=url,
            session=session,
            timeout=timeout,
            max_retries=max_retries,
            current_ip=current_ip,
        )

        if response is None and self.session_manager.is_ip_banned(current_ip):
            fallback_info = self.session_manager.get_healthy_ip_info()
            if fallback_info and fallback_info["ip"] != current_ip:
                fallback_ip = fallback_info["ip"]
                logger.warning(
                    f"🔄 Переключение запроса с забаненного IP {current_ip} "
                    f"на живой IP {fallback_ip}"
                )
                fallback_session = self.session_manager.get_session_by_ip(fallback_ip)
                response = self.session_manager.make_request_fast(
                    url=url,
                    session=fallback_session,
                    timeout=timeout,
                    max_retries=max_retries,
                    current_ip=fallback_ip,
                )
                return response, fallback_ip

        return response, current_ip

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
            records_per_page = str(getattr(settings, "records_per_page", "50") or "50")
        records_per_page = str(records_per_page)
        if not records_per_page.startswith("_"):
            records_per_page = f"_{records_per_page}"

        params = {
            "morphology": "on",
            "search-filter": "Дате обновления",
            "fz44": "on",
            "contractStageList_0": "on",
            "contractStageList": "0",
            "sortBy": "UPDATE_DATE",
            "pageNumber": str(page),
            "sortDirection": "false",
            "recordsPerPage": records_per_page,
            "showLotsInfoHidden": "false",
        }
        # contract_date_* = день updateDate (имена аргументов оставляем для совместимости)
        if contract_date_from:
            params["updateDateFrom"] = contract_date_from
        if contract_date_to:
            params["updateDateTo"] = contract_date_to
        if price_from is not None:
            params["contractPriceFrom"] = str(price_from)
        if price_to is not None:
            params["contractPriceTo"] = str(price_to)

        return f"{self.BASE_SEARCH_URL}?{urlencode(params)}"

    def _parse_total_records(self, html_content: str) -> int:
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            total_elem = soup.find("div", class_="search-results__total")
            if total_elem:
                text = total_elem.get_text(strip=True)
                match = re.search(r"([\d\s\xa0\u202f]+)", text)
                if match:
                    num_str = re.sub(r"\D", "", match.group(1))
                    if num_str.isdigit():
                        return int(num_str)
            return 0
        except Exception:
            return 0

    @staticmethod
    def _cache_key(date_from_iso: str, date_to_iso: str, price_from, price_to) -> str:
        return f"upd|{date_from_iso}|{date_to_iso}|{price_from}|{price_to or 'inf'}"

    def _check_records(
        self,
        date_from: date,
        date_to: date,
        price_from: int,
        price_to: Optional[int] = None,
        target_ip: Optional[str] = None,
    ) -> Optional[int]:
        """
        None = ошибка сети/HTTP (НЕ путать с 0 записей).
        0 = реально пустая выдача.
        """
        key = self._cache_key(
            date_from.isoformat(), date_to.isoformat(), price_from, price_to
        )
        with self._lock:
            if key in self._records_cache:
                return self._records_cache[key]

        url = self._build_search_url(
            page=1,
            contract_date_from=date_from.strftime("%d.%m.%Y"),
            contract_date_to=date_to.strftime("%d.%m.%Y"),
            price_from=price_from,
            price_to=price_to,
        )
        response = self._make_request_with_ip(
            url, timeout=30, max_retries=3, target_ip=target_ip
        )[0]

        if response is None or getattr(response, "status_code", None) != 200:
            logger.warning(
                f"⚠️ _check_records fail {date_from}–{date_to} "
                f"status={getattr(response, 'status_code', None)}"
            )
            return None

        result = self._parse_total_records(response.text)
        with self._lock:
            self._records_cache[key] = result
        return result

    def _grow_window_greedy_forward(
        self, date_from: date, hard_limit_end: date
    ) -> date:
        """Жадно расширяем окно вперёд. При ошибке сети — только 1 день."""
        target = int(self.MAX_RECORDS_PER_WINDOW * self.GREEDY_TARGET_RATIO)
        step_days = 6
        best_end = min(date_from + timedelta(days=step_days), hard_limit_end)
        best_total = self._check_records(
            date_from, best_end, self.DEFAULT_PRICE_FROM, self.MAX_PRICE_TO
        )

        if best_total is None:
            logger.warning(
                f"⚠️ grow: нет ответа для {date_from}–{best_end}, окно = 1 день"
            )
            return date_from

        if best_total >= target or best_end >= hard_limit_end:
            return best_end

        while True:
            step_days *= 2
            candidate_end = min(date_from + timedelta(days=step_days), hard_limit_end)
            total = self._check_records(
                date_from, candidate_end, self.DEFAULT_PRICE_FROM, self.MAX_PRICE_TO
            )

            if total is None:
                return best_end

            if total > self.MAX_RECORDS_PER_WINDOW:
                return best_end

            best_end, best_total = candidate_end, total
            if total >= target or candidate_end >= hard_limit_end:
                return best_end

    def _generate_windows_aggregated(
        self,
    ) -> Generator[Tuple[str, str, int, Optional[int]], None, None]:
        """Один день = updateDate. С сегодня → START_DATE. При >4800 — split по цене."""
        today = datetime.now().date()
        start_limit = self.START_DATE

        logger.info(
            f"📦 Окна updateDate: {today.strftime('%d.%m.%Y')} ← "
            f"{start_limit.strftime('%d.%m.%Y')} (от новых к старым)..."
        )

        current = today
        while current >= start_limit:
            yield from self._split_window_recursive(
                current, current, self.DEFAULT_PRICE_FROM, self.MAX_PRICE_TO
            )
            current -= timedelta(days=1)

    def _split_window_recursive(
        self,
        date_from: date,
        date_to: date,
        price_from: int,
        price_to: Optional[int] = None,
    ) -> Generator[Tuple[str, str, int, Optional[int]], None, None]:
        total = self._check_records(date_from, date_to, price_from, price_to)

        if total is None:
            logger.error(
                f"❌ split: нет ответа ЕИС для {date_from}–{date_to}, окно пропущено"
            )
            return

        if total <= self.MAX_RECORDS_PER_WINDOW:
            yield (
                date_from.strftime("%d.%m.%Y"),
                date_to.strftime("%d.%m.%Y"),
                price_from,
                price_to,
            )
            return

        if date_from == date_to:
            logger.info(
                f"  ⚠️ {date_from}: {total} записей за день > лимита → дробим по цене"
            )
            yield from self._split_by_price(
                date_from, price_from, price_to or 1_000_000_000
            )
            return

        days_diff = (date_to - date_from).days
        mid_date = date_from + timedelta(days=days_diff // 2)
        logger.info(
            f"  ✂️ {date_from}-{date_to}: {total} записей > лимита → дробим дату пополам"
        )

        # сначала старая половина, потом новая
        yield from self._split_window_recursive(
            date_from, mid_date, price_from, price_to
        )
        yield from self._split_window_recursive(
            mid_date + timedelta(days=1), date_to, price_from, price_to
        )

    def _find_price_leaves(
        self, date_item: date, price_from: int, price_to: int
    ) -> List[Tuple[int, int, int]]:
        total_records = self._check_records(date_item, date_item, price_from, price_to)
        if total_records is None:
            return []
        if total_records <= self.MAX_RECORDS_PER_WINDOW:
            return [(price_from, price_to, total_records)]

        leaves: List[Tuple[int, int, int]] = []
        queue = deque([(price_from, price_to)])

        while queue:
            pf, pt = queue.popleft()
            records = self._check_records(date_item, date_item, pf, pt)
            if records is None:
                continue
            if records <= self.MAX_RECORDS_PER_WINDOW:
                leaves.append((pf, pt, records))
            elif (pt - pf) <= self.MIN_PRICE_RANGE:
                logger.warning(
                    f"  ⚠️ {date_item}: {pf:,}-{pt:,} = {records}, мин. ценовой диапазон"
                )
                leaves.append((pf, pt, records))
            else:
                mid = pf + (pt - pf) // 2
                queue.append((pf, mid))
                queue.append((mid, pt))

        leaves.sort(key=lambda x: x[0])
        return leaves

    def _merge_price_leaves(
        self, date_item: date, leaves: List[Tuple[int, int, int]]
    ) -> Generator[Tuple[str, str, int, Optional[int]], None, None]:
        if not leaves:
            return
        date_str = date_item.strftime("%d.%m.%Y")
        buf_pf, buf_pt, buf_records = leaves[0]
        merged_count = 1

        for pf, pt, records in leaves[1:]:
            if pf == buf_pt and buf_records + records <= self.MAX_RECORDS_PER_WINDOW:
                buf_pt = pt
                buf_records += records
                merged_count += 1
            else:
                if merged_count > 1:
                    logger.info(
                        f"  🔗 {date_item}: склеено {merged_count} ценовых окон → "
                        f"{buf_pf:,}-{buf_pt:,} = {buf_records}"
                    )
                yield (date_str, date_str, buf_pf, buf_pt)
                buf_pf, buf_pt, buf_records = pf, pt, records
                merged_count = 1

        if merged_count > 1:
            logger.info(
                f"  🔗 {date_item}: склеено {merged_count} ценовых окон → "
                f"{buf_pf:,}-{buf_pt:,} = {buf_records}"
            )
        yield (date_str, date_str, buf_pf, buf_pt)

    def _split_by_price(
        self, date_item: date, price_from: int, price_to: int
    ) -> Generator[Tuple[str, str, int, Optional[int]], None, None]:
        leaves = self._find_price_leaves(date_item, price_from, price_to)
        yield from self._merge_price_leaves(date_item, leaves)

    def _fetch_page_batch_multi_ip(
        self, urls: List[str], ip_pool: List[str]
    ) -> List[Tuple[Optional[Any], str]]:
        if not urls:
            return []

        if not ip_pool:
            plain = self._fetch_page_batch(urls, target_ip=None)
            return [(r, "unknown") for r in plain]

        # не больше 4 IP в пачке — меньше давление на WAF
        ip_pool = ip_pool[:4]
        n_ip = len(ip_pool)
        results: List[Tuple[Optional[Any], str]] = [(None, "unknown")] * len(urls)

        def fetch_one(idx: int, url: str, ip: str):
            resp = None
            for attempt in range(1, 4):
                resp = self._make_request_with_ip(
                    url, timeout=30, max_retries=2, target_ip=ip
                )[0]
                if resp is not None and getattr(resp, "status_code", None) == 200:
                    break
                backoff = (2**attempt) + random.uniform(0, 1.5)
                logger.warning(
                    f"⚠️ Стр.idx={idx} IP={ip} попытка {attempt}/3, сон {backoff:.1f}с"
                )
                time.sleep(backoff)
            return idx, resp, ip

        for batch_start in range(0, len(urls), n_ip):
            batch_urls = urls[batch_start : batch_start + n_ip]
            batch_num = batch_start // n_ip + 1
            logger.info(
                f"  ⚡ Пачка {batch_num}: страницы "
                f"{batch_start + 1}-{batch_start + len(batch_urls)} "
                f"({len(batch_urls)} IP)"
            )

            tasks = []
            for j, url in enumerate(batch_urls):
                idx = batch_start + j
                ip = ip_pool[j % n_ip]
                tasks.append((idx, url, ip))

            with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
                futures = [
                    executor.submit(fetch_one, idx, url, ip) for idx, url, ip in tasks
                ]
                for fut in as_completed(futures):
                    idx, resp, ip = fut.result()
                    results[idx] = (resp, ip)

            if batch_start + n_ip < len(urls):
                time.sleep(random.uniform(3.0, 6.0))

        return results

    def _fetch_page_batch(
        self, urls: List[str], batch_size: int = 3, target_ip: Optional[str] = None
    ) -> List[Optional[Any]]:
        results = []
        for i, url in enumerate(urls):
            if i > 0:
                if i % 20 == 0:
                    cooldown = random.uniform(12.0, 20.0)
                    logger.info(
                        f"   ❄️ Плановое остывание после {i} страниц: {cooldown:.1f}с"
                    )
                    time.sleep(cooldown)
                else:
                    time.sleep(random.uniform(1.8, 3.5))

            resp = None
            for attempt in range(1, 4):
                resp = self._make_request_with_ip(
                    url, timeout=30, max_retries=2, target_ip=target_ip
                )[0]
                if resp is not None and resp.status_code == 200:
                    break
                backoff = (2**attempt) + random.uniform(0, 2)
                logger.warning(
                    f"⚠️ Страница {i+1} не загружена ({attempt}/3). "
                    f"Повтор через {backoff:.1f}с..."
                )
                time.sleep(backoff)
            results.append(resp)
        return results

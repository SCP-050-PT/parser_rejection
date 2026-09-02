"""
Обработка контрактов и парсинг отказов.
VK версия — убраны Telegram-зависимости.
Добавлено: парсинг карточки контракта, отправка писем поставщикам.

ИСПРАВЛЕНИЕ 2026-06-17:
- Парсинг customer_name (заказчик) из common-info.html
- Передача customer_name в email_sender (вместо supplier_name)
"""

import time
import random
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import datetime
from typing import List, Dict, Optional, Any
from loguru import logger
from config.settings import settings
from bs4 import BeautifulSoup
import re

from src.rejection_parser.utils import run_async_in_thread
from src.rejection_parser.contract_card_parser import get_contract_card_parser
from src.rejection_parser.email_sender import get_email_sender


class ContractProcessor:
    """Обработка контрактов: парсинг страниц, проверка дат, сохранение, отправка писем"""

    REJECTION_KEYWORD = "Решение об одностороннем отказе"

    def __init__(
        self,
        excel_manager,
        rejections_cache,
        session_manager,
        admin_vk_id: Optional[str] = None,
    ):
        self.excel_manager = excel_manager
        self.rejections_cache = rejections_cache
        self.session_manager = session_manager
        self.admin_vk_id = admin_vk_id
        self._lock = Lock()
        self.session_processed = 0
        self.session_found = 0
        self.found_rejections: List[Dict] = []
        self._save_counter = 0
        self._file_created_on_disk = False

        # Инициализация парсера карточки и отправщика писем
        self.card_parser = get_contract_card_parser(session_manager)
        self.email_sender = get_email_sender()

    def _parse_customer_name(self, html_content: str) -> str:
        """
        Парсит название ЗАКАЗЧИКА из HTML карточки контракта (common-info.html).
        Ищет блок с классом cardMainInfo__title содержащий "Заказчик".
        """
        try:
            soup = BeautifulSoup(html_content, "lxml")

            # Вариант 1: Ищем div с заголовком "Заказчик"
            for section in soup.find_all("div", class_="cardMainInfo__section"):
                title_span = section.find("span", class_="cardMainInfo__title")
                if title_span and "Заказчик" in title_span.get_text(strip=True):
                    content_span = section.find("span", class_="cardMainInfo__content")
                    if content_span:
                        # Ищем ссылку или текст
                        link = content_span.find("a")
                        if link:
                            name = link.get_text(strip=True)
                            if name and len(name) > 5:
                                return name
                        # Если ссылки нет — берём весь текст
                        text = content_span.get_text(strip=True)
                        if text and len(text) > 5:
                            return text

            # Вариант 2: Ищем по тексту "Заказчик" в любом span
            for span in soup.find_all("span", class_="cardMainInfo__title"):
                if "Заказчик" in span.get_text(strip=True):
                    parent = span.find_parent("div", class_="cardMainInfo__section")
                    if parent:
                        content = parent.find("span", class_="cardMainInfo__content")
                        if content:
                            link = content.find("a")
                            if link:
                                name = link.get_text(strip=True)
                                if name and len(name) > 5:
                                    return name
                            text = content.get_text(strip=True)
                            if text and len(text) > 5:
                                return text

            # Вариант 3: Ищем в sectionMainInfo (как в твоём HTML)
            for div in soup.find_all("div", class_="sectionMainInfo"):
                title = div.find("span", class_="cardMainInfo__title")
                if title and "Заказчик" in title.get_text(strip=True):
                    content = div.find("span", class_="cardMainInfo__content")
                    if content:
                        link = content.find("a")
                        if link:
                            name = link.get_text(strip=True)
                            if name and len(name) > 5:
                                return name
                        text = content.get_text(strip=True)
                        if text and len(text) > 5:
                            return text

            # Вариант 4: Ищем по заголовку h2 "Сведения о заказчике"
            for h2 in soup.find_all("h2", class_="blockInfo__title"):
                if "заказчик" in h2.get_text(strip=True).lower():
                    parent = h2.find_parent("div", class_="blockInfo")
                    if parent:
                        # Ищем первую ссылку на организацию
                        link = parent.find("a", href=re.compile(r"organization", re.I))
                        if link:
                            name = link.get_text(strip=True)
                            if name and len(name) > 5:
                                return name

            logger.warning("   ⚠️ Заказчик не найден в HTML")
            return ""

        except Exception as e:
            logger.error(f"   ❌ Ошибка парсинга заказчика: {e}")
            return ""

    def _parse_search_page(self, html_content: str) -> List[Dict]:
        soup = BeautifulSoup(html_content, "lxml")
        all_entries = soup.find_all(
            "div", class_=re.compile(r"search-registry-entry-block")
        )
        logger.info(f"  🔍 Всего карточек: {len(all_entries)}")

        reject_links = soup.find_all("a", href=re.compile(r"reject-info\.html"))
        logger.info(f"  🔍 Ссылок reject-info: {len(reject_links)}")

        contracts = []
        seen_reestr = set()

        for link in reject_links:
            href = link.get("href", "")
            match = re.search(r"contractRegNum=(\d+)", href)
            if not match:
                continue

            reestr_number = match.group(1)
            if reestr_number in seen_reestr:
                continue
            seen_reestr.add(reestr_number)

            contract_link = soup.find(
                "a",
                href=re.compile(rf"common-info\.html\?reestrNumber={reestr_number}"),
            )
            contract_number = (
                contract_link.get_text(strip=True).replace("№", "").strip()
                if contract_link
                else reestr_number
            )

            reject_url = (
                f"https://zakupki.gov.ru{href}" if href.startswith("/") else href
            )
            contracts.append(
                {
                    "reestr_number": reestr_number,
                    "contract_number": contract_number,
                    "reject_url": reject_url,
                }
            )

        return contracts

    def _parse_reject_date(self, html_content: str) -> Optional[str]:
        try:
            soup = BeautifulSoup(html_content, "lxml")
            attachments = soup.find_all("section", class_="attachment")

            for attachment in attachments:
                title_section = attachment.find("section", class_="littleBlack pb-3")
                if not title_section:
                    continue

                title_text = title_section.get_text(strip=True)
                if (
                    self.REJECTION_KEYWORD in title_text
                    and "Извещение об отмене" not in title_text
                ):
                    match = re.search(r"от\s+(\d{2}\.\d{2}\.\d{4})", title_text)
                    if match:
                        return match.group(1)

                    rows = attachment.find_all("section", class_="col-6")
                    for row in rows:
                        title = row.find("section", class_="section__title")
                        if title and "Поставщик" in title.get_text():
                            date_value = row.find("section", class_="attachment__value")
                            if date_value:
                                date_text = date_value.get_text(strip=True)
                                match = re.search(
                                    r"(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})", date_text
                                )
                                if match:
                                    return match.group(1)
                    break
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга даты отказа: {e}")
            return None

    def _send_email_to_supplier(
        self,
        reestr_number: str,
        contract_number: str,
        customer_name: str,  # ← НОВОЕ: заказчик
        supplier_name: str,  # ← поставщик
        email: str,
    ) -> Dict[str, Any]:
        """
        Отправляет письмо поставщику при отказе.
        customer_name = ЗАКАЗЧИК (подставляется в шаблон)
        supplier_name = ПОСТАВЩИК (для логов)
        Returns: {"sent": bool, "status": str, "details": str}
        """
        if not email or "@" not in email:
            return {"sent": False, "status": "no_email", "details": "Email не найден"}

        # Формируем URL заказчика
        customer_url = (
            f"https://zakupki.gov.ru/epz/organization/view/info.html?organizationCode="
        )

        # Отправляем письмо
        # customer_name = ЗАКАЗЧИК (в шаблон)
        # supplier_name = ПОСТАВЩИК (в лог)
        result = self.email_sender.send(
            to_email=email,
            contract_number=reestr_number,
            customer_name=customer_name,  # ← ЗАКАЗЧИК в шаблон!
            customer_url=customer_url,
            supplier_name=supplier_name,  # ← ПОСТАВЩИК в лог
        )

        return {
            "sent": result["success"],
            "status": result["status"],
            "details": result["details"],
        }

    def _check_reject_date_inline(self, contract: Dict, max_workers: int = 1) -> int:
        return self._check_reject_date_inline_with_session(
            contract, self.session_manager.session, max_workers
        )

    def _check_reject_date_inline_with_session(
        self, contract: Dict, session, max_workers: int = 1
    ) -> int:
        reestr_number = contract["reestr_number"]
        contract_number = contract["contract_number"]
        reject_url = contract["reject_url"]

        try:
            if self.rejections_cache.is_processed(reestr_number):
                logger.info(f"  ⏭ [{contract_number}] уже в кэше")
                return 0

            # Уменьшенная задержка перед запросом
            time.sleep(0.1 + random.uniform(0, 0.2))

            response = self.session_manager.make_request_fast(reject_url, session)

            with self._lock:
                self.session_processed += 1

            if response is None:
                logger.warning(f"  ⚠️ [{contract_number}] Нет ответа от reject-info")
                return 0
            if response.status_code != 200:
                logger.warning(
                    f"  ⚠️ [{contract_number}] Статус {response.status_code}"
                )
                return 0

            reject_date = self._parse_reject_date(response.text)
            if not reject_date:
                logger.info(f"  🔍 [{contract_number}] — Дата не найдена")
                return 0

            try:
                date_part = reject_date.split()[0]
                reject_day = datetime.strptime(date_part, "%d.%m.%Y").date()
                today = datetime.now().date()

                if reject_day != today:
                    self.rejections_cache.add(
                        reestr_number=reestr_number,
                        contract_number=contract_number,
                        reject_date=reject_date,
                        reject_url=reject_url,
                    )
                    logger.info(
                        f"  ⏭ [{contract_number}] — Отказ от {date_part}, не сегодня (в кэш)"
                    )
                    return 0
            except Exception as e:
                logger.debug(
                    f"  🔍 [{contract_number}] — Не удалось сравнить дату: {e}"
                )
                return 0

            # === НАЙДЕН СЕГОДНЯШНИЙ ОТКАЗ ===
            found_date = datetime.now().strftime("%d.%m.%Y %H:%M")

            # === ПАРСИМ КАРТОЧКУ КОНТРАКТА ===
            logger.info(f"  📄 [{contract_number}] Парсим карточку контракта...")

            # 1. Загружаем common-info.html для получения заказчика
            common_info_url = f"https://zakupki.gov.ru/epz/contract/contractCard/common-info.html?reestrNumber={reestr_number}"
            customer_name = ""
            try:
                common_response = self.session_manager.make_request_fast(common_info_url, session)
                if common_response and common_response.status_code == 200:
                    customer_name = self._parse_customer_name(common_response.text)
                    if customer_name:
                        logger.info(f"  🏛️ [{contract_number}] Заказчик: {customer_name[:80]}...")
                    else:
                        logger.warning(f"  ⚠️ [{contract_number}] Заказчик не найден в common-info")
                else:
                    logger.warning(f"  ⚠️ [{contract_number}] Не удалось загрузить common-info")
            except Exception as e:
                logger.error(f"  ❌ [{contract_number}] Ошибка загрузки common-info: {e}")

            # 2. Парсим данные поставщика (email, название)
            card_data = self.card_parser.parse(reestr_number)
            supplier_name = card_data.supplier_name
            email = card_data.email

            logger.info(
                f"  📋 [{contract_number}] Поставщик: {supplier_name[:50] if supplier_name else 'не найден'}... | "
                f"Email: {email or 'не найден'}"
            )

            # Если заказчик не найден — используем поставщика как fallback
            if not customer_name:
                customer_name = supplier_name
                logger.warning(f"  ⚠️ [{contract_number}] Заказчик не найден, используем поставщика как fallback")

            # Отправляем письмо поставщику (если email найден)
            email_result = {"sent": False, "status": "not_attempted", "details": ""}
            if email:
                email_result = self._send_email_to_supplier(
                    reestr_number=reestr_number,
                    contract_number=contract_number,
                    customer_name=customer_name,  # ← ЗАКАЗЧИК!
                    supplier_name=supplier_name,  # ← ПОСТАВЩИК
                    email=email,
                )
                logger.info(
                    f"  📧 [{contract_number}] Email: {email_result['status']} | "
                    f"{email_result['details'][:60] if email_result['details'] else 'OK'}"
                )
            else:
                logger.info(
                    f"  ⏭ [{contract_number}] Email не найден, письмо не отправлено"
                )

            rejection_data = {
                "contract_number": contract_number,
                "reestr_number": reestr_number,
                "reject_info_url": reject_url,
                "reject_date": reject_date,
                "found_date": found_date,
                "customer_name": customer_name,  # ← ЗАКАЗЧИК
                "supplier_name": supplier_name,  # ← ПОСТАВЩИК
                "email": email,
                "email_sent": email_result["sent"],
                "email_status": email_result["status"],
            }

            with self._lock:
                self.found_rejections.append(rejection_data)

            is_new = self.rejections_cache.add(
                reestr_number=reestr_number,
                contract_number=contract_number,
                reject_date=reject_date,
                reject_url=reject_url,
                customer_name=customer_name,  # ← Сохраняем заказчика
                supplier_name=supplier_name,
                email=email,
                email_sent=email_result["sent"],
                email_status=email_result["status"],
            )

            with self._lock:
                self.session_found += 1

            logger.info(
                f"  🚨 [{contract_number}] ✅ ОТКАЗ НАЙДЕН! Дата: {reject_date}"
            )

            # Уведомление через очередь (VK версия)
            if is_new and self.admin_vk_id:
                try:
                    from bot.user_manager import _notification_queue

                    _notification_queue.put_nowait(
                        {
                            "contract_number": contract_number,
                            "reject_url": reject_url,
                            "reject_date": reject_date,
                            "admin_chat_id": self.admin_vk_id,
                            "customer_name": customer_name,
                            "supplier_name": supplier_name,
                            "email": email,
                            "email_sent": email_result["sent"],
                            "email_status": email_result["status"],
                        }
                    )
                    logger.info(f"  📨 Уведомление в очереди: {contract_number}")
                except Exception as e:
                    logger.error(f"  ❌ Ошибка очереди: {e}")

            # Сохранение в Excel
            self._save_to_excel(
                contract_number=contract_number,
                reject_url=reject_url,
                reject_date=reject_date,
                customer_name=customer_name,
                supplier_name=supplier_name,
                email_sent=email_result["sent"],
                email_status=email_result["status"],
            )

            return 1 if is_new else 0

        except Exception as e:
            logger.error(f"  ❌ [{contract_number}] Ошибка: {e}")
            return 0

    def _process_contracts_parallel(
        self, contracts: List[Dict], max_workers: int = 1
    ) -> List[Dict]:
        if not contracts:
            return []

        today_contracts = []
        actual_workers = min(max_workers, len(contracts))
        logger.info(
            f"⚡ Параллельная обработка: {len(contracts)} контрактов, {actual_workers} потоков"
        )

        args = [
            (c, self.session_manager.get_session(i)) for i, c in enumerate(contracts)
        ]

        def process_one(args_item):
            contract, session = args_item
            return self._check_reject_date_inline_with_session(
                contract, session, max_workers
            )

        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
            future_to_contract = {executor.submit(process_one, a): a[0] for a in args}
            for future in as_completed(future_to_contract):
                contract = future_to_contract[future]
                try:
                    is_today = future.result(timeout=30)
                    if is_today:
                        today_contracts.append(contract)
                        logger.info(
                            f"  🚨 +1 СЕГОДНЯШНИЙ отказ: {contract['contract_number']}, всего: {len(today_contracts)}"
                        )
                except Exception as e:
                    logger.error(
                        f"  ❌ Ошибка обработки {contract['contract_number']}: {e}"
                    )

        self._flush_page_to_disk()
        return today_contracts

    def _process_contracts_sequential(
        self, contracts: List[Dict], max_workers: int = 1
    ) -> List[Dict]:
        today_contracts = []
        for contract in contracts:
            is_today = self._check_reject_date_inline(contract, max_workers)
            if is_today:
                today_contracts.append(contract)
                logger.info(
                    f"  🚨 +1 СЕГОДНЯШНИЙ отказ: {contract['contract_number']}, всего: {len(today_contracts)}"
                )
        self._flush_page_to_disk()
        return today_contracts

    def _save_to_excel(
        self,
        contract_number: str,
        reject_url: str,
        reject_date: str,
        customer_name: str = "",
        supplier_name: str = "",
        email_sent: bool = False,
        email_status: str = "",
    ):
        try:
            sent_str = "Да" if email_sent else "Нет"
            if not email_sent and email_status:
                sent_str = f"Нет ({email_status})"

            record = {
                "Номер контракта": contract_number,
                "Ссылка на отказ": reject_url,
                "Новый": "Да",
                "Дата подписания заказчиком": reject_date,
                "Заказчик": customer_name,
                "Поставщик": supplier_name,
                "Отправлено": sent_str,
            }
            result = self.excel_manager.add_record(record)
            if result:
                with self._lock:
                    self._save_counter += 1
                logger.info(
                    f"  💾 [{contract_number}] Запись добавлена в Excel (буфер: {self._save_counter})"
                )
            else:
                logger.debug(f"  ⏭ [{contract_number}] Пропущен (дубликат/фильтр)")
        except Exception as e:
            logger.error(f"  ❌ [{contract_number}] Excel ошибка: {e}")

    def _flush_page_to_disk(self):
        with self._lock:
            counter = self._save_counter
            if counter == 0:
                return
            self._save_counter = 0

        logger.info(f"💾 Сохраняем {counter} записей на Я.Диск...")
        try:
            public_url = run_async_in_thread(self.excel_manager.force_save())
            if public_url:
                self._file_created_on_disk = True
                logger.info(f"✅ Сохранено на Я.Диск: {public_url[:50]}...")
            else:
                logger.warning(f"⚠️ force_save вернул None")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения на Я.Диск: {e}")

    def search_contracts_with_rejections(
        self,
        search_manager,
        contract_date_from: Optional[str] = None,
        contract_date_to: Optional[str] = None,
        price_from: Optional[int] = None,
        price_to: Optional[int] = None,
        max_pages: int = 100,
        max_workers: int = 1,
    ) -> List[Dict]:
        today_contracts = []
        seen_reestr = set()
        empty_contract_pages = 0
        consecutive_errors = 0

        price_label = f" 💰 {price_from:,}-{price_to or '∞'}" if price_from else ""
        window_label = f" 📅 {contract_date_from} – {contract_date_to}{price_label}"

        first_url = search_manager._build_search_url(
            page=1,
            contract_date_from=contract_date_from,
            contract_date_to=contract_date_to,
            price_from=price_from,
            price_to=price_to,
        )
        first_response = search_manager._make_request(first_url)
        if first_response is None:
            logger.error(f"⏹ Не удалось загрузить 1-ю страницу{window_label}")
            return today_contracts
        if "captcha" in first_response.text.lower():
            logger.error("🤖 CAPTCHA!")
            return today_contracts

        total_records = search_manager._parse_total_records(first_response.text)
        if total_records > 0:
            records_per_page = int(
                getattr(settings, "records_per_page", "50").replace("_", "")
            )
            total_pages = min(
                (total_records + records_per_page - 1) // records_per_page, max_pages
            )
            logger.info(
                f"  📊 Всего записей: {total_records}, страниц: {total_pages}{window_label}"
            )
        else:
            total_pages = max_pages
            logger.info(
                f"  📊 Не удалось определить количество, лимит {max_pages} стр.{window_label}"
            )

        if "reject-info" not in first_response.text:
            soup = BeautifulSoup(first_response.text, "lxml")
            all_entries = soup.find_all(
                "div", class_=re.compile(r"search-registry-entry-block")
            )
            if len(all_entries) == 0:
                empty_contract_pages = 1
                logger.info(f"  ⏭ Пустая страница (1/3){window_label}")
            else:
                logger.info(
                    f"  ⏭ Страница без отказов ({len(all_entries)} контрактов){window_label}"
                )
        else:
            contracts = self._parse_search_page(first_response.text)
            for c in contracts:
                if c["reestr_number"] not in seen_reestr:
                    seen_reestr.add(c["reestr_number"])
            if max_workers > 1 and len(contracts) > 1:
                page_contracts = self._process_contracts_parallel(
                    contracts, max_workers
                )
            else:
                page_contracts = self._process_contracts_sequential(
                    contracts, max_workers
                )
            today_contracts.extend(page_contracts)
            if page_contracts:
                empty_contract_pages = 0

        if total_pages == 1:
            logger.info(
                f"✅ Поиск завершён{window_label}: {len(today_contracts)} СЕГОДНЯШНИХ отказов"
            )
            return today_contracts

        remaining_urls = []
        for page_num in range(2, total_pages + 1):
            url = search_manager._build_search_url(
                page=page_num,
                contract_date_from=contract_date_from,
                contract_date_to=contract_date_to,
                price_from=price_from,
                price_to=price_to,
            )
            remaining_urls.append(url)

        batch_size = getattr(settings, "search_page_batch_size", 3)
        responses = search_manager._fetch_page_batch(
            remaining_urls, batch_size=batch_size
        )

        for page_idx, response in enumerate(responses, start=2):
            if response is None:
                consecutive_errors += 1
                logger.warning(
                    f"  ⚠️ Страница {page_idx} не загружена ({consecutive_errors}/3)"
                )
                if consecutive_errors >= 3:
                    logger.error(f"⏹ 3 ошибки подряд, прерываем{window_label}")
                    break
                continue

            consecutive_errors = 0
            if "reject-info" not in response.text:
                soup = BeautifulSoup(response.text, "lxml")
                all_entries = soup.find_all(
                    "div", class_=re.compile(r"search-registry-entry-block")
                )
                if len(all_entries) == 0:
                    empty_contract_pages += 1
                    logger.info(
                        f"  ⏭ Пустая страница ({empty_contract_pages}/3) на стр.{page_idx}{window_label}"
                    )
                    if empty_contract_pages >= 3:
                        logger.info(f"  ⏹ 3 пустые страницы, завершаем{window_label}")
                        break
                else:
                    empty_contract_pages = 0
                    logger.info(
                        f"  ⏭ Страница {page_idx} без отказов ({len(all_entries)} контрактов){window_label}"
                    )
                continue

            contracts = self._parse_search_page(response.text)
            new_contracts = [
                c for c in contracts if c["reestr_number"] not in seen_reestr
            ]
            for c in new_contracts:
                seen_reestr.add(c["reestr_number"])

            if new_contracts:
                if max_workers > 1 and len(new_contracts) > 1:
                    page_contracts = self._process_contracts_parallel(
                        new_contracts, max_workers
                    )
                else:
                    page_contracts = self._process_contracts_sequential(
                        new_contracts, max_workers
                    )
                today_contracts.extend(page_contracts)
                if page_contracts:
                    empty_contract_pages = 0

        logger.info(
            f"✅ Поиск завершён{window_label}: {len(today_contracts)} СЕГОДНЯШНИХ отказов"
        )
        return today_contracts
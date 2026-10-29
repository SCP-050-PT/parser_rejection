"""
processing.py
Обработка контрактов и парсинг отказов.
VK версия v7.7 Adapted for Arina (rnp-help.ru).
ИСПРАВЛЕНИЕ v7.7: Фоновая очередь email, Circuit Breaker, Graceful Shutdown.
Сохранена структура Excel-отчета Арины (ИНН, Телефон, Цена).
"""

import time
import random
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Event
from datetime import datetime
from typing import List, Dict, Optional, Any
from loguru import logger
from config.settings import settings
from bs4 import BeautifulSoup
import re
import os
from src.rejection_parser.utils import run_async_in_thread
from src.rejection_parser.contract_card_parser import get_contract_card_parser
from src.rejection_parser.email_sender import get_email_sender

REJECT_MARKERS = (
    "reject-info.html",
    "Решение об одностороннем отказе",
    "решение об одностороннем отказе",
    "одностороннем отказе",
    "reject-info",
)


class ContractProcessor:
    """Обработка контрактов: парсинг страниц, проверка дат, сохранение, отправка писем"""

    REJECTION_KEYWORD = "Решение об одностороннем отказе"
    PENDING_EMAILS_FILE = "pending_emails_arina.json"
    CIRCUIT_BREAKER_THRESHOLD = 3
    CIRCUIT_BREAKER_COOLDOWN = 600

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

        self.card_parser = get_contract_card_parser(session_manager)
        self.email_sender = get_email_sender()

        # === ФОНОВАЯ ОЧЕРЕДЬ EMAIL ===
        self._email_queue: "queue.Queue" = queue.Queue()
        self._email_worker_thread: Optional[threading.Thread] = None
        self._shutdown_event = Event()
        self._email_circuit_breaker = {
            "failures": 0,
            "last_failure_time": 0,
            "state": "closed",
        }

        self._start_email_worker()
        self._load_pending_emails()

    def _start_email_worker(self):
        if (
            self._email_worker_thread is None
            or not self._email_worker_thread.is_alive()
        ):
            self._email_worker_thread = threading.Thread(
                target=self._email_worker_loop,
                daemon=True,
                name="email-sender-worker-arina",
            )
            self._email_worker_thread.start()
            logger.info("📧 Фоновый поток отправки писем (Arina) запущен")

    def _email_worker_loop(self):
        while not self._shutdown_event.is_set():
            try:
                task = self._email_queue.get(timeout=1.0)
                if task is None:
                    break

                if self._email_circuit_breaker["state"] == "open":
                    if (
                        time.time() - self._email_circuit_breaker["last_failure_time"]
                        > self.CIRCUIT_BREAKER_COOLDOWN
                    ):
                        self._email_circuit_breaker["state"] = "half-open"
                    else:
                        self._email_queue.put(task)
                        time.sleep(5)
                        continue

                success = self._process_email_task(task)
                if success:
                    self._email_circuit_breaker["failures"] = 0
                    self._email_circuit_breaker["state"] = "closed"
                else:
                    self._email_circuit_breaker["failures"] += 1
                    self._email_circuit_breaker["last_failure_time"] = time.time()
                    if (
                        self._email_circuit_breaker["failures"]
                        >= self.CIRCUIT_BREAKER_THRESHOLD
                    ):
                        self._email_circuit_breaker["state"] = "open"
                        logger.error(
                            f"📧 Circuit Breaker АКТИВИРОВАН. Пауза {self.CIRCUIT_BREAKER_COOLDOWN}с"
                        )

                self._email_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"📧 Ошибка в потоке отправки писем: {e}")
                time.sleep(5)
        logger.info("📧 Фоновый поток отправки писем (Arina) остановлен")

    def _process_email_task(self, task: Dict) -> bool:
        try:
            reestr_number = task["reestr_number"]
            contract_number = task["contract_number"]
            customer_name = task["customer_name"]
            supplier_name = task["supplier_name"]
            email = task["email"]
            reject_url = task["reject_url"]
            reject_date = task["reject_date"]

            # Для Арины email отправляется упрощенно, но можно расширить
            logger.info(f"  📧 [{contract_number}] Отправка письма из очереди...")
            email_result = self._send_email_to_supplier(
                reestr_number=reestr_number,
                supplier_name=supplier_name,
                email=email,
            )

            self.rejections_cache.add(
                reestr_number=reestr_number,
                contract_number=contract_number,
                reject_date=reject_date,
                reject_url=reject_url,
                supplier_name=supplier_name,
                email=email,
                email_sent=email_result["sent"],
                email_status=email_result["status"],
            )

            # Сохраняем в Excel с полной информацией Арины
            self._save_to_excel(
                reestr_number=reestr_number,
                reject_url=reject_url,
                reject_date=reject_date,
                supplier_name=supplier_name,
                email=email,
                inn=task.get("inn", ""),
                phone=task.get("phone", ""),
                price=task.get("price", ""),
                region=task.get("region", ""),
                customer_name=customer_name,
                contract_date=task.get("contract_date", ""),
                email_sent=email_result["sent"],
                email_status=email_result["status"],
            )

            logger.info(f"  📧 [{contract_number}] Email: {email_result['status']}")
            return email_result["sent"]
        except Exception as e:
            logger.error(
                f"  ❌ Ошибка отправки письма для {task.get('contract_number', 'unknown')}: {e}"
            )
            return False

    def _load_pending_emails(self):
        if not os.path.exists(self.PENDING_EMAILS_FILE):
            return
        try:
            with open(self.PENDING_EMAILS_FILE, "r", encoding="utf-8") as f:
                pending = json.load(f)
            for task in pending:
                self._email_queue.put(task)
            if pending:
                logger.info(
                    f"📧 Загружено {len(pending)} несент-писем из прошлого запуска"
                )
            os.remove(self.PENDING_EMAILS_FILE)
        except Exception as e:
            logger.error(f"Ошибка загрузки pending emails: {e}")

    def _save_pending_emails(self):
        pending = []
        try:
            while True:
                task = self._email_queue.get_nowait()
                if task is not None:
                    pending.append(task)
                self._email_queue.task_done()
        except queue.Empty:
            pass

        if pending:
            try:
                with open(self.PENDING_EMAILS_FILE, "w", encoding="utf-8") as f:
                    json.dump(pending, f, ensure_ascii=False, indent=2)
                logger.info(
                    f"📧 Сохранено {len(pending)} несент-писем для следующего запуска"
                )
            except Exception as e:
                logger.error(f"Ошибка сохранения pending emails: {e}")

    def shutdown(self):
        logger.info("📧 Инициализация shutdown для email worker (Arina)...")
        self._shutdown_event.set()
        if self._email_worker_thread and self._email_worker_thread.is_alive():
            self._email_worker_thread.join(timeout=30)
        self._save_pending_emails()

    def _parse_customer_name(self, html_content: str) -> str:
        try:
            soup = BeautifulSoup(html_content, "lxml")
            for section in soup.find_all("div", class_="cardMainInfo__section"):
                title_span = section.find("span", class_="cardMainInfo__title")
                if title_span and "Заказчик" in title_span.get_text(strip=True):
                    content_span = section.find("span", class_="cardMainInfo__content")
                    if content_span:
                        link = content_span.find("a")
                        if link:
                            name = link.get_text(strip=True)
                            if name and len(name) > 5:
                                return name
                        text = content_span.get_text(strip=True)
                        if text and len(text) > 5:
                            return text
            return ""
        except Exception as e:
            logger.error(f"   ❌ Ошибка парсинга заказчика: {e}")
            return ""

    def _parse_search_page(self, html_content: str) -> List[Dict]:
        if not self._page_has_reject(html_content):
            return []

        pattern = r"/epz/rdik/card/reject-info\.html\?contractRegNum=(\d+)"
        found_ids = re.findall(pattern, html_content)
        if not found_ids:
            found_ids = re.findall(
                r"reject-info\.html\?contractRegNum=(\d+)", html_content
            )

        if not found_ids:
            logger.warning("⚠️ 'reject-info' в тексте есть, но ID не извлечён regex")
            return []

        contracts = []
        seen = set()
        for rid in found_ids:
            if rid in seen:
                continue
            seen.add(rid)
            contracts.append(
                {
                    "reestr_number": rid,
                    "contract_number": rid,  # точный номер подтянется из карточки
                    "reject_url": (
                        f"https://zakupki.gov.ru/epz/rdik/card/reject-info.html"
                        f"?contractRegNum={rid}"
                    ),
                }
            )
        return contracts

    def _page_has_reject(self, html: str) -> bool:
        if not html:
            return False
        low = html.lower()
        return any(m in low for m in REJECT_MARKERS)

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
                    date_match = re.search(r"от\s+(\d{2}\.\d{2}\.\d{4})", title_text)
                    if date_match:
                        return date_match.group(1)
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга даты отказа: {e}")
            return None

    def _send_email_to_supplier(
        self, reestr_number: str, supplier_name: str, email: str
    ) -> Dict[str, Any]:
        if not email or "@" not in email:
            return {"sent": False, "status": "no_email", "details": "Email не найден"}

        result = self.email_sender.send(to_email=email, contract_number=reestr_number)
        return {
            "sent": result["success"],
            "status": result["status"],
            "details": result["details"],
        }

    def _check_reject_date_inline_with_session(
        self,
        contract: Dict,
        session,
        max_workers: int = 1,
        current_ip: Optional[str] = None,
    ) -> int:
        reestr_number = contract["reestr_number"]
        contract_number = contract["contract_number"]
        reject_url = contract["reject_url"]

        t_start = time.time()
        try:
            if self.rejections_cache.is_processed(reestr_number):
                logger.debug(f"  ⏭ [{contract_number}] уже в кэше")
                return 0

            time.sleep(random.uniform(1.2, 2.5))

            response = self.session_manager.make_request_fast(
                reject_url, session, current_ip=current_ip or "unknown"
            )
            with self._lock:
                self.session_processed += 1

            if response is None or response.status_code != 200:
                return 0

            reject_date = self._parse_reject_date(response.text)
            if not reject_date:
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
                    return 0
            except Exception:
                return 0

            # === НАЙДЕН СЕГОДНЯШНИЙ ОТКАЗ ===
            logger.info(
                f"  🚨 [{contract_number}] ✅ ОТКАЗ СЕГОДНЯ! Начинаю сбор данных..."
            )

            # 1. Запрос common-info для Заказчика
            common_info_url = (
                "https://zakupki.gov.ru/epz/contract/contractCard/"
                f"common-info.html?reestrNumber={reestr_number}"
            )
            customer_name = ""
            registry_placed = ""
            registry_updated = ""
            try:
                common_response = self.session_manager.make_request_fast(
                    common_info_url, session, current_ip=current_ip or "unknown"
                )
                if common_response and common_response.status_code == 200:
                    customer_name = self._parse_customer_name(common_response.text)
                    registry_placed, registry_updated = self._parse_registry_dates(
                        common_response.text
                    )
                    if registry_placed or registry_updated:
                        logger.info(
                            f"  📅 [{contract_number}] реестр: "
                            f"размещен={registry_placed or '—'}, "
                            f"обновлен={registry_updated or '—'}"
                        )
            except Exception as e:
                logger.error(f"  ❌ [{contract_number}] Ошибка common-info: {e}")

            # 2. Парсинг карточки через CardParser (Поставщик + Email + ИНН + Телефон)
            card_data = self.card_parser.parse(reestr_number)
            supplier_name = card_data.supplier_name
            email = card_data.email
            inn = card_data.inn
            phone = card_data.phone
            price = card_data.price
            region = card_data.region
            contract_date = card_data.contract_date

            if not customer_name:
                customer_name = supplier_name or "Неизвестный"

            # Добавляем в кэш и очередь писем
            is_new = self.rejections_cache.add(
                reestr_number=reestr_number,
                contract_number=contract_number,
                reject_date=reject_date,
                reject_url=reject_url,
                supplier_name=supplier_name,
                email=email,
                email_sent=False,
                email_status="queued",
            )

            with self._lock:
                self.session_found += 1

            if email:
                email_task = {
                    "reestr_number": reestr_number,
                    "contract_number": contract_number,
                    "customer_name": customer_name,
                    "supplier_name": supplier_name,
                    "email": email,
                    "reject_url": reject_url,
                    "reject_date": reject_date,
                    "inn": inn,
                    "phone": phone,
                    "price": price,
                    "region": region,
                    "contract_date": contract_date,
                }
                self._email_queue.put(email_task)
                logger.info(f"  📧 [{contract_number}] Письмо в очереди")
            else:
                # Если email нет, сразу сохраняем в Excel как "не отправлено"
                self._save_to_excel(
                    reestr_number=reestr_number,
                    reject_url=reject_url,
                    reject_date=reject_date,
                    supplier_name=supplier_name,
                    email="",
                    inn=inn,
                    phone=phone,
                    price=price,
                    region=region,
                    customer_name=customer_name,
                    contract_date=contract_date,
                    email_sent=False,
                    email_status="no_email",
                )

            if is_new and self.admin_vk_id:
                try:
                    from bot.user_manager import _notification_queue

                    _notification_queue.put_nowait(
                        {
                            "contract_number": contract_number,
                            "reject_url": reject_url,
                            "reject_date": reject_date,
                            "admin_chat_id": self.admin_vk_id,
                            "supplier_name": supplier_name,
                            "email": email,
                            "email_sent": False,
                            "email_status": "queued",
                            # для VK:
                            "reestr_number": reestr_number,
                            "contract_date": contract_date or "",
                            "customer_name": customer_name or "",
                            "price": price or "",
                            "region": region or "",
                            "registry_placed": registry_placed or "",
                            "registry_updated": registry_updated or "",
                        }
                    )
                except Exception as e:
                    logger.error(f"  ❌ Ошибка очереди уведомлений: {e}")

            return 1 if is_new else 0
        except Exception as e:
            logger.error(f"  ❌ [{contract_number}] Ошибка: {e}")
            return 0

    def _parse_registry_dates(self, html_content: str) -> tuple:
        """Размещен / Обновлен контракт в реестре контрактов."""
        placed, updated = "", ""
        try:
            soup = BeautifulSoup(html_content, "lxml")
            for section in soup.find_all("div", class_="cardMainInfo__section"):
                title_el = section.find("span", class_="cardMainInfo__title")
                content_el = section.find("span", class_="cardMainInfo__content")
                if not title_el or not content_el:
                    continue
                title = title_el.get_text(" ", strip=True).lower()
                m = re.search(r"(\d{2}\.\d{2}\.\d{4})", content_el.get_text())
                if not m:
                    continue
                d = m.group(1)
                if "размещен контракт" in title:
                    placed = d
                elif "обновлен контракт" in title:
                    updated = d
            if not placed:
                m = re.search(
                    r"Размещен контракт в реестре контрактов\s*</span>\s*"
                    r'<span[^>]*>\s*(\d{2}\.\d{2}\.\d{4})',
                    html_content,
                    re.I | re.S,
                )
                if m:
                    placed = m.group(1)
            if not updated:
                m = re.search(
                    r"Обновлен контракт в реестре контрактов\s*</span>\s*"
                    r'<span[^>]*>\s*(\d{2}\.\d{2}\.\d{4})',
                    html_content,
                    re.I | re.S,
                )
                if m:
                    updated = m.group(1)
        except Exception as e:
            logger.debug(f"parse registry dates: {e}")
        return placed, updated

    def _process_contracts_parallel(
        self,
        contracts: List[Dict],
        max_workers: int = 1,
        current_ip: Optional[str] = None,
    ) -> List[Dict]:
        if not contracts:
            return []
        today_contracts = []
        actual_workers = min(max_workers, len(contracts))

        args = []
        for i, c in enumerate(contracts):
            if current_ip:
                sess = self.session_manager.get_session_by_ip(current_ip)
            else:
                sess = self.session_manager.get_session(i)
            args.append((c, sess))

        def process_one(item):
            contract, session = item
            return self._check_reject_date_inline_with_session(
                contract, session, max_workers, current_ip=current_ip
            )

        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
            futures = {executor.submit(process_one, a): a[0] for a in args}
            for future in as_completed(futures):
                contract = futures[future]
                try:
                    is_today = future.result(timeout=40)
                    if is_today:
                        today_contracts.append(contract)
                except Exception as e:
                    logger.error(
                        f"  ❌ Ошибка обработки {contract['contract_number']}: {e}"
                    )
                self._flush_page_to_disk()
        return today_contracts

    def _process_contracts_sequential(
        self,
        contracts: List[Dict],
        max_workers: int = 1,
        current_ip: Optional[str] = None,
    ) -> List[Dict]:
        today_contracts = []
        if current_ip:
            session = self.session_manager.get_session_by_ip(current_ip)
        else:
            session = self.session_manager.session

        if not session:
            logger.error("❌ Нет сессии для последовательной обработки!")
            return []

        for contract in contracts:
            is_today = self._check_reject_date_inline_with_session(
                contract, session=session, max_workers=1, current_ip=current_ip
            )
            if is_today:
                today_contracts.append(contract)
                logger.info(f"  🚨 +1 СЕГОДНЯШНИЙ отказ: {contract['contract_number']}")
            self._flush_page_to_disk()
        return today_contracts

    def _save_to_excel(
        self,
        reestr_number: str,
        reject_url: str,
        reject_date: str,
        supplier_name: str = "",
        email: str = "",
        inn: str = "",
        phone: str = "",
        price: str = "",
        region: str = "",
        customer_name: str = "",
        contract_date: str = "",
        email_sent: bool = False,
        email_status: str = "",
    ):
        try:
            sent_str = "Да" if email_sent else "Нет"
            if not email_sent and email_status:
                sent_str = f"Нет ({email_status})"

            record = {
                "Реестровый номер контракта": reestr_number,
                "Ссылка на контракт": reject_url,
                "Цена контракта": price,
                "Регион Заказчика": region,
                "Наименование Заказчика": customer_name,
                "Наименование поставщика": supplier_name,
                "ИНН Поставщика": inn,
                "Телефон Поставщика": phone,
                "Почта поставщика": email,
                "Дата заключения контракта": contract_date,
                "Дата размещения решения о расторжении": reject_date,
                "Отправлено": sent_str,
            }
            result = self.excel_manager.add_record(record)
            if result:
                with self._lock:
                    self._save_counter += 1
        except Exception as e:
            logger.error(f"  ❌ [{reestr_number}] Excel ошибка: {e}")

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
        current_ip: Optional[str] = None,
        ip_pool: Optional[List[str]] = None,
        **kwargs,
    ) -> List[Dict]:
        today_contracts: List[Dict] = []
        seen_reestr = set()
        consecutive_errors = 0

        # статистика окна
        stats = {
            "pages_ok": 0,
            "pages_fail": 0,
            "reject_links_total": 0,
            "cache_skip": 0,
            "checked": 0,
            "today": 0,
        }

        price_label = f" 💰 {price_from:,}-{price_to or '∞'}" if price_from else ""
        window_label = f" 📅 {contract_date_from} – {contract_date_to}{price_label}"
        start_ip = current_ip or (ip_pool[0] if ip_pool else None)

        # --- 1-я страница ---
        first_url = search_manager._build_search_url(
            page=1,
            contract_date_from=contract_date_from,
            contract_date_to=contract_date_to,
            price_from=price_from,
            price_to=price_to,
        )
        first_response, used_ip = search_manager._make_request_with_ip(
            first_url, target_ip=start_ip
        )
        ip_name = (
            search_manager.session_manager.get_ip_name(used_ip) if used_ip else "unknown"
        )

        if first_response is None:
            logger.error(f"⏹ Не удалось загрузить 1-ю страницу{window_label} | 🌐 {ip_name}")
            return today_contracts

        if "captcha" in first_response.text.lower():
            logger.error(f"🤖 CAPTCHA!{window_label}")
            return today_contracts

        total_records = search_manager._parse_total_records(first_response.text)
        if total_records > 0:
            rpp = int(getattr(settings, "records_per_page", "50").replace("_", ""))
            total_pages = min((total_records + rpp - 1) // rpp, max_pages)
            logger.info(
                f"  📊 Всего записей: {total_records}, страниц: {total_pages}"
                f"{window_label} | 🌐 {ip_name}"
            )
        else:
            total_pages = max_pages
            logger.info(f"  📊 Лимит страниц: {max_pages}{window_label} | 🌐 {ip_name}")

        def process_page(html: str, page_idx: int, page_ip: str, page_ip_name: str):
            nonlocal consecutive_errors
            if html is None:
                consecutive_errors += 1
                stats["pages_fail"] += 1
                logger.warning(
                    f"  ⚠️ Стр.{page_idx} не загружена ({consecutive_errors}/3)"
                    f"{window_label} | 🌐 {page_ip_name}"
                )
                return consecutive_errors >= 3  # stop?

            consecutive_errors = 0
            stats["pages_ok"] += 1

            # --- быстрый text-check ---
            if not self._page_has_reject(html):
                logger.info(
                    f"  ⏭ Стр.{page_idx} без reject-info{window_label} | 🌐 {page_ip_name}"
                )
                return False

            contracts = self._parse_search_page(html)
            new_contracts = [c for c in contracts if c["reestr_number"] not in seen_reestr]
            for c in new_contracts:
                seen_reestr.add(c["reestr_number"])

            stats["reject_links_total"] += len(new_contracts)
            logger.info(
                f"  🔍 Стр.{page_idx}: reject-info={len(new_contracts)}"
                f"{window_label} | 🌐 {page_ip_name}"
            )

            if not new_contracts:
                return False

            # кэш до тяжёлых запросов
            to_check = []
            for c in new_contracts:
                if self.rejections_cache.is_processed(c["reestr_number"]):
                    stats["cache_skip"] += 1
                else:
                    to_check.append(c)

            if stats["cache_skip"] and not to_check:
                logger.info(
                    f"  ⏭ Стр.{page_idx}: все {len(new_contracts)} уже в кэше"
                    f"{window_label}"
                )
                return False

            if to_check:
                logger.info(
                    f"  ⚡ Стр.{page_idx}: к проверке даты {len(to_check)} "
                    f"(кэш skip {len(new_contracts) - len(to_check)}){window_label}"
                )
                stats["checked"] += len(to_check)

                if max_workers > 1 and len(to_check) > 1:
                    page_today = self._process_contracts_parallel(
                        to_check, max_workers, current_ip=page_ip
                    )
                else:
                    page_today = self._process_contracts_sequential(
                        to_check, max_workers, current_ip=page_ip
                    )
                today_contracts.extend(page_today)
                stats["today"] += len(page_today)

            return False

        # страница 1
        stop = process_page(first_response.text, 1, used_ip, ip_name)
        if stop or total_pages == 1:
            logger.info(
                f"✅ Окно{window_label}: стр={stats['pages_ok']}/{total_pages}, "
                f"reject-info={stats['reject_links_total']}, кэш={stats['cache_skip']}, "
                f"проверено={stats['checked']}, СЕГОДНЯ={stats['today']}"
            )
            return today_contracts

        # остальные страницы (по пулу IP)
        remaining_urls = [
            search_manager._build_search_url(
                page=p,
                contract_date_from=contract_date_from,
                contract_date_to=contract_date_to,
                price_from=price_from,
                price_to=price_to,
            )
            for p in range(2, total_pages + 1)
        ]

        if ip_pool and hasattr(search_manager, "_fetch_page_batch_multi_ip"):
            responses_with_ip = search_manager._fetch_page_batch_multi_ip(
                remaining_urls, ip_pool=ip_pool
            )
            for page_idx, (resp, page_ip) in enumerate(responses_with_ip, start=2):
                page_ip_name = search_manager.session_manager.get_ip_name(page_ip) if page_ip else "unknown"
                html = resp.text if resp else None
                # process_page(html, page_idx, page_ip, page_ip_name) — твой текущий разбор
                if process_page(
                    resp.text if resp else None, page_idx, page_ip, page_ip_name
                ):
                    break
        else:
            for i, url in enumerate(remaining_urls):
                page_num = i + 2
                selected_ip = ip_pool[i % len(ip_pool)] if ip_pool else start_ip
                resp, actual_ip = search_manager._make_request_with_ip(
                    url, target_ip=selected_ip
                )
                actual_name = (
                    search_manager.session_manager.get_ip_name(actual_ip)
                    if actual_ip
                    else "unknown"
                )
                if process_page(
                    resp.text if resp else None, page_num, actual_ip, actual_name
                ):
                    break
                time.sleep(random.uniform(0.4, 1.2))

        logger.info(
            f"✅ Окно{window_label}: стр={stats['pages_ok']}/{total_pages}, "
            f"reject-info={stats['reject_links_total']}, кэш={stats['cache_skip']}, "
            f"проверено={stats['checked']}, СЕГОДНЯ={stats['today']}"
        )
        return today_contracts

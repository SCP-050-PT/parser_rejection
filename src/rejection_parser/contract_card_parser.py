"""
contract_card_parser.py — парсинг карточки контракта zakupki.gov.ru
Версия для заказчицы Арины Еремян (rnp-help.ru).
Извлекает: цена, заказчик, регион, дата заключения, поставщик, email, ИНН, телефон.
"""

import re
import time
import random
from typing import Optional, Dict, Any
from dataclasses import dataclass
from bs4 import BeautifulSoup
from loguru import logger


@dataclass
class ContractCardData:
    """Данные из карточки контракта"""

    # === Новые поля ===
    price: str = ""  # Цена контракта (например: "278 285,83 ₽")
    region: str = ""  # Регион заказчика (полный адрес)
    customer_name: str = ""  # Наименование заказчика
    contract_date: str = ""  # Дата заключения контракта (DD.MM.YYYY)

    # === Старые поля поставщика ===
    supplier_name: str = ""
    email: str = ""
    inn: str = ""
    phone: str = ""
    address: str = ""

    def has_email(self) -> bool:
        return bool(self.email) and "@" in self.email

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price": self.price,
            "region": self.region,
            "customer_name": self.customer_name,
            "contract_date": self.contract_date,
            "supplier_name": self.supplier_name,
            "email": self.email,
            "inn": self.inn,
            "phone": self.phone,
            "address": self.address,
        }


class ContractCardParser:
    """Парсер карточки контракта zakupki.gov.ru (common-info.html)"""

    BASE_URL = "https://zakupki.gov.ru/epz/contract/contractCard/common-info.html"

    def __init__(self, session_manager):
        self.session_manager = session_manager

    def _build_url(self, reestr_number: str) -> str:
        return f"{self.BASE_URL}?reestrNumber={reestr_number}"

    def parse(self, reestr_number: str) -> ContractCardData:
        """
        Парсит карточку контракта и возвращает данные поставщика + заказчика.
        """
        url = self._build_url(reestr_number)
        logger.info(f"  📄 Парсим карточку контракта: {reestr_number}")

        time.sleep(0.5 + random.uniform(0, 0.5))

        try:
            response = self.session_manager.make_request_fast(
                url, self.session_manager.session
            )

            if response is None or response.status_code != 200:
                logger.warning(f"  ⚠️ Не удалось загрузить карточку {reestr_number}")
                return ContractCardData()

            return self._parse_html(response.text, reestr_number)

        except Exception as e:
            logger.error(f"  ❌ Ошибка парсинга карточки {reestr_number}: {e}")
            return ContractCardData()

    def _parse_html(self, html: str, reestr_number: str) -> ContractCardData:
        """Парсит HTML карточки контракта"""
        data = ContractCardData()

        try:
            soup = BeautifulSoup(html, "lxml")

            # =====================================================================
            # 1. ЦЕНА КОНТРАКТА
            # =====================================================================
            price_elem = soup.find("span", class_="cardMainInfo__content cost")
            if price_elem:
                data.price = price_elem.get_text(strip=True)
            else:
                # Альтернативный поиск
                price_div = soup.find("div", class_="price")
                if price_div:
                    price_span = price_div.find("span", class_="cardMainInfo__content")
                    if price_span:
                        data.price = price_span.get_text(strip=True)

            # =====================================================================
            # 2. НАИМЕНОВАНИЕ ЗАКАЗЧИКА
            # =====================================================================
            for section in soup.find_all("div", class_="cardMainInfo__section"):
                title = section.find("span", class_="cardMainInfo__title")
                if title and "Заказчик" in title.get_text():
                    content = section.find("span", class_="cardMainInfo__content")
                    if content:
                        # Берём текст, убираем лишние пробелы
                        data.customer_name = content.get_text(separator=" ", strip=True)
                        # Убираем возможные артефакты HTML
                        data.customer_name = re.sub(
                            r"\s+", " ", data.customer_name
                        ).strip()
                    break

            # =====================================================================
            # 3. ДАТА ЗАКЛЮЧЕНИЯ КОНТРАКТА
            # =====================================================================
            for section in soup.find_all("div", class_="cardMainInfo__section"):
                title = section.find("span", class_="cardMainInfo__title")
                if title and "Заключение контракта" in title.get_text():
                    content = section.find("span", class_="cardMainInfo__content")
                    if content:
                        data.contract_date = content.get_text(strip=True)
                    break

            # =====================================================================
            # 4. РЕГИОН ЗАКАЗЧИКА (адрес места поставки)
            # =====================================================================
            # Ищем блок "Информация о месте поставки товара, выполнения работы или оказания услуги"
            delivery_section = None
            for h2 in soup.find_all("h2", class_=re.compile(r"blockInfo__title")):
                if "Информация о месте поставки" in h2.get_text():
                    delivery_section = h2.find_parent("div", class_="blockInfo")
                    break

            if delivery_section:
                info_span = delivery_section.find("span", class_="section__info")
                if info_span:
                    data.region = info_span.get_text(separator=" ", strip=True)
                    data.region = re.sub(r"\s+", " ", data.region).strip()
                else:
                    # Пробуем найти по другому
                    section = delivery_section.find(
                        "section", class_="blockInfo__section"
                    )
                    if section:
                        info = section.find("span", class_="section__info")
                        if info:
                            data.region = info.get_text(separator=" ", strip=True)
                            data.region = re.sub(r"\s+", " ", data.region).strip()

            # =====================================================================
            # 5. ИНФОРМАЦИЯ О ПОСТАВЩИКАХ
            # =====================================================================
            supplier_section = None

            # Вариант 1: по заголовку h2
            for h2 in soup.find_all("h2", class_="blockInfo__title"):
                if "Информация о поставщиках" in h2.get_text():
                    supplier_section = h2.find_parent("div", class_="blockInfo")
                    break

            # Вариант 2: по заголовку в div
            if not supplier_section:
                for div in soup.find_all("div", class_="blockInfo"):
                    title = div.find("h2", class_="blockInfo__title")
                    if title and "Информация о поставщиках" in title.get_text():
                        supplier_section = div
                        break

            if not supplier_section:
                logger.warning(
                    f"  ⚠️ Раздел 'Информация о поставщиках' не найден в {reestr_number}"
                )
                return data

            # Ищем таблицу внутри раздела
            table = supplier_section.find("table", class_="blockInfo__table")
            if not table:
                logger.warning(f"  ⚠️ Таблица поставщиков не найдена в {reestr_number}")
                return data

            # Берём первую строку данных (первый поставщик)
            tbody = table.find("tbody", class_="tableBlock__body")
            if not tbody:
                tbody = table.find("tbody")

            if not tbody:
                logger.warning(
                    f"  ⚠️ tbody не найден в таблице поставщиков {reestr_number}"
                )
                return data

            first_row = tbody.find("tr", class_="tableBlock__row")
            if not first_row:
                first_row = tbody.find("tr")

            if not first_row:
                logger.warning(f"  ⚠️ Строка поставщика не найдена в {reestr_number}")
                return data

            # Парсим колонки
            cols = first_row.find_all("td", class_="tableBlock__col")

            if len(cols) < 2:
                logger.warning(
                    f"  ⚠️ Недостаточно колонок в таблице поставщиков {reestr_number}"
                )
                return data

            # Колонка 0: Организация + ИНН + КПП
            org_col = cols[0]

            # Название организации — первый текстовый узел или span
            org_name_elem = org_col.find(
                "span", class_=re.compile(r"cardMainInfo__content|organizationName")
            )
            if org_name_elem:
                data.supplier_name = org_name_elem.get_text(strip=True)
            else:
                # Берём весь текст первого td, убирая ИНН/КПП
                full_text = org_col.get_text(separator="\n", strip=True)
                lines = [l.strip() for l in full_text.split("\n") if l.strip()]
                # Первая непустая строка — обычно название
                for line in lines:
                    if (
                        not line.startswith("ИНН:")
                        and not line.startswith("КПП:")
                        and len(line) > 5
                    ):
                        data.supplier_name = line
                        break

            # ИНН
            inn_match = re.search(r"ИНН:\s*(\d+)", org_col.get_text())
            if inn_match:
                data.inn = inn_match.group(1)

            # Колонка с email (обычно предпоследняя или с заголовком "Телефон, электронная почта")
            for col in cols:
                text = col.get_text(separator=" ", strip=True)
                # Ищем email
                email_match = re.search(
                    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text
                )
                if email_match:
                    data.email = email_match.group(0).lower().strip()
                    # Телефон — всё что до email
                    phone_part = text[: text.find(data.email)].strip()
                    # Убираем лишние пробелы и переносы
                    phone_part = re.sub(r"\s+", " ", phone_part).strip()
                    if phone_part and not phone_part.startswith("@"):
                        data.phone = phone_part
                    break

            # Если email не нашли в колонках — ищем по всей строке
            if not data.email:
                row_text = first_row.get_text(separator=" ", strip=True)
                email_match = re.search(
                    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", row_text
                )
                if email_match:
                    data.email = email_match.group(0).lower().strip()

            logger.info(
                f"  ✅ Карточка спарсена: "
                f"Цена: {data.price or 'не найдена'} | "
                f"Заказчик: {data.customer_name[:40] if data.customer_name else 'не найден'}... | "
                f"Регион: {data.region[:40] if data.region else 'не найден'}... | "
                f"Дата заключения: {data.contract_date or 'не найдена'} | "
                f"Поставщик: {data.supplier_name[:40] if data.supplier_name else 'не найдено'}... | "
                f"Email: {data.email or 'не найден'} | "
                f"ИНН: {data.inn or 'не найден'}"
            )

            return data

        except Exception as e:
            logger.error(f"  ❌ Ошибка парсинга HTML карточки {reestr_number}: {e}")
            return ContractCardData()


# =============================================================================
# ФАБРИКА
# =============================================================================

_card_parser: Optional[ContractCardParser] = None


def get_contract_card_parser(session_manager) -> ContractCardParser:
    global _card_parser
    if _card_parser is None:
        _card_parser = ContractCardParser(session_manager)
    return _card_parser

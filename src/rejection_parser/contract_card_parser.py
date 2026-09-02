"""
contract_card_parser.py — парсинг карточки контракта zakupki.gov.ru
Извлекает: название поставщика, email, ИНН, телефон из раздела "Информация о поставщиках"
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

    supplier_name: str = ""
    email: str = ""
    inn: str = ""
    phone: str = ""
    address: str = ""

    def has_email(self) -> bool:
        return bool(self.email) and "@" in self.email

    def to_dict(self) -> Dict[str, Any]:
        return {
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
        Парсит карточку контракта и возвращает данные поставщика.
        """
        url = self._build_url(reestr_number)
        logger.info(f"  📄 Парсим карточку контракта: {reestr_number}")

        # Небольшая задержка перед запросом
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

            # Ищем раздел "Информация о поставщиках"
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
                f"  ✅ Карточка спарсена: {data.supplier_name[:50] if data.supplier_name else 'не найдено'}... | "
                f"Email: {data.email or 'не найден'} | ИНН: {data.inn or 'не найден'}"
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

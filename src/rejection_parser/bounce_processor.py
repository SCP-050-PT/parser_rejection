"""
bounce_processor.py — обработка bounce-писем с IMAP.
Версия для парсера отказов zakupki.gov.ru (rnp-help.ru).

Функции:
- Подключение к lawyer@rnp-help.ru по IMAP
- Сканирование папки Bounce на bounce-письма
- Парсинг: Final-Recipient, Status, Diagnostic-Code, Remote-MTA
- Классификация: hard/soft/rate_limit/policy/relay/timeout
- Автодобавление в blacklist (только hard bounce)
- Перемещение обработанных в папку Processed
- Логирование статистики

Использование:
    from bounce_processor import process_bounces
    stats = process_bounces()
    # или по cron:
    python -c "from bounce_processor import process_bounces; process_bounces()"
"""

import os
import re
import json
import time
import logging
import imaplib
import email
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from datetime import datetime, timedelta

# Загрузка .env
try:
    from dotenv import load_dotenv

    for path in [".env", "../.env", "../../.env", "/opt/zakupki-rejections/.env"]:
        if os.path.exists(path):
            load_dotenv(path)
            break
except ImportError:
    pass

logger = logging.getLogger("bounce_processor")

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

DATA_DIR = Path("data")
BLACKLIST_FILE = DATA_DIR / "blacklist_rejections.json"
BOUNCE_LOG_FILE = DATA_DIR / "bounce_log.json"
PROCESSED_FOLDER = "Processed"
BOUNCE_FOLDER = "Bounce"

# IMAP настройки (из .env или дефолтные)
IMAP_HOST = os.environ.get("BOUNCE_IMAP_HOST", "imap.timeweb.ru")
IMAP_PORT = int(os.environ.get("BOUNCE_IMAP_PORT", "993"))
IMAP_USER = os.environ.get("BOUNCE_IMAP_USER", "lawyer@rnp-help.ru")
IMAP_PASS = os.environ.get("BOUNCE_IMAP_PASSWORD", "")

# =============================================================================
# ПАТТЕРНЫ BOUNCE (33+ классификаций)
# =============================================================================

BOUNCE_PATTERNS = {
    "hard": [
        r"user unknown",
        r"no such user",
        r"does not exist",
        r"recipient unknown",
        r"invalid recipient",
        r"address rejected",
        r"bad destination",
        r"mailbox unavailable",
        r"user is terminated",
        r"account disabled",
        r"account inactive",
        r"account suspended",
        r"account closed",
        r"account deleted",
        r"mailbox disabled",
        r"mailbox inactive",
        r"mailbox terminated",
        r"recipient address rejected",
        r"unknown user",
        r"unknown recipient",
        r"unrecognized address",
        r"address does not exist",
        r"no mailbox here",
        r"no such mailbox",
        r"mailbox not found",
        r"recipient not found",
        r"user not found",
        r"email not found",
        r"address not found",
        r"invalid address",
        r"bad address",
        r"malformed address",
        r"syntax error",
        r"domain not found",
        r"no such domain",
        r"unknown domain",
        r"unrouteable address",
        r"unrouteable domain",
        r"mx record not found",
        r"no mx record",
        r"dns error",
        r"host not found",
        r"host unknown",
        r"server not found",
        r"connection refused",
        r"connection timed out",
        r"network unreachable",
        r"domain error",
        r"permanent failure",
        r"permanent error",
        r"5\.1\.1",
        r"5\.1\.2",
        r"5\.1\.3",
        r"5\.1\.6",
        r"5\.1\.8",
        r"5\.2\.1",
        r"5\.2\.2",
        r"5\.3\.1",
        r"5\.3\.2",
        r"5\.3\.5",
        r"5\.4\.1",
        r"5\.4\.4",
        r"5\.4\.6",
        r"5\.5\.0",
        r"5\.5\.1",
        r"5\.5\.2",
        r"5\.5\.3",
        r"5\.5\.4",
        r"5\.5\.5",
        r"5\.5\.6",
        r"5\.6\.0",
        r"5\.6\.1",
        r"5\.6\.2",
        r"5\.6\.3",
        r"5\.6\.4",
        r"5\.6\.5",
        r"5\.7\.0",
        r"5\.7\.1",
        r"5\.7\.2",
        r"5\.7\.3",
        r"5\.7\.4",
        r"5\.7\.5",
        r"5\.7\.6",
        r"5\.7\.7",
        r"5\.7\.9",
        r"5\.7\.10",
        r"5\.7\.11",
        r"5\.7\.12",
        r"5\.7\.13",
        r"5\.7\.14",
        r"5\.7\.15",
        r"5\.7\.16",
        r"5\.7\.17",
        r"5\.7\.18",
        r"5\.7\.19",
        r"5\.7\.20",
        r"5\.7\.21",
        r"5\.7\.22",
        r"5\.7\.23",
        r"5\.7\.24",
        r"5\.7\.25",
        r"5\.7\.26",
        r"5\.7\.27",
    ],
    "soft": [
        r"mailbox full",
        r"quota exceeded",
        r"insufficient space",
        r"over quota",
        r"disk quota",
        r"storage quota",
        r"temporary failure",
        r"temporarily unavailable",
        r"try again later",
        r"retry",
        r"defer",
        r"delayed",
        r"greylist",
        r"grey listing",
        r"4\.2\.1",
        r"4\.2\.2",
        r"4\.2\.3",
        r"4\.3\.1",
        r"4\.3\.2",
        r"4\.3\.3",
        r"4\.3\.5",
        r"4\.4\.1",
        r"4\.4\.2",
        r"4\.4\.3",
        r"4\.4\.4",
        r"4\.4\.5",
        r"4\.4\.6",
        r"4\.4\.7",
        r"4\.5\.0",
        r"4\.5\.1",
        r"4\.5\.2",
        r"4\.5\.3",
        r"4\.5\.4",
        r"4\.5\.5",
        r"4\.6\.0",
        r"4\.6\.1",
        r"4\.6\.2",
        r"4\.6\.3",
        r"4\.6\.4",
        r"4\.6\.5",
        r"4\.7\.0",
        r"4\.7\.1",
        r"4\.7\.2",
        r"4\.7\.3",
        r"4\.7\.4",
        r"4\.7\.5",
        r"4\.7\.6",
        r"4\.7\.7",
        r"4\.7\.8",
        r"4\.7\.9",
        r"4\.7\.10",
        r"4\.7\.11",
        r"4\.7\.12",
        r"4\.7\.13",
        r"4\.7\.14",
        r"4\.7\.15",
        r"4\.7\.16",
        r"4\.7\.17",
        r"4\.7\.18",
        r"4\.7\.19",
        r"4\.7\.20",
        r"4\.7\.21",
        r"4\.7\.22",
        r"4\.7\.23",
        r"4\.7\.24",
        r"4\.7\.25",
        r"4\.7\.26",
        r"4\.7\.27",
    ],
    "rate_limit": [
        r"rate limit",
        r"throttl",
        r"too many",
        r"concurrent",
        r"limit exceeded",
        r"deferred",
        r"temporarily rejected",
        r"4\.7\.1",
        r"4\.4\.5",
        r"4\.7\.0",
        r"4\.7\.28",
        r"4\.7\.8",
        r"421",
        r"450",
        r"451",
    ],
    "policy": [
        r"policy rejection",
        r"rejected by policy",
        r"blocked by policy",
        r"prohibited by policy",
        r"disallowed by policy",
        r"rejected by security policy",
        r"blocked by security",
        r"blocked due to security",
        r"content rejected",
        r"message rejected",
        r"spam detected",
        r"spam filter",
        r"virus detected",
        r"content filter",
        r"attachment rejected",
        r"message too large",
        r"size limit exceeded",
        r"message size exceeds",
        r"5\.3\.4",
        r"5\.7\.1",
        r"5\.7\.12",
        r"5\.7\.13",
        r"5\.7\.14",
        r"5\.7\.15",
        r"5\.7\.23",
        r"5\.7\.24",
        r"5\.7\.25",
        r"5\.7\.26",
        r"5\.7\.27",
    ],
    "relay": [
        r"relay not permitted",
        r"relay access denied",
        r"not a relay",
        r"unauthorized relay",
        r"relay denied",
        r"relay prohibited",
        r"relay rejected",
        r"5\.7\.1",
        r"5\.7\.4",
    ],
    "timeout": [
        r"retry timeout exceeded",
        r"delivery timeout",
        r"connection timeout",
        r"session timeout",
        r"operation timed out",
        r"timed out",
        r"timeout exceeded",
        r"4\.4\.7",
        r"4\.7\.5",
    ],
}


def classify_bounce(text: str) -> str:
    """Классифицирует bounce-письмо по тексту."""
    text_lower = text.lower()

    if "rate limit" in text_lower:
        return "rate_limit"
    if "retry timeout" in text_lower or "delivery timeout" in text_lower:
        return "timeout"
    if "relay not permitted" in text_lower or "relay access denied" in text_lower:
        return "relay"
    if "policy rejection" in text_lower:
        return "policy"

    for category, patterns in BOUNCE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return category
    return "unknown"


# =============================================================================
# ПАРСИНГ BOUNCE-ПИСЬМА
# =============================================================================


def parse_bounce_email(raw_email: bytes) -> Optional[Dict[str, str]]:
    """Парсит bounce-письмо и извлекает данные."""
    try:
        msg = email.message_from_bytes(raw_email)

        from_header = msg.get("From", "").lower()
        subject = msg.get("Subject", "").lower()

        bounce_indicators = [
            "mail delivery system",
            "mail delivery subsystem",
            "postmaster",
            "mailer-daemon",
            "mailer daemon",
            "bounce",
            "undelivered",
            "delivery status notification",
            "delivery failure",
            "returned mail",
        ]

        is_bounce = any(
            ind in from_header or ind in subject for ind in bounce_indicators
        )
        if not is_bounce:
            return None

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type in (
                    "text/plain",
                    "text/html",
                    "message/delivery-status",
                ):
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body += payload.decode("utf-8", errors="ignore") + "\n"
                    except Exception:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="ignore")
            except Exception:
                pass

        final_recipient = None
        match = re.search(
            r"Final-Recipient:\s*.*?;\s*([^\s]+@[^\s]+)", body, re.IGNORECASE
        )
        if not match:
            match = re.search(
                r"Original-Recipient:\s*.*?;\s*([^\s]+@[^\s]+)", body, re.IGNORECASE
            )
        if not match:
            match = re.search(r"To:\s*([^\s]+@[^\s]+)", body, re.IGNORECASE)
        if match:
            final_recipient = match.group(1).strip().lower()

        status = None
        match = re.search(r"Status:\s*(\d\.\d\.\d)", body, re.IGNORECASE)
        if match:
            status = match.group(1)

        diagnostic = None
        match = re.search(
            r"Diagnostic-Code:\s*(.*?)(?=\n[A-Z]|\Z)", body, re.IGNORECASE | re.DOTALL
        )
        if match:
            diagnostic = match.group(1).strip().replace("\n", " ").replace("\r", "")

        remote_mta = None
        match = re.search(r"Remote-MTA:\s*.*?;\s*([^\s]+)", body, re.IGNORECASE)
        if match:
            remote_mta = match.group(1).strip()

        action = None
        match = re.search(r"Action:\s*(\w+)", body, re.IGNORECASE)
        if match:
            action = match.group(1).lower()

        category = classify_bounce(body)

        if action == "failed" and status and status.startswith("5."):
            category = "hard"
        elif action == "delayed" and status and status.startswith("4."):
            category = "soft"

        return {
            "final_recipient": final_recipient,
            "status": status,
            "diagnostic": diagnostic,
            "remote_mta": remote_mta,
            "action": action,
            "category": category,
            "subject": msg.get("Subject", ""),
            "date": msg.get("Date", ""),
        }

    except Exception as e:
        logger.error(f"❌ Ошибка парсинга bounce: {e}")
        return None


# =============================================================================
# BLACKLIST
# =============================================================================


def load_blacklist() -> Set[str]:
    if BLACKLIST_FILE.exists():
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_blacklist(blacklist: Set[str]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(blacklist)), f, ensure_ascii=False, indent=2)


def add_to_blacklist(email: str):
    blacklist = load_blacklist()
    email = email.lower().strip()
    if email not in blacklist:
        blacklist.add(email)
        save_blacklist(blacklist)
        logger.info(f"🚫 Добавлен в blacklist: {email}")
        return True
    return False


# =============================================================================
# ЛОГ BOUNCE
# =============================================================================


def log_bounce(data: Dict[str, str]):
    """Логирует обработанный bounce."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    if BOUNCE_LOG_FILE.exists():
        try:
            with open(BOUNCE_LOG_FILE, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            pass

    entries.append(
        {
            "timestamp": datetime.now().isoformat(),
            "email": data.get("final_recipient"),
            "status": data.get("status"),
            "category": data.get("category"),
            "diagnostic": data.get("diagnostic", "")[:200],
            "remote_mta": data.get("remote_mta"),
            "action": data.get("action"),
        }
    )

    cutoff = datetime.now() - timedelta(days=90)
    entries = [
        e
        for e in entries
        if datetime.fromisoformat(e.get("timestamp", "1970-01-01T00:00:00")) > cutoff
    ]

    with open(BOUNCE_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


# =============================================================================
# IMAP ОБРАБОТКА
# =============================================================================


def process_bounces(
    imap_host: str = None,
    imap_port: int = None,
    imap_user: str = None,
    imap_pass: str = None,
    mark_processed: bool = True,
    move_to_folder: str = None,
) -> Dict[str, any]:
    """
    Основная функция обработки bounce-писем.
    """
    host = imap_host or IMAP_HOST
    port = imap_port or IMAP_PORT
    user = imap_user or IMAP_USER
    password = imap_pass or IMAP_PASS

    if not password:
        logger.error("❌ Пароль IMAP не задан. Установите BOUNCE_IMAP_PASSWORD в .env")
        return {
            "processed": 0,
            "added_to_blacklist": 0,
            "by_category": {},
            "by_provider": {},
            "errors": 1,
        }

    stats = {
        "processed": 0,
        "added_to_blacklist": 0,
        "by_category": {},
        "by_provider": {},
        "errors": 0,
    }

    mail = None
    try:
        logger.info(f"📧 Подключение к IMAP {host}:{port}...")
        mail = imaplib.IMAP4_SSL(host, port)
        mail.login(user, password)
        logger.info(f"✅ Вход выполнен: {user}")

        # Выбираем папку Bounce (фильтры Timeweb уже отсортировали)
        status, _ = mail.select(BOUNCE_FOLDER)
        if status != "OK":
            logger.error(f"❌ Не удалось выбрать папку {BOUNCE_FOLDER}")
            return stats

        # Все письма в папке Bounce — уже bounce, обрабатываем все непрочитанные
        status, messages = mail.search(None, "UNSEEN")

        if status != "OK":
            logger.warning("⚠️ Не удалось выполнить поиск")
            return stats

        msg_ids = messages[0].split()
        logger.info(f"📨 Найдено {len(msg_ids)} непрочитанных писем")

        for msg_id in msg_ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                bounce_data = parse_bounce_email(raw_email)

                if not bounce_data:
                    # Не bounce — помечаем прочитанным и пропускаем
                    if mark_processed:
                        mail.store(msg_id, "+FLAGS", "\\Seen")
                    continue

                stats["processed"] += 1
                category = bounce_data.get("category", "unknown")
                stats["by_category"][category] = (
                    stats["by_category"].get(category, 0) + 1
                )

                provider = bounce_data.get("remote_mta", "unknown")
                if provider:
                    stats["by_provider"][provider] = (
                        stats["by_provider"].get(provider, 0) + 1
                    )

                email_addr = bounce_data.get("final_recipient")

                # Логируем
                log_bounce(bounce_data)

                # Добавляем в blacklist только hard bounce
                if category == "hard" and email_addr:
                    if add_to_blacklist(email_addr):
                        stats["added_to_blacklist"] += 1

                logger.info(
                    f"  📋 Bounce: {email_addr} | {category} | {bounce_data.get('status', 'N/A')} | "
                    f"{bounce_data.get('diagnostic', '')[:60]}..."
                )

                # Перемещаем в Processed и помечаем для удаления
                target_folder = move_to_folder or PROCESSED_FOLDER
                try:
                    mail.create(target_folder)
                except:
                    pass  # Папка уже есть
                mail.copy(msg_id, target_folder)
                mail.store(msg_id, "+FLAGS", "\\Deleted")

            except Exception as e:
                logger.error(f"❌ Ошибка обработки письма {msg_id}: {e}")
                stats["errors"] += 1

        # Expunge удалённых
        mail.expunge()

        mail.close()
        mail.logout()

        logger.info(
            f"✅ Обработка завершена: {stats['processed']} bounce, "
            f"{stats['added_to_blacklist']} в blacklist"
        )
        logger.info(f"📊 По категориям: {stats['by_category']}")

    except Exception as e:
        logger.error(f"❌ Ошибка IMAP: {e}")
        stats["errors"] += 1
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass

    return stats


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    result = process_bounces(move_to_folder="Processed")
    print(f"\n📊 Результат: {result}")

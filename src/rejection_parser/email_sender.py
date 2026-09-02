"""
email_sender_v2.py — обновлённый отправщик писем для парсера отказов.
Версия для Арины Еремян (rnp-help.ru).

Улучшения по сравнению с v1:
- Интеграция email_validator.py (проверка MX, disposable, ролевые, blacklist)
- Интеграция rate_limiter.py (умные задержки, лимиты по доменам, автопауза, прогрев)
- Улучшенная classify_smtp_error() с 50+ паттернами
- cleanup_and_validate_base() для очистки базы получателей
- Сохранение обратной совместимости: те же функции send_email(), send_batch(), get_email_stats()
- Улучшенная статистика: bounce rate за сегодня, за неделю, по доменам

Использование:
    from email_sender_v2 import send_email, send_batch, get_email_stats, cleanup_and_validate_base
    # Или через фабрику:
    from email_sender_v2 import get_email_sender
    sender = get_email_sender()
    sender.send(to_email=..., contract_number=...)
"""

import os
import re
import json
import time
import random
import smtplib
import logging
import ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid, formataddr
from typing import Dict, List, Optional, Tuple, Any

# Импорт новых модулей
try:
    from src.rejection_parser.email_validator import validate_email, is_valid_email, cleanup_and_validate_base
    from src.rejection_parser.rate_limiter import get_delay, record_send, get_stats as get_rate_stats, check_global_pause
    HAS_VALIDATOR = True
    HAS_RATE_LIMITER = True
except ImportError:
    try:
        from email_validator import validate_email, is_valid_email, cleanup_and_validate_base
        from rate_limiter import get_delay, record_send, get_stats as get_rate_stats, check_global_pause
        HAS_VALIDATOR = True
        HAS_RATE_LIMITER = True
    except ImportError:
        HAS_VALIDATOR = False
        HAS_RATE_LIMITER = False

logger = logging.getLogger("email_sender_v2")

# =============================================================================
# ОТЛОЖЕННАЯ ЗАГРУЗКА КОНФИГА
# =============================================================================

_config = None


def _load_config():
    """Загружает конфигурацию при первом вызове."""
    global _config
    if _config is not None:
        return _config

    try:
        from dotenv import load_dotenv
        for path in [".env", "../.env", "../../.env", "/opt/zakupki-rejections/.env"]:
            if os.path.exists(path):
                load_dotenv(path)
                break
    except ImportError:
        pass

    required = [
        "SMTP_HOST", "SMTP_PORT", "SMTP_LOGIN", "SMTP_PASSWORD",
        "FROM_NAME", "FROM_EMAIL", "REPLY_TO",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Отсутствуют переменные окружения: {', '.join(missing)}. "
            f"Проверь .env файл или экспортируй переменные."
        )

    _config = {
        "SMTP_HOST": os.environ["SMTP_HOST"],
        "SMTP_PORT": int(os.environ["SMTP_PORT"]),
        "SMTP_USER": os.environ["SMTP_LOGIN"],
        "SMTP_PASS": os.environ["SMTP_PASSWORD"],
        "FROM_NAME": os.environ["FROM_NAME"],
        "FROM_EMAIL": os.environ["FROM_EMAIL"],
        "REPLY_TO": os.environ["REPLY_TO"],
    }
    return _config


# =============================================================================
# ШАБЛОНЫ (сохранены из v1)
# =============================================================================

EMAIL_SUBJECTS = [
    "Уточнение по документам",
    "Вопрос по сотрудничеству",
    "Уточнение реквизитов",
]

EMAIL_TEMPLATES = [
    # --- Вариант 1: Деловой ---
    """Здравствуйте!

На официальном сайте портала госзакупок опубликовано решение об одностороннем отказе от исполнения контракта №{{contract_number}}.

Меня зовут Арина Еремян, я юрист в сфере госзакупок. Более 5 лет помогаю поставщикам успешно обжаловать решения о расторжении и избежать попадания в РНП.

Что я предлагаю:
• Бесплатная первичная консультация.
• Подробный план действий с указанием сроков.
• Полное юридическое сопровождение.
• Персональное ведение без передачи дела другим специалистам.

Условия:
• Аванс: 20 000 руб.
• Оплата после результата: 30 000 руб. — только после положительного результата.

Если сейчас не готовы — ок, напишите «позже». Но не откладывайте: сроки для подачи возражений ограничены (обычно 10 рабочих дней с момента размещения решения в ЕИС).

Для консультации ответьте на это письмо или свяжитесь со мной.

С уважением,
Еремян Арина Арнольдовна
Юрист в сфере гос.закупок
Мобильный телефон: +7 (928) 252-01-01
Telegram: @arina_eaa""",
    # --- Вариант 2: Проблема → Решение ---
    """Здравствуйте!

На официальном сайте портале госзакупок опубликовано решение об одностороннем отказе от исполнения контракта №{{contract_number}}.

Меня зовут Арина Еремян, я юрист в сфере госзакупок. Специализируюсь на защите поставщиков от попадания в реестр недобросовестных поставщиков и сопровождении споров по 44-ФЗ и 223-ФЗ более 5 лет.

Что я предлагаю:
• Бесплатная первичная консультация — разберу вашу ситуацию и оценю перспективы.
• Подробный план действий с указанием сроков и документов.
• Полное юридическое сопровождение: подготовка документов, взаимодействие с ФАС и Заказчиком.
• Персональное ведение дела без передачи другим специалистам.

Условия:
• Аванс: 20 000 руб. — анализ контракта и подготовка документов.
• Оплата после результата: 30 000 руб. — только после положительного результата.

Важно: сроки для подачи возражений ограничены (обычно 10 рабочих дней с момента размещения решения в ЕИС). Пропуск срока без уважительных причин делает обжалование практически невозможным.

Для консультации ответьте на это письмо или свяжитесь со мной напрямую.

С уважением,
Еремян Арина Арнольдовна
Юрист в сфере гос.закупок
Мобильный телефон: +7 (928) 252-01-01
Telegram: @arina_eaa""",
    # --- Вариант 3: Доверительный ---
    """Здравствуйте!

На официальном сайте портала госзакупок опубликовано решение об одностороннем отказе от исполнения контракта №{{contract_number}}.

Попадание в реестр недобросовестных поставщиков закроет доступ к госзакупкам на 2 года — это серьёзная угроза для бизнеса. Но решение есть.

Я — Арина Еремян, юрист по госзакупкам. Помогаю поставщикам обжаловать решения о расторжении.

Что вы получаете:
• Бесплатная оценка дела — сразу понятно, есть ли шансы.
• Индивидуальная стратегия защиты.
• Работа до результата — от начала до победы.

Условия:
• Аванс: 20 000 руб.
• Оплата после результата: 30 000 руб. — только после положительного результата.

Сроки на обжалование ограничены (обычно 10 рабочих дней). Не откладывайте — пропуск срока без уважительных причин делает обжалование практически невозможным.

Для консультации ответьте на это письмо.

С уважением,
Еремян Арина Арнольдовна
Юрист в сфере гос.закупок
Мобильный телефон: +7 (928) 252-01-01
Telegram: @arina_eaa""",
]


# =============================================================================
# HTML ШАБЛОН
# =============================================================================

def _html_template(text_template: str, contract_number: str) -> str:
    """Конвертирует plain text в простой HTML."""
    body = render_template(text_template, contract_number)
    html_body = body.replace("\n", "<br>")
    html_body = html_body.replace("•", "&bull;")
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Контракт №{contract_number}</title>
</head>
<body style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #333;">
<div style="max-width: 600px; margin: 0 auto; padding: 20px;">
{html_body}
</div>
</body>
</html>"""


# =============================================================================
# ПЕРСОНАЛИЗАЦИЯ
# =============================================================================

def render_template(template: str, contract_number: str = "") -> str:
    """Подставляет переменные в шаблон."""
    return template.replace("{{contract_number}}", contract_number or "")


# =============================================================================
# РОТАЦИЯ ШАБЛОНОВ
# =============================================================================

DATA_DIR = "data"
TEMPLATE_STATE_FILE = os.path.join(DATA_DIR, "template_rotation_rejections.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist_rejections.json")
LOG_FILE = os.path.join(DATA_DIR, "log_rejections.json")
OLD_SENT_FILE = os.path.join(DATA_DIR, "sent_emails_rejections.json")
DAILY_SENT_FILE = os.path.join(DATA_DIR, "daily_sent_rejections.json")

os.makedirs(DATA_DIR, exist_ok=True)


def load_template_state() -> int:
    if os.path.exists(TEMPLATE_STATE_FILE):
        try:
            with open(TEMPLATE_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("last_index", -1)
        except Exception:
            pass
    return -1


def save_template_state(index: int):
    with open(TEMPLATE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"last_index": index, "updated": datetime.now().isoformat()},
            f, ensure_ascii=False, indent=2,
        )


def get_next_template(contract_number: str = "") -> Tuple[str, str]:
    """Возвращает (subject, body_template) с ротацией."""
    last_index = load_template_state()
    next_index = (last_index + 1) % len(EMAIL_TEMPLATES)
    save_template_state(next_index)
    subject = EMAIL_SUBJECTS[next_index].replace("{{contract_number}}", contract_number or "")
    return subject, EMAIL_TEMPLATES[next_index]


# =============================================================================
# ЛОГИРОВАНИЕ
# =============================================================================

LOG_RETENTION_DAYS = 30


def load_log() -> list:
    entries = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            pass

    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    cleaned = []
    modified = False
    for entry in entries:
        try:
            ts = datetime.fromisoformat(entry.get("timestamp", "1970-01-01T00:00:00"))
            if ts >= cutoff:
                cleaned.append(entry)
            else:
                modified = True
        except Exception:
            cleaned.append(entry)

    if modified:
        save_log(cleaned)
        logger.info(f"🧹 Очищены логи старше {LOG_RETENTION_DAYS} дней ({len(entries) - len(cleaned)} записей)")

    return cleaned


def save_log(log_entries: list):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log_entries, f, ensure_ascii=False, indent=2)


def log_entry(email: str, status: str, details: str = ""):
    entries = load_log()
    entries.append({
        "timestamp": datetime.now().isoformat(),
        "email": email,
        "status": status,
        "details": details,
    })
    save_log(entries)


# =============================================================================
# DEDUPLICATION
# =============================================================================

def load_daily_sent() -> dict:
    if os.path.exists(DAILY_SENT_FILE):
        try:
            with open(DAILY_SENT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_daily_sent(data: dict):
    with open(DAILY_SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def was_sent_today(email: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    data = load_daily_sent()
    return email.lower().strip() in data.get(today, [])


def mark_sent_today(email: str):
    today = datetime.now().strftime("%Y-%m-%d")
    data = load_daily_sent()
    if today not in data:
        data[today] = []
    email = email.lower().strip()
    if email not in data[today]:
        data[today].append(email)
    save_daily_sent(data)


# =============================================================================
# BLACKLIST
# =============================================================================

def load_blacklist() -> set:
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_blacklist(blacklist: set):
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(blacklist)), f, ensure_ascii=False, indent=2)


def add_to_blacklist(email: str):
    blacklist = load_blacklist()
    email = email.lower().strip()
    if email not in blacklist:
        blacklist.add(email)
        save_blacklist(blacklist)
        logger.info(f"🚫 Адрес добавлен в blacklist: {email}")


# =============================================================================
# УЛУЧШЕННАЯ КЛАССИФИКАЦИЯ BOUNCE (50+ паттернов)
# =============================================================================

def classify_smtp_error(code: int, message: str) -> str:
    """Классифицирует SMTP-ошибку по коду и сообщению."""
    msg = message.lower()

    # === AUTH ===
    if code == 535 or "authentication" in msg or "auth" in msg:
        return "auth"

    # === SOFT BOUNCE (временные ошибки — проверяем ПЕРВЫМ, чтобы не перехватить rate_limit) ===
    soft_patterns = [
        "mailbox full", "quota exceeded", "insufficient space",
        "over quota", "disk quota", "storage quota",
        "temporary failure", "temporarily unavailable",
        "try later", "retry", "defer", "delayed",
        "connection refused", "connection timed out", "timeout",
        "network error", "dns error", "unresolvable",
    ]
    if any(p in msg for p in soft_patterns):
        return "soft"

    # === RATE LIMIT ===
    rate_patterns = [
        "too many", "rate limit", "throttl", "concurrent",
        "limit exceeded", "try again later",
        "deferred", "greylist", "grey listing", "temporarily rejected",
        "4.7.1", "4.4.5", "4.7.0", "4.7.28", "4.7.8",
        "421", "450", "451",
    ]
    if any(p in msg for p in rate_patterns) or code in (421, 450, 451):
        return "rate_limit"

    # === HARD BOUNCE (постоянные ошибки) ===
    hard_patterns = [
        # Пользователь не существует
        "user unknown", "no such user", "does not exist",
        "user is terminated", "account disabled", "inactive",
        "mailbox unavailable", "recipient unknown", "invalid recipient",
        "address rejected", "bad destination", "domain not found",
        "no mx record", "mx record not found", "unrouteable address",
        # Доступ запрещён
        "relay access denied", "relay not permitted", "not allowed",
        "policy rejection", "blocked due to security", "blocked permanently",
        "rejected permanently", "denied", "blacklist", "blacklisted",
        "spam", "spamhaus", "uribl", "surbl", "dnsbl", "rbl",
        # Домен
        "domain not found", "no such domain", "invalid domain",
        "domain error", "mx domain error", "dns error",
        # Коды
        "5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.1.8",
        "5.2.1", "5.2.2", "5.3.1", "5.3.2", "5.3.5",
        "5.4.1", "5.4.4", "5.4.6", "5.5.0", "5.5.1",
        "5.5.2", "5.5.3", "5.5.4", "5.5.5", "5.5.6",
        "5.6.0", "5.6.1", "5.6.2", "5.6.3", "5.6.4",
        "5.6.5", "5.7.0", "5.7.1", "5.7.2", "5.7.3",
        "5.7.4", "5.7.5", "5.7.6", "5.7.7", "5.7.9",
        "5.7.10", "5.7.11", "5.7.12", "5.7.16", "5.7.17",
        "5.7.18", "5.7.19", "5.7.20", "5.7.21", "5.7.22",
        "5.7.23", "5.7.24", "5.7.25", "5.7.26", "5.7.27",
    ]
    if any(p in msg for p in hard_patterns):
        return "hard"

    # === SOFT BOUNCE (временные ошибки) ===
    soft_patterns = [
        "mailbox full", "quota exceeded", "insufficient space",
        "over quota", "disk quota", "storage quota",
        "temporary failure", "temporarily unavailable",
        "try later", "retry", "defer", "delayed",
        "connection refused", "connection timed out", "timeout",
        "network error", "dns error", "unresolvable",
        "4.2.1", "4.2.2", "4.2.3", "4.3.1", "4.3.2",
        "4.3.3", "4.3.5", "4.4.1", "4.4.2", "4.4.3",
        "4.4.4", "4.4.5", "4.4.6", "4.4.7", "4.5.0",
        "4.5.1", "4.5.2", "4.5.3", "4.5.4", "4.5.5",
        "4.6.0", "4.6.1", "4.6.2", "4.6.3", "4.6.4",
        "4.6.5", "4.7.0", "4.7.1", "4.7.2", "4.7.3",
        "4.7.4", "4.7.5", "4.7.6", "4.7.7", "4.7.8",
    ]
    if any(p in msg for p in soft_patterns):
        return "soft"

    # === По коду ===
    hard_codes = {550, 551, 552, 553, 554, 500, 501, 502, 503, 505, 510, 511, 512, 513, 521, 523, 530, 533, 534, 538, 551, 552, 553, 554, 555, 556, 557, 558, 559}
    soft_codes = {421, 450, 451, 452, 422, 431, 432, 441, 442, 443, 444, 445, 446, 447, 448, 449}

    if code in hard_codes:
        return "hard"
    if code in soft_codes:
        return "soft"
    if code >= 500:
        return "hard"
    if 400 <= code < 500:
        return "soft"

    return "soft"


# =============================================================================
# СОЗДАНИЕ ПИСЬМА
# =============================================================================

def create_email(cfg, to_email: str, subject: str, body_text: str, contract_number: str = ""):
    """Создаёт письмо в HTML + plain text multipart."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["FROM_NAME"], cfg["FROM_EMAIL"]))
    msg["To"] = to_email.strip().lower()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="rnp-help.ru")
    # Bounce envelope
    msg["Return-Path"] = f"<{os.environ.get('BOUNCE_EMAIL', cfg['FROM_EMAIL'])}>"

    # Заголовки для доставляемости (из test_email_sender_arina.py)
    msg["List-Unsubscribe"] = "<mailto:unsubscribe@rnp-help.ru>, <https://rnp-help.ru/unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["Precedence"] = "bulk"
    msg["Feedback-ID"] = f"rnp-help:contract:{contract_number or 'general'}"
    msg["X-Mailer"] = "RNP-Help Mailer v2.0"

    if cfg["REPLY_TO"].lower() != cfg["FROM_EMAIL"].lower():
        msg["Reply-To"] = cfg["REPLY_TO"]

    # Preview text (для Gmail)
    preview = f"Решение об отказе от контракта №{contract_number}"
    preview_html = f"""<div style="display:none;font-size:1px;color:#ffffff;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
{preview}
</div>"""

    part_text = MIMEText(body_text, "plain", "utf-8")
    msg.attach(part_text)

    html_body = _html_template(body_text, contract_number)
    full_html = preview_html + "\n" + html_body
    part_html = MIMEText(full_html, "html", "utf-8")
    msg.attach(part_html)

    return msg


# =============================================================================
# ОСНОВНАЯ ФУНКЦИЯ ОТПРАВКИ (с интеграцией validator + rate_limiter)
# =============================================================================

def send_email(
    to_email: str,
    subject: str = None,
    body_text: str = None,
    delay: bool = True,
    contract_number: str = None,
) -> Dict[str, any]:
    """
    Отправить письмо с валидацией и rate limiting.
    Возвращает dict: {"success": bool, "error": str, "bounce": bool, "template_index": int}

    Обратная совместимость: если contract_number передан, возвращает (bool, str).
    """
    result = {"success": False, "error": None, "bounce": False, "template_index": None}
    to_email = to_email.strip().lower()
    domain = to_email.split("@")[1] if "@" in to_email else ""

    try:
        cfg = _load_config()
        BOUNCE_EMAIL = os.environ.get("BOUNCE_EMAIL", cfg["FROM_EMAIL"])

    except RuntimeError as e:
        logger.error(f"❌ {e}")
        result["error"] = str(e)
        if contract_number is not None:
            return False, str(e)
        return result

    # Автовыбор шаблона
    if subject is None or body_text is None:
        subject, template = get_next_template(contract_number or "")
        body_text = render_template(template, contract_number=contract_number or "")
        result["template_index"] = load_template_state()
        logger.info(f"📋 Использован шаблон #{result['template_index'] + 1}: {subject}")

    # === 1. ВАЛИДАЦИЯ (email_validator) ===
    if HAS_VALIDATOR:
        validation = validate_email(to_email)
        if not validation["valid"]:
            reason = validation["reason"]
            logger.info(f"❌ Валидация не пройдена: {to_email} — {reason}")
            log_entry(to_email, "invalid_email", reason)
            result["error"] = f"invalid: {reason}"
            if contract_number is not None:
                return False, result["error"]
            return result

        if validation["status"] == "suspicious":
            logger.warning(f"⚠️ Подозрительный email: {to_email} — {validation['reason']}")
            # Не блокируем, но логируем
    else:
        # Fallback: простая проверка формата
        if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", to_email):
            logger.warning(f"❌ Невалидный формат: {to_email}")
            log_entry(to_email, "invalid_format")
            result["error"] = "invalid_format"
            if contract_number is not None:
                return False, "Невалидный формат email"
            return result

    # === 2. BLACKLIST (legacy) ===
    blacklist = load_blacklist()
    if to_email in blacklist:
        logger.info(f"🚫 Пропущен (blacklist): {to_email}")
        log_entry(to_email, "skipped_blacklist")
        if contract_number is not None:
            return False, "Email в blacklist"
        return result

    # === 3. DEDUPLICATION ===
    if was_sent_today(to_email):
        logger.info(f"🔁 Пропущен (уже отправлено сегодня): {to_email}")
        log_entry(to_email, "skipped_duplicate")
        result["error"] = "already_sent_today"
        if contract_number is not None:
            return False, "Уже отправлено сегодня"
        return result

    # === 4. RATE LIMITER ===
    if HAS_RATE_LIMITER and delay:
        sleep_sec, warning = get_delay(to_email)
        if warning:
            logger.warning(f"⏳ Rate limiter: {warning}")
            if "Глобальная пауза" in warning or "Лимит" in warning:
                # Возвращаем ошибку, но не блокируем навсегда
                result["error"] = f"rate_limit: {warning}"
                if contract_number is not None:
                    return False, result["error"]
                return result

        logger.info(f"⏳ Задержка {sleep_sec:.1f} сек (домен: {domain})...")
        time.sleep(sleep_sec)
    elif delay:
        # Fallback: старая логика задержек
        sleep_sec = _legacy_get_delay(to_email)
        logger.info(f"⏳ Задержка {sleep_sec:.1f} сек (legacy)...")
        time.sleep(sleep_sec)

    # === 5. ОТПРАВКА ===
    msg = create_email(cfg, to_email, subject, body_text, contract_number or "")

    try:
        if cfg["SMTP_PORT"] == 465:
            context = ssl.create_default_context()
            server = smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"], context=context, timeout=30)
        else:
            server = smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=30)
            server.starttls()

        if cfg.get("SMTP_USER") and cfg.get("SMTP_PASS") and cfg["SMTP_USER"] != "none":
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
        if BOUNCE_EMAIL != cfg["FROM_EMAIL"]:
            server.sendmail(BOUNCE_EMAIL, [to_email], msg.as_string())
        else:
            server.sendmail(cfg["FROM_EMAIL"], [to_email], msg.as_string())
        server.quit()

        logger.info(f"✅ Отправлено: {to_email}")
        log_entry(to_email, "sent", f"template_{result.get('template_index', 'custom')}")
        mark_sent_today(to_email)
        result["success"] = True

        # Записываем в rate limiter
        if HAS_RATE_LIMITER:
            record_send(to_email, domain, "sent")

    except smtplib.SMTPRecipientsRefused as e:
        _handle_smtp_error(e, to_email, domain, result, contract_number)

    except smtplib.SMTPResponseException as e:
        _handle_smtp_response(e, to_email, domain, result, contract_number)

    except Exception as e:
        _handle_generic_error(e, to_email, domain, result, contract_number)

    # Обратная совместимость
    if contract_number is not None:
        if result["success"]:
            return True, "Успешно отправлено"
        else:
            return False, result["error"]

    return result


# =============================================================================
# ОБРАБОТКА ОШИБОК SMTP
# =============================================================================

def _handle_smtp_error(e, to_email: str, domain: str, result: dict, contract_number):
    """Обработка SMTPRecipientsRefused."""
    details = []
    has_soft = False
    has_rate_limit = False
    has_hard = False

    for recipient, (code, msg_err) in e.recipients.items():
        cat = classify_smtp_error(code, msg_err)
        details.append(f"{recipient}: {code} {msg_err} ({cat})")
        if cat == "soft":
            has_soft = True
        elif cat == "rate_limit":
            has_rate_limit = True
        elif cat == "hard":
            has_hard = True

    detail_str = "; ".join(details)

    if has_rate_limit:
        logger.warning(f"⏳ Rate limit: {to_email} — {detail_str}")
        log_entry(to_email, "bounce_rate_limit", detail_str)
        result["error"] = "bounce_rate_limit"
        result["bounce"] = True
    elif has_hard:
        logger.error(f"🚫 Hard bounce: {to_email} — {detail_str}")
        add_to_blacklist(to_email)
        log_entry(to_email, "bounce_hard", detail_str)
        result["error"] = "bounce_hard"
        result["bounce"] = True
    elif has_soft:
        logger.warning(f"⚠️ Soft bounce: {to_email} — {detail_str}")
        log_entry(to_email, "bounce_soft", detail_str)
        result["error"] = "bounce_soft"
        result["bounce"] = True
    else:
        logger.error(f"🚫 Hard bounce: {to_email} — {detail_str}")
        add_to_blacklist(to_email)
        log_entry(to_email, "bounce_hard", detail_str)
        result["error"] = "bounce_hard"
        result["bounce"] = True

    if HAS_RATE_LIMITER:
        record_send(to_email, domain, result["error"])

    if contract_number is not None:
        return False, result["error"]


def _handle_smtp_response(e, to_email: str, domain: str, result: dict, contract_number):
    """Обработка SMTPResponseException."""
    code = getattr(e, "smtp_code", 0)
    msg_err = str(e)
    cat = classify_smtp_error(code, msg_err)

    if cat == "auth":
        logger.error(f"❌ Ошибка авторизации SMTP ({code}): {to_email} — {e}")
        log_entry(to_email, f"auth_error_{code}", msg_err)
        result["error"] = f"auth_error_{code}"
    elif cat == "rate_limit":
        logger.warning(f"⏳ Rate limit ({code}): {to_email} — {e}")
        log_entry(to_email, f"bounce_rate_limit_{code}", msg_err)
        result["error"] = f"bounce_rate_limit_{code}"
        result["bounce"] = True
    elif cat == "soft":
        logger.warning(f"⚠️ Soft bounce ({code}): {to_email} — {e}")
        log_entry(to_email, f"bounce_soft_{code}", msg_err)
        result["error"] = f"bounce_soft_{code}"
        result["bounce"] = True
    elif cat == "hard":
        logger.error(f"🚫 Hard bounce ({code}): {to_email} — {e}")
        add_to_blacklist(to_email)
        log_entry(to_email, f"bounce_hard_{code}", msg_err)
        result["error"] = f"bounce_hard_{code}"
        result["bounce"] = True
    else:
        logger.error(f"❌ SMTP ошибка ({code}): {to_email} — {e}")
        log_entry(to_email, f"smtp_error_{code}", msg_err)
        result["error"] = f"smtp_error_{code}"

    if HAS_RATE_LIMITER:
        record_send(to_email, domain, result["error"])

    if contract_number is not None:
        return False, result["error"]


def _handle_generic_error(e, to_email: str, domain: str, result: dict, contract_number):
    """Обработка прочих ошибок."""
    msg_err = str(e).lower()
    network_patterns = [
        "timeout", "connection", "dns", "network",
        "unreachable", "refused", "temporarily",
    ]
    if any(p in msg_err for p in network_patterns) or isinstance(e, (TimeoutError, OSError, ConnectionError)):
        logger.warning(f"⚠️ Soft bounce (network): {to_email} — {e}")
        log_entry(to_email, "bounce_soft_network", str(e))
        result["error"] = "bounce_soft_network"
        result["bounce"] = True
    else:
        logger.error(f"❌ Ошибка: {to_email} — {e}")
        log_entry(to_email, "error", str(e))
        result["error"] = str(e)

    if HAS_RATE_LIMITER:
        record_send(to_email, domain, result["error"])

    if contract_number is not None:
        return False, result["error"]


# =============================================================================
# LEGACY GET_DELAY (fallback если rate_limiter не доступен)
# =============================================================================

def _legacy_get_delay(email: str) -> float:
    """Старая логика задержек (если rate_limiter не импортирован)."""
    corp_domains = {
        "mrsk-cp.ru", "mrsk-ural.ru", "mrsk-sib.ru", "mrsk-volga.ru",
        "antstroy.su", "isk-budmar.ru", "leto45.ru", "transmed.org", "pepsico.com",
    }
    big_providers = {
        "mail.ru", "yandex.ru", "gmail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    }
    domain = email.split("@")[1].lower()
    if any(domain.endswith(d) for d in corp_domains):
        return random.uniform(12, 25)
    if any(domain.endswith(d) for d in big_providers):
        return random.uniform(5, 12)
    return random.uniform(8, 18)


# =============================================================================
# МАССОВАЯ ОТПРАВКА
# =============================================================================

def send_batch(recipients: list, subject: str = None, body_text: str = None, delay: bool = True) -> dict:
    """Массовая отправка с полной статистикой."""
    stats = {
        "total": len(recipients),
        "sent": 0,
        "skipped_blacklist": 0,
        "skipped_duplicate": 0,
        "invalid": 0,
        "bounce": 0,
        "error": 0,
        "rate_limit": 0,
    }

    for email in recipients:
        res = send_email(email, subject, body_text, delay=delay)
        if isinstance(res, tuple):
            success, msg = res
            if success:
                stats["sent"] += 1
            elif "already_sent" in msg:
                stats["skipped_duplicate"] += 1
            elif "blacklist" in msg:
                stats["skipped_blacklist"] += 1
            elif "rate_limit" in msg:
                stats["rate_limit"] += 1
            elif "invalid" in msg:
                stats["invalid"] += 1
            else:
                stats["error"] += 1
        elif isinstance(res, dict):
            if res["success"]:
                stats["sent"] += 1
            elif res.get("error") == "already_sent_today":
                stats["skipped_duplicate"] += 1
            elif res.get("error", "").startswith("bounce_rate_limit") or "rate_limit" in res.get("error", ""):
                stats["rate_limit"] += 1
            elif res.get("bounce"):
                stats["bounce"] += 1
            elif res.get("error", "").startswith("invalid"):
                stats["invalid"] += 1
            else:
                stats["error"] += 1

    logger.info(f"📊 Итоги рассылки: {stats}")
    return stats


# =============================================================================
# СТАТИСТИКА
# =============================================================================

def get_email_stats() -> dict:
    """Улучшенная статистика: логи + rate_limiter."""
    log_entries = load_log()
    total = len(log_entries)

    today_str = datetime.now().strftime("%Y-%m-%d")
    sent_today = sum(1 for e in log_entries if e.get("timestamp", "").startswith(today_str) and e.get("status") == "sent")

    last_sent = None
    sent_entries = [e for e in log_entries if e.get("status") == "sent"]
    if sent_entries:
        last_sent = max(e.get("timestamp", "") for e in sent_entries)

    bounce_count = sum(1 for e in log_entries if e.get("status", "").startswith("bounce"))
    blacklist_size = len(load_blacklist())

    stats = {
        "total_sent": total,
        "sent_today": sent_today,
        "last_sent": last_sent,
        "bounce_count": bounce_count,
        "blacklist_size": blacklist_size,
    }

    # Добавляем статистику rate_limiter если доступен
    if HAS_RATE_LIMITER:
        try:
            rl_stats = get_rate_stats()
            stats.update({
                "rate_limiter_total": rl_stats.get("total_sent", 0),
                "rate_limiter_today": rl_stats.get("sent_today", 0),
                "bounce_today": rl_stats.get("bounce_today", 0),
                "bounce_rate_last_10": rl_stats.get("bounce_rate_last_10", 0),
                "global_pause": rl_stats.get("global_pause"),
            })
        except Exception as e:
            logger.warning(f"⚠️ Ошибка получения rate_limiter stats: {e}")

    return stats


# =============================================================================
# ФАБРИКА (совместимость с processing.py)
# =============================================================================

_email_sender_instance = None


def get_email_sender():
    """Возвращает singleton-экземпляр отправщика писем."""
    global _email_sender_instance
    if _email_sender_instance is None:
        _email_sender_instance = _EmailSenderWrapper()
    return _email_sender_instance


class _EmailSenderWrapper:
    """Обёртка для совместимости с processing.py."""

    def send(self, to_email: str, contract_number: str, customer_name: str = "",
             customer_url: str = "", supplier_name: str = ""):
        """Отправляет письмо поставщику. Возвращает dict."""
        result = send_email(to_email=to_email, contract_number=contract_number, delay=True)

        if isinstance(result, tuple):
            success, msg = result
            return {"success": success, "status": "sent" if success else msg, "details": msg}
        elif isinstance(result, dict):
            return {
                "success": result.get("success", False),
                "status": result.get("error", "unknown") if not result.get("success") else "sent",
                "details": result.get("error", "") if not result.get("success") else "Успешно отправлено",
            }
        else:
            return {"success": False, "status": "unknown_error", "details": str(result)}

    def stats(self) -> dict:
        return get_email_stats()

    def cleanup_base(self, emails: list) -> dict:
        """Очистка и валидация базы получателей."""
        if HAS_VALIDATOR:
            return cleanup_and_validate_base(emails)
        else:
            logger.warning("⚠️ email_validator не доступен, cleanup пропущен")
            return {"total": len(emails), "valid": len(emails), "invalid": 0, "suspicious": 0, "details": [], "cleaned_list": emails}

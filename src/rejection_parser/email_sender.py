"""
email_sender.py — отправка писем поставщикам при отказе от заключения договора.
Версия: 2026-08-21 v8.1 — фикс rate limiting, exponential backoff, остановка батча

Изменения v8.1:
- Увеличены задержки для нового/прогреваемого IP (30-60 сек на mail.ru)
- Добавлен SQLite rate limiter (история отправок, лимиты по домену/провайдеру)
- Добавлен прогрев IP (ограничение писем в день)
- Добавлена автопауза при bounce rate >20%
- Добавлен exponential backoff при rate limit
- Остановка send_batch при rate limit / глобальной паузе / дневном лимите
- envelope_from = BOUNCE_EMAIL для корректной обработки bounce
- Улучшена classify_smtp_error (rate limit паттерны)
- Убран лишний Return-Path заголовок
"""

import os
import re
import json
import time
import random
import smtplib
import logging
import ssl
import sqlite3
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formatdate, make_msgid, formataddr
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
        "SMTP_HOST",
        "SMTP_PORT",
        "FROM_NAME",
        "FROM_EMAIL",
        "REPLY_TO",
        "UNSUBSCRIBE_EMAIL",
        "BOUNCE_EMAIL",
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
        "UNSUBSCRIBE_EMAIL": os.environ["UNSUBSCRIBE_EMAIL"],
        "BOUNCE_EMAIL": os.environ["BOUNCE_EMAIL"],
    }
    return _config


# =============================================================================
# RATE LIMITER (SQLite)
# =============================================================================

DATA_DIR = Path("data")
DB_FILE = DATA_DIR / "email_rate_limiter.db"
WARMUP_STATE_FILE = DATA_DIR / "ip_warmup_state.json"

# === РЕЖИМ ПРОГРЕВА IP ===
IP_WARMUP_MODE = True

# Задержки по доменам (мин, макс) в секундах
DOMAIN_DELAYS = {
    "mail.ru": (30, 60),
    "bk.ru": (30, 60),
    "list.ru": (30, 60),
    "inbox.ru": (30, 60),
    "internet.ru": (30, 60),
    "yandex.ru": (25, 50),
    "ya.ru": (25, 50),
    "yandex.com": (25, 50),
    "yandex.net": (25, 50),
    "yandex.by": (25, 50),
    "yandex.kz": (25, 50),
    "yandex.ua": (25, 50),
    "gmail.com": (20, 45),
    "googlemail.com": (20, 45),
    "outlook.com": (25, 50),
    "hotmail.com": (25, 50),
    "live.com": (25, 50),
    "msn.com": (25, 50),
    "yahoo.com": (25, 50),
    "yahoo.ru": (25, 50),
    "ymail.com": (25, 50),
    "rambler.ru": (30, 60),
    "lenta.ru": (30, 60),
    "narod.ru": (25, 50),
}

# Группы провайдеров для групповых лимитов
PROVIDER_GROUPS = {
    "mailru": ["mail.ru", "bk.ru", "list.ru", "inbox.ru", "internet.ru"],
    "yandex": ["yandex.ru", "ya.ru", "yandex.com", "yandex.net", "yandex.by",
               "yandex.kz", "yandex.ua", "narod.ru"],
    "gmail": ["gmail.com", "googlemail.com"],
    "microsoft": ["outlook.com", "hotmail.com", "live.com", "msn.com"],
    "yahoo": ["yahoo.com", "yahoo.ru", "ymail.com"],
    "rambler": ["rambler.ru", "lenta.ru"],
}

HOURLY_LIMIT = 15
PROVIDER_HOURLY_LIMIT = 10
WARMUP_COUNT = 5
WARMUP_MULTIPLIER = 2.0
WARMUP_DELAY_MULTIPLIER = 3.0
BOUNCE_RATE_THRESHOLD = 0.20
BOUNCE_WINDOW_SIZE = 50
AUTO_PAUSE_MINUTES = 120
DEFAULT_DELAY = (20, 45)


def _init_db() -> sqlite3.Connection:
    """Инициализирует SQLite базу для хранения истории отправок."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS send_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            domain TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'sent',
            error_code INTEGER,
            error_message TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_domain_sent_at 
        ON send_history(domain, sent_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_status_sent_at 
        ON send_history(status, sent_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS domain_warmup (
            domain TEXT PRIMARY KEY,
            sent_count INTEGER DEFAULT 0,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS global_pause (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            paused_until TIMESTAMP,
            reason TEXT
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO global_pause (id, paused_until, reason) 
        VALUES (1, NULL, NULL)
    """)
    conn.commit()
    return conn


def _load_warmup_state() -> dict:
    if WARMUP_STATE_FILE.exists():
        try:
            with open(WARMUP_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"ip_warmup_active": IP_WARMUP_MODE, "total_sent": 0, "first_send_date": None, "daily_sent": {}}


def _save_warmup_state(state: dict):
    try:
        with open(WARMUP_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"⚠️ Ошибка сохранения warmup state: {e}")


def _update_warmup_state():
    state = _load_warmup_state()
    state["total_sent"] = state.get("total_sent", 0) + 1
    today = datetime.now().strftime("%Y-%m-%d")
    state["daily_sent"][today] = state["daily_sent"].get(today, 0) + 1
    if state["first_send_date"] is None:
        state["first_send_date"] = today
    _save_warmup_state(state)
    return state


def get_ip_warmup_days() -> int:
    state = _load_warmup_state()
    if state["first_send_date"] is None:
        return 0
    first = datetime.strptime(state["first_send_date"], "%Y-%m-%d")
    return (datetime.now() - first).days


def get_daily_sent_count() -> int:
    state = _load_warmup_state()
    today = datetime.now().strftime("%Y-%m-%d")
    return state["daily_sent"].get(today, 0)


def record_send(email: str, domain: str, status: str = "sent",
                error_code: Optional[int] = None, error_message: Optional[str] = None):
    try:
        conn = _init_db()
        conn.execute("""
            INSERT INTO send_history (email, domain, status, error_code, error_message)
            VALUES (?, ?, ?, ?, ?)
        """, (email.lower(), domain.lower(), status, error_code, error_message))
        conn.execute("""
            INSERT INTO domain_warmup (domain, sent_count)
            VALUES (?, 1)
            ON CONFLICT(domain) DO UPDATE SET sent_count = sent_count + 1
        """, (domain.lower(),))
        conn.commit()
        conn.close()
        _update_warmup_state()
    except Exception as e:
        logger.warning(f"⚠️ Ошибка записи в rate_limiter DB: {e}")


def get_domain_hourly_count(domain: str) -> int:
    try:
        conn = _init_db()
        hour_ago = datetime.now() - timedelta(hours=1)
        cursor = conn.execute("""
            SELECT COUNT(*) FROM send_history 
            WHERE domain = ? AND sent_at > ? AND status = 'sent'
        """, (domain.lower(), hour_ago.isoformat()))
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.warning(f"⚠️ Ошибка подсчёта hourly count: {e}")
        return 0


def get_provider_hourly_count(domain: str) -> int:
    provider = None
    for prov_name, domains in PROVIDER_GROUPS.items():
        if any(domain == d or domain.endswith("." + d) for d in domains):
            provider = prov_name
            break
    if provider is None:
        return 0
    conn = _init_db()
    hour_ago = datetime.now() - timedelta(hours=1)
    all_domains = PROVIDER_GROUPS[provider]
    placeholders = ",".join(["?"] * len(all_domains))
    cursor = conn.execute(
        f"SELECT COUNT(*) FROM send_history WHERE domain IN ({placeholders}) AND sent_at > ? AND status = 'sent'",
        [d.lower() for d in all_domains] + [hour_ago.isoformat()]
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_domain_warmup_count(domain: str) -> int:
    try:
        conn = _init_db()
        cursor = conn.execute("SELECT sent_count FROM domain_warmup WHERE domain = ?", (domain.lower(),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def get_recent_bounce_rate(window: int = BOUNCE_WINDOW_SIZE) -> float:
    try:
        conn = _init_db()
        cursor = conn.execute("SELECT status FROM send_history ORDER BY sent_at DESC LIMIT ?", (window,))
        statuses = [row[0] for row in cursor.fetchall()]
        conn.close()
        if not statuses:
            return 0.0
        bounce_count = sum(1 for s in statuses if s.startswith("bounce"))
        return bounce_count / len(statuses)
    except Exception:
        return 0.0


def check_global_pause() -> Optional[str]:
    try:
        conn = _init_db()
        cursor = conn.execute("SELECT paused_until, reason FROM global_pause WHERE id = 1")
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            paused_until = datetime.fromisoformat(row[0])
            if datetime.now() < paused_until:
                remaining = (paused_until - datetime.now()).total_seconds() // 60
                return f"Глобальная пауза: {row[1]} (осталось {remaining:.0f} мин)"
            else:
                _clear_global_pause()
        return None
    except Exception:
        return None


def set_global_pause(minutes: int = AUTO_PAUSE_MINUTES, reason: str = "bounce_rate"):
    try:
        paused_until = datetime.now() + timedelta(minutes=minutes)
        conn = _init_db()
        conn.execute("UPDATE global_pause SET paused_until = ?, reason = ? WHERE id = 1",
                     (paused_until.isoformat(), reason))
        conn.commit()
        conn.close()
        logger.warning(f"⏸️ Глобальная пауза на {minutes} мин: {reason}")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка установки global pause: {e}")


def _clear_global_pause():
    try:
        conn = _init_db()
        conn.execute("UPDATE global_pause SET paused_until = NULL, reason = NULL WHERE id = 1")
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_delay(email: str) -> Tuple[float, Optional[str]]:
    email = email.strip().lower()
    domain = email.split("@")[1]

    base_delay = None
    for known_domain, (min_d, max_d) in DOMAIN_DELAYS.items():
        if domain == known_domain or domain.endswith("." + known_domain):
            base_delay = (min_d, max_d)
            break
    if base_delay is None:
        base_delay = DEFAULT_DELAY

    # Режим прогрева IP
    state = _load_warmup_state()
    warmup_days = get_ip_warmup_days()
    daily_sent = get_daily_sent_count()

    if state.get("ip_warmup_active", IP_WARMUP_MODE):
        if warmup_days < 7:
            daily_limit = 20
        elif warmup_days < 14:
            daily_limit = 50
        elif warmup_days < 21:
            daily_limit = 100
        else:
            daily_limit = 200

        if daily_sent >= daily_limit:
            tomorrow = datetime.now() + timedelta(days=1)
            tomorrow = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
            wait_seconds = max((tomorrow - datetime.now()).total_seconds(), 3600)
            return (wait_seconds, f"🌡️ Прогрев IP: лимит {daily_limit} писем/день достигнут ({daily_sent}). День {warmup_days + 1}.")

        multiplier = WARMUP_DELAY_MULTIPLIER
        if warmup_days >= 14:
            multiplier = 2.0
        elif warmup_days >= 21:
            multiplier = 1.5

        delay = random.uniform(base_delay[0] * multiplier, base_delay[1] * multiplier)
        return (delay, f"🌡️ Прогрев IP (день {warmup_days + 1}): задержка x{multiplier} ({delay:.0f} сек), {daily_sent}/{daily_limit}")

    # Прогрев новых доменов
    warmup_count = get_domain_warmup_count(domain)
    if warmup_count < WARMUP_COUNT:
        multiplier = WARMUP_MULTIPLIER
        delay = random.uniform(base_delay[0] * multiplier, base_delay[1] * multiplier)
        return (delay, f"Прогрев домена {domain}: письмо {warmup_count + 1}/{WARMUP_COUNT}, задержка x{multiplier}")

    # Глобальная пауза
    pause_reason = check_global_pause()
    if pause_reason:
        return (AUTO_PAUSE_MINUTES * 60, pause_reason)

    # Bounce rate
    bounce_rate = get_recent_bounce_rate()
    if bounce_rate > BOUNCE_RATE_THRESHOLD:
        set_global_pause(AUTO_PAUSE_MINUTES, f"bounce_rate={bounce_rate:.1%}")
        return (AUTO_PAUSE_MINUTES * 60, f"⏸️ Автопауза: bounce rate {bounce_rate:.1%} > {BOUNCE_RATE_THRESHOLD:.0%}")

    # Групповой лимит
    provider_count = get_provider_hourly_count(domain)
    if provider_count >= PROVIDER_HOURLY_LIMIT:
        return (3600, f"📊 Групповой лимит {PROVIDER_HOURLY_LIMIT} писем/час на провайдера достигнут ({provider_count})")

    # Лимит на домен
    hourly_count = get_domain_hourly_count(domain)
    if hourly_count >= HOURLY_LIMIT:
        return (3600, f"📊 Лимит {HOURLY_LIMIT} писем/час на {domain} достигнут ({hourly_count})")

    delay = random.uniform(base_delay[0], base_delay[1])
    return (delay, None)


# =============================================================================
# 3 ШАБЛОНА ПИСЕМ (Елизавета Борисова)
# =============================================================================

EMAIL_SUBJECTS = [
    "Вопрос по контракту ",
    "Контракт — вопрос по процедуре",
    "По вашему контракту ",
]

EMAIL_TEMPLATES = [
    """Здравствуйте, {{supplier_name}}.

Меня зовут Елизавета Борисова, ведущий юрист компании ФасЭксперт 24.
Специализируюсь на защите поставщиков от включения в реестр недобросовестных
поставщиков (РНП) и сопровождении споров по 44-ФЗ и 223-ФЗ.

Пишу по поводу одностороннего расторжения контракта № {{contract_number}}
с {{org_name}}.

Чем могу помочь:
— Проведу анализ ваших документов и оценю перспективы дела
— Подготовлю возражения и пояснения для комиссии УФАС
— Представлю ваши интересы на заседании по видеоконференции

Условия:
— Предоплата: 20 000 руб. (анализ, подготовка документов, участие в заседании)
— Окончательный расчет: 30 000 руб. (после вынесения решения)

Срок подачи возражений — 10 календарных дней с момента публикации в ЕИС.
Пропуск срока существенно усложняет защиту.

Примеры успешных дел: https://disk.yandex.ru/d/3XCOPOCARw72Ww

Свяжитесь со мной: +7 (927) 188-31-82

С уважением,
Елизавета Борисова
ФасЭксперт 24
https://fasexpert24.ru""",
    """Здравствуйте, {{supplier_name}}.

Меня зовут Елизавета, юрист ФасЭксперт 24. Увидела информацию о расторжении
контракта № {{contract_number}} с {{org_name}}.

Если у вас есть вопросы по процедуре или нужна помощь в подготовке документов —
буду рада обсудить ситуацию. Первое знакомство с делом провожу без оплаты,
чтобы вы могли оценить перспективы.

Что входит в работу:
— Анализ документов и правовая позиция
— Подготовка возражений на одностороннее расторжение
— Участие в заседании комиссии УФАС

Стоимость:
— 20 000 руб. — начало работы (анализ, документы, заседание)
— 30 000 руб. — после получения положительного решения

Срок ограничен: 10 дней с момента публикации в ЕИС.

Примеры успешных дел: https://disk.yandex.ru/d/3XCOPOCARw72Ww

Свяжитесь со мной: +7 (927) 188-31-82

С уважением,
Елизавета Борисова
ФасЭксперт 24
https://fasexpert24.ru""",
    """Здравствуйте, {{supplier_name}}.

По контракту № {{contract_number}} с {{org_name}} у вас есть риск включения
в реестр недобросовестных поставщиков. Срок на защиту — 10 дней.

Я — Елизавета Борисова, юрист ФасЭксперт 24. Помогаю поставщикам оспаривать
одностороннее расторжение и защищать репутацию.

Условия:
— 20 000 руб. — анализ и подготовка документов
— 30 000 руб. — после положительного решения

Первое знакомство с делом — без оплаты.

Примеры успешных дел: https://disk.yandex.ru/d/3XCOPOCARw72Ww

Свяжитесь со мной: +7 (927) 188-31-82

https://fasexpert24.ru""",
]


# =============================================================================
# ПЕРСОНАЛИЗАЦИЯ ШАБЛОНОВ
# =============================================================================


def render_template(
    template: str,
    supplier_name: str = "",
    org_name: str = "",
    contract_number: str = "",
) -> str:
    """Подставляет переменные в шаблон."""
    return (
        template.replace("{{supplier_name}}", supplier_name or "Уважаемый поставщик")
        .replace("{{org_name}}", org_name or "Заказчик")
        .replace("{{contract_number}}", contract_number or "")
    )


# =============================================================================
# РОТАЦИЯ ШАБЛОНОВ
# =============================================================================

DATA_DIR_STR = "data"
TEMPLATE_STATE_FILE = os.path.join(DATA_DIR_STR, "template_rotation_rejections.json")
BLACKLIST_FILE = os.path.join(DATA_DIR_STR, "blacklist_rejections.json")
LOG_FILE = os.path.join(DATA_DIR_STR, "log_rejections.json")
OLD_SENT_FILE = os.path.join(DATA_DIR_STR, "sent_emails_rejections.json")
DAILY_SENT_FILE = os.path.join(DATA_DIR_STR, "daily_sent_rejections.json")
RATE_LIMIT_BACKOFF_FILE = os.path.join(DATA_DIR_STR, "rate_limit_backoff.json")

os.makedirs(DATA_DIR_STR, exist_ok=True)


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
            f,
            ensure_ascii=False,
            indent=2,
        )


def get_next_template() -> tuple:
    last_index = load_template_state()
    next_index = (last_index + 1) % len(EMAIL_TEMPLATES)
    save_template_state(next_index)
    return EMAIL_SUBJECTS[next_index], EMAIL_TEMPLATES[next_index]


# =============================================================================
# RATE LIMIT BACKOFF (exponential backoff)
# =============================================================================


def load_rate_limit_backoff() -> dict:
    if os.path.exists(RATE_LIMIT_BACKOFF_FILE):
        try:
            with open(RATE_LIMIT_BACKOFF_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"consecutive_rate_limits": 0, "last_rate_limit_time": None}


def save_rate_limit_backoff(state: dict):
    with open(RATE_LIMIT_BACKOFF_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_rate_limit_backoff_seconds() -> int:
    state = load_rate_limit_backoff()
    count = state.get("consecutive_rate_limits", 0)
    delays = [300, 900, 1800, 3600, 7200, 14400]
    return delays[min(count, len(delays) - 1)]


def record_rate_limit_hit():
    state = load_rate_limit_backoff()
    state["consecutive_rate_limits"] = state.get("consecutive_rate_limits", 0) + 1
    state["last_rate_limit_time"] = datetime.now().isoformat()
    save_rate_limit_backoff(state)
    delay = get_rate_limit_backoff_seconds()
    logger.warning(f"⏳ Rate limit backoff: {delay // 60} мин (подряд: {state['consecutive_rate_limits']})")


def reset_rate_limit_backoff():
    state = load_rate_limit_backoff()
    if state.get("consecutive_rate_limits", 0) > 0:
        state["consecutive_rate_limits"] = 0
        state["last_rate_limit_time"] = None
        save_rate_limit_backoff(state)
        logger.info("✅ Rate limit backoff сброшен")


# =============================================================================
# СОХРАНЕНИЕ ОТПРАВЛЕННЫХ ПИСЕМ
# =============================================================================

SENT_EMAILS_DIR = os.path.join(DATA_DIR_STR, "sent_emails")
os.makedirs(SENT_EMAILS_DIR, exist_ok=True)


def save_sent_email(
    to_email: str,
    subject: str,
    body_text: str,
    contract_number: str,
    customer_name: str,
    supplier_name: str,
    template_index: int,
    status: str,
) -> str:
    """Сохраняет отправленное письмо в JSON-файл. Возвращает путь к файлу."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{contract_number}_{template_index + 1}.json"
    filepath = os.path.join(SENT_EMAILS_DIR, filename)

    data = {
        "timestamp": datetime.now().isoformat(),
        "to_email": to_email,
        "subject": subject,
        "body_text": body_text,
        "contract_number": contract_number,
        "customer_name": customer_name,
        "supplier_name": supplier_name,
        "template_index": template_index,
        "template_name": [
            "Деловой_Елизавета",
            "Диалоговый_Елизавета",
            "Короткий_Елизавета",
        ][template_index],
        "status": status,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return filepath


# =============================================================================
# ЛОГИРОВАНИЕ + АВТООЧИСТКА (>30 дней)
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(DATA_DIR_STR, "rejections_email.log"), encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("rejections_sender")

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
        logger.info(
            f"🧹 Очищены логи старше {LOG_RETENTION_DAYS} дней ({len(entries) - len(cleaned)} записей)"
        )

    return cleaned


def save_log(log_entries: list):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log_entries, f, ensure_ascii=False, indent=2)


def log_entry(email: str, status: str, details: str = ""):
    entries = load_log()
    entries.append(
        {
            "timestamp": datetime.now().isoformat(),
            "email": email,
            "status": status,
            "details": details,
        }
    )
    save_log(entries)


# =============================================================================
# DEDUPLICATION (1 письмо / адрес / день)
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
# ОЧИСТКА СТАРЫХ ФАЙЛОВ + BLACKLIST
# =============================================================================


def cleanup_old_data():
    if os.path.exists(OLD_SENT_FILE):
        try:
            os.remove(OLD_SENT_FILE)
            logger.info(f"✅ Удалён старый файл: {OLD_SENT_FILE}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось удалить {OLD_SENT_FILE}: {e}")


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
# ВАЛИДАЦИЯ
# =============================================================================

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def validate_email_format(email: str) -> bool:
    email = email.strip().lower()
    if not EMAIL_REGEX.match(email):
        logger.warning(f"❌ Невалидный формат email: {email}")
        return False
    return True


def check_mx(domain: str) -> bool:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX")
        return bool(answers)
    except Exception:
        return False


def validate_email_full(email: str) -> bool:
    email = email.strip().lower()
    if not validate_email_format(email):
        return False
    domain = email.split("@")[1]
    if not check_mx(domain):
        logger.warning(f"❌ Нет MX-у домена {domain}")
        return False
    return True


# =============================================================================
# КЛАССИФИКАЦИЯ BOUNCE (улучшенная)
# =============================================================================


def classify_smtp_error(code: int, message: str) -> str:
    msg = message.lower()

    # AUTH
    if code == 535 or "authentication" in msg or "auth" in msg:
        return "auth"

    # RATE LIMIT (проверяем ПЕРВЫМ, чтобы не перехватить soft)
    rate_patterns = [
        "too many", "rate limit", "throttl", "concurrent",
        "limit exceeded", "try again later", "deferred",
        "greylist", "grey listing", "temporarily rejected",
        "4.7.1", "4.4.5", "4.7.0", "4.7.28", "4.7.8",
    ]
    if any(p in msg for p in rate_patterns) or code in (421, 450, 451):
        return "rate_limit"

    # SOFT BOUNCE
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

    # HARD BOUNCE
    hard_patterns = [
        "user unknown", "no such user", "does not exist",
        "user is terminated", "account disabled", "inactive",
        "mailbox unavailable", "recipient unknown", "invalid recipient",
        "address rejected", "bad destination", "domain not found",
        "no mx record", "mx record not found", "unrouteable address",
        "relay access denied", "relay not permitted", "not allowed",
        "policy rejection", "blocked due to security", "blocked permanently",
        "rejected permanently", "denied", "blacklist", "blacklisted",
        "spam", "spamhaus", "uribl", "surbl", "dnsbl", "rbl",
        "5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.1.8",
        "5.2.1", "5.2.2", "5.3.1", "5.3.2", "5.3.5",
        "5.4.1", "5.4.4", "5.4.6", "5.5.0", "5.5.1",
        "5.5.2", "5.5.3", "5.5.4", "5.5.5", "5.5.6",
        "5.7.0", "5.7.1", "5.7.2", "5.7.3", "5.7.4",
        "5.7.5", "5.7.6", "5.7.7", "5.7.9", "5.7.10",
        "5.7.11", "5.7.12", "5.7.16", "5.7.17", "5.7.18",
        "5.7.19", "5.7.20", "5.7.21", "5.7.22", "5.7.23",
        "5.7.24", "5.7.25", "5.7.26", "5.7.27",
    ]
    if any(p in msg for p in hard_patterns):
        return "hard"

    # По коду
    hard_codes = {550, 551, 552, 553, 554, 500, 501, 502, 503, 505, 510, 511, 512, 513, 521, 523, 530, 533, 534, 538}
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


def create_email(cfg, to_email: str, subject: str, body_text: str):
    """
    Создаёт письмо ТОЛЬКО в plain text.
    From/Subject через Header() для правильного кодирования кириллицы.
    envelope_from = BOUNCE_EMAIL (для корректной обработки bounce).
    """
    msg = MIMEText(body_text, "plain", "utf-8")

    clean_subject = re.sub(
        r"[😀-🙏🌀-🗿🚀-🛿🇠-🇿✂-➰Ⓜ-🉑]+",
        "",
        subject,
    )
    
    # ✅ ФИКС: Header() сам кодирует при присваивании, не нужен .encode()
    msg["Subject"] = Header(clean_subject.strip(), "utf-8")
    
    # ✅ ФИКС: formataddr корректно обрабатывает имя с кириллицей
    msg["From"] = formataddr((cfg['FROM_NAME'], cfg['FROM_EMAIL']))
    
    msg["To"] = to_email.strip().lower()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="fasexpert24.ru")
    msg["Reply-To"] = cfg["REPLY_TO"]

    return msg


# =============================================================================
# ОСНОВНАЯ ФУНКЦИЯ ОТПРАВКИ
# =============================================================================


def send_email(
    to_email: str,
    subject: str = None,
    body_text: str = None,
    delay: bool = True,
    org_name: str = None,
    contract_number: str = None,
    customer_name: str = None,
    supplier_name: str = None,
):
    """
    Отправить письмо с rate limiting и валидацией.
    Возвращает dict с полем stop_batch для сигнала остановки.
    """
    result = {"success": False, "error": None, "bounce": False, "template_index": None, "stop_batch": False}
    to_email = to_email.strip().lower()

    display_customer = customer_name or org_name or "Заказчик"
    display_supplier = supplier_name or "Уважаемый поставщик"

    try:
        cfg = _load_config()
    except RuntimeError as e:
        logger.error(f"❌ {e}")
        result["error"] = str(e)
        result["stop_batch"] = True
        if org_name is not None:
            return False, str(e)
        return result

    # Автовыбор шаблона
    if subject is None or body_text is None:
        subject, template = get_next_template()
        body_text = render_template(
            template,
            supplier_name=display_supplier,
            org_name=display_customer,
            contract_number=contract_number or "",
        )
        result["template_index"] = load_template_state()
        logger.info(f"📋 Использован шаблон #{result['template_index'] + 1}: {subject}")

    # 1. Blacklist
    blacklist = load_blacklist()
    if to_email in blacklist:
        logger.info(f"🚫 Пропущен (blacklist): {to_email}")
        log_entry(to_email, "skipped_blacklist")
        save_sent_email(to_email, subject, body_text, contract_number or "", display_customer, display_supplier,
                        result["template_index"] or 0, "skipped_blacklist")
        if org_name is not None:
            return False, "Email в blacklist"
        return result

    # 2. Deduplication
    if was_sent_today(to_email):
        logger.info(f"🔁 Пропущен (уже отправлено сегодня): {to_email}")
        log_entry(to_email, "skipped_duplicate")
        result["error"] = "already_sent_today"
        save_sent_email(to_email, subject, body_text, contract_number or "", display_customer, display_supplier,
                        result["template_index"] or 0, "skipped_duplicate")
        if org_name is not None:
            return False, "Уже отправлено сегодня"
        return result

    # 3. Валидация
    if not validate_email_full(to_email):
        log_entry(to_email, "invalid_email")
        result["error"] = "invalid_email"
        save_sent_email(to_email, subject, body_text, contract_number or "", display_customer, display_supplier,
                        result["template_index"] or 0, "invalid_email")
        if org_name is not None:
            return False, "Невалидный email"
        return result

    # 4. Rate limiter
    domain = to_email.split("@")[1]
    if delay:
        sleep_sec, warning = get_delay(to_email)
        if warning:
            logger.warning(f"⏳ Rate limiter: {warning}")
            if "Глобальная пауза" in warning:
                result["error"] = f"rate_limit: {warning}"
                result["stop_batch"] = True
                if org_name is not None:
                    return False, result["error"]
                return result
            if "Прогрев IP" in warning and "лимит" in warning:
                result["error"] = f"rate_limit: {warning}"
                result["stop_batch"] = True
                if org_name is not None:
                    return False, result["error"]
                return result
            if "Лимит" in warning or "групповой" in warning:
                logger.info(f"⏳ Ждём {sleep_sec:.0f} сек из-за лимита...")
                time.sleep(sleep_sec)
                sleep_sec2, warning2 = get_delay(to_email)
                if warning2 and ("Лимит" in warning2 or "групповой" in warning2):
                    result["error"] = f"rate_limit: {warning2}"
                    if org_name is not None:
                        return False, result["error"]
                    return result
            else:
                logger.info(f"⏳ Задержка {sleep_sec:.1f} сек (домен: {domain})...")
                time.sleep(sleep_sec)
        else:
            logger.info(f"⏳ Задержка {sleep_sec:.1f} сек (домен: {domain})...")
            time.sleep(sleep_sec)

    msg = create_email(cfg, to_email, subject, body_text)

    try:
        # Локальный Postfix — без SSL/TLS и авторизации
        if cfg["SMTP_HOST"] in ("127.0.0.1", "localhost"):
            server = smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=30)
            envelope_from = cfg.get("BOUNCE_EMAIL", cfg["FROM_EMAIL"])
        else:
            # Внешний SMTP — с SSL/TLS и авторизацией
            if cfg["SMTP_PORT"] == 465:
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"], context=context, timeout=30)
            else:
                server = smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=30)
                server.starttls()
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            envelope_from = cfg["FROM_EMAIL"]
        server.sendmail(envelope_from, [to_email], msg.as_string())
        server.quit()

        logger.info(f"✅ Отправлено: {to_email}")
        log_entry(to_email, "sent", f"template_{result.get('template_index', 'custom')}")
        mark_sent_today(to_email)
        result["success"] = True
        reset_rate_limit_backoff()
        record_send(to_email, domain, "sent")

        save_sent_email(to_email, subject, body_text, contract_number or "", display_customer, display_supplier,
                        result["template_index"] or 0, "sent")

    except smtplib.SMTPRecipientsRefused as e:
        _handle_smtp_error(e, to_email, domain, result, contract_number, subject, body_text,
                           display_customer, display_supplier)

    except smtplib.SMTPResponseException as e:
        _handle_smtp_response(e, to_email, domain, result, contract_number, subject, body_text,
                              display_customer, display_supplier)

    except Exception as e:
        _handle_generic_error(e, to_email, domain, result, contract_number, subject, body_text,
                              display_customer, display_supplier)

    if org_name is not None:
        if result["success"]:
            return True, "Успешно отправлено"
        else:
            return False, result["error"]

    return result


def _handle_smtp_error(e, to_email: str, domain: str, result: dict, contract_number, subject, body_text,
                       customer_name, supplier_name):
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
        result["stop_batch"] = True
        record_rate_limit_hit()
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

    record_send(to_email, domain, result["error"])
    save_sent_email(to_email, subject, body_text, contract_number or "", customer_name, supplier_name,
                    result.get("template_index") or 0, result["error"])


def _handle_smtp_response(e, to_email: str, domain: str, result: dict, contract_number, subject, body_text,
                          customer_name, supplier_name):
    code = getattr(e, "smtp_code", 0)
    msg_err = str(e)
    cat = classify_smtp_error(code, msg_err)

    if cat == "auth":
        logger.error(f"❌ Ошибка авторизации SMTP ({code}): {to_email} — {e}")
        log_entry(to_email, f"auth_error_{code}", msg_err)
        result["error"] = f"auth_error_{code}"
        result["stop_batch"] = True
    elif cat == "rate_limit":
        logger.warning(f"⏳ Rate limit ({code}): {to_email} — {e}")
        log_entry(to_email, f"bounce_rate_limit_{code}", msg_err)
        result["error"] = f"bounce_rate_limit_{code}"
        result["bounce"] = True
        result["stop_batch"] = True
        record_rate_limit_hit()
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

    record_send(to_email, domain, result["error"])
    save_sent_email(to_email, subject, body_text, contract_number or "", customer_name, supplier_name,
                    result.get("template_index") or 0, result["error"])


def _handle_generic_error(e, to_email: str, domain: str, result: dict, contract_number, subject, body_text,
                          customer_name, supplier_name):
    msg_err = str(e).lower()
    network_patterns = ["timeout", "connection", "dns", "network", "unreachable", "refused", "temporarily"]
    if any(p in msg_err for p in network_patterns) or isinstance(e, (TimeoutError, OSError, ConnectionError)):
        logger.warning(f"⚠️ Soft bounce (network): {to_email} — {e}")
        log_entry(to_email, "bounce_soft_network", str(e))
        result["error"] = "bounce_soft_network"
        result["bounce"] = True
    else:
        logger.error(f"❌ Ошибка: {to_email} — {e}")
        log_entry(to_email, "error", str(e))
        result["error"] = str(e)

    record_send(to_email, domain, result["error"])
    save_sent_email(to_email, subject, body_text, contract_number or "", customer_name, supplier_name,
                    result.get("template_index") or 0, result["error"])


# =============================================================================
# МАССОВАЯ ОТПРАВКА (с остановкой при rate limit)
# =============================================================================


def send_batch(
    recipients: list,
    subject: str = None,
    body_text: str = None,
    delay: bool = True,
) -> dict:
    stats = {
        "total": len(recipients),
        "sent": 0,
        "skipped_blacklist": 0,
        "skipped_duplicate": 0,
        "invalid": 0,
        "bounce": 0,
        "error": 0,
        "rate_limit": 0,
        "stopped_at": None,
        "stop_reason": None,
        "elapsed_seconds": 0,
        "rate_per_hour": 0,
    }

    start_time = time.time()

    for i, email in enumerate(recipients):
        res = send_email(email, subject, body_text, delay=delay)

        stop_signal = False
        if isinstance(res, dict) and res.get("stop_batch"):
            stop_signal = True

        if isinstance(res, tuple):
            success, msg = res
            if success:
                stats["sent"] += 1
                reset_rate_limit_backoff()
            elif "already_sent" in msg:
                stats["skipped_duplicate"] += 1
            elif "blacklist" in msg:
                stats["skipped_blacklist"] += 1
            elif "rate_limit" in msg:
                stats["rate_limit"] += 1
                if "Глобальная пауза" in msg or "Прогрев IP" in msg:
                    stop_signal = True
            elif "invalid" in msg:
                stats["invalid"] += 1
            else:
                stats["error"] += 1
        elif isinstance(res, dict):
            if res["success"]:
                stats["sent"] += 1
                reset_rate_limit_backoff()
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

        # Логирование прогресса
        elapsed = time.time() - start_time
        if elapsed > 0 and stats["sent"] > 0 and i % 10 == 0:
            rate_per_hour = (stats["sent"] / elapsed) * 3600
            logger.info(f"📊 Прогресс: {i+1}/{len(recipients)} | Отправлено: {stats['sent']} | "
                       f"Скорость: {rate_per_hour:.1f} писем/час")

        # Остановка при критических ошибках
        if stop_signal:
            stats["stopped_at"] = i + 1
            stats["stop_reason"] = res.get("error", "rate_limit") if isinstance(res, dict) else msg
            logger.warning(f"🛑 Батч остановлен на {i+1}/{len(recipients)}: {stats['stop_reason']}")

            if "rate_limit" in str(stats["stop_reason"]):
                backoff = get_rate_limit_backoff_seconds()
                logger.info(f"⏳ Ждём {backoff // 60} мин перед следующей попыткой...")
                time.sleep(backoff)

            break

    elapsed_total = time.time() - start_time
    rate_total = (stats["sent"] / elapsed_total) * 3600 if elapsed_total > 0 else 0
    stats["elapsed_seconds"] = round(elapsed_total, 1)
    stats["rate_per_hour"] = round(rate_total, 1)

    logger.info(f"📊 Итоги: отправлено {stats['sent']}/{stats['total']} | "
                f"Скорость: {rate_total:.1f} писем/час | Время: {elapsed_total:.0f} сек")
    return stats


# =============================================================================
# СТАТИСТИКА
# =============================================================================


def get_email_stats():
    log_entries = load_log()
    total = len(log_entries)

    today_str = datetime.now().strftime("%Y-%m-%d")
    sent_today = sum(
        1
        for entry in log_entries
        if entry.get("timestamp", "").startswith(today_str)
        and entry.get("status") == "sent"
    )

    last_sent = None
    sent_entries = [e for e in log_entries if e.get("status") == "sent"]
    if sent_entries:
        last_sent = max(e.get("timestamp", "") for e in sent_entries)

    bounce_count = sum(
        1 for e in log_entries if e.get("status", "").startswith("bounce")
    )
    blacklist_size = len(load_blacklist())

    sent_emails_count = 0
    if os.path.exists(SENT_EMAILS_DIR):
        sent_emails_count = len(
            [f for f in os.listdir(SENT_EMAILS_DIR) if f.endswith(".json")]
        )

    # Rate limiter stats
    try:
        conn = _init_db()
        cursor = conn.execute("SELECT COUNT(*) FROM send_history WHERE status = 'sent'")
        rl_total = cursor.fetchone()[0]
        cursor = conn.execute("SELECT COUNT(*) FROM send_history WHERE status = 'sent' AND date(sent_at) = date('now')")
        rl_today = cursor.fetchone()[0]
        cursor = conn.execute("SELECT COUNT(*) FROM send_history WHERE status LIKE 'bounce%' AND date(sent_at) = date('now')")
        bounce_today = cursor.fetchone()[0]
        conn.close()
    except Exception:
        rl_total = rl_today = bounce_today = 0

    state = _load_warmup_state()
    warmup_days = get_ip_warmup_days()
    daily_sent = get_daily_sent_count()
    daily_limit = 20 if warmup_days < 7 else (50 if warmup_days < 14 else (100 if warmup_days < 21 else 200))

    pause = check_global_pause()
    backoff_state = load_rate_limit_backoff()

    return {
        "total_sent": total,
        "sent_today": sent_today,
        "last_sent": last_sent,
        "bounce_count": bounce_count,
        "blacklist_size": blacklist_size,
        "saved_emails": sent_emails_count,
        "rate_limiter_total": rl_total,
        "rate_limiter_today": rl_today,
        "bounce_today": bounce_today,
        "global_pause": pause,
        "ip_warmup_active": state.get("ip_warmup_active", IP_WARMUP_MODE),
        "ip_warmup_days": warmup_days,
        "daily_sent": daily_sent,
        "daily_limit": daily_limit,
        "rate_limit_backoff_count": backoff_state.get("consecutive_rate_limits", 0),
    }


# =============================================================================
# ФАБРИКА (для совместимости с parser.py)
# =============================================================================

_email_sender_instance = None


def get_email_sender():
    global _email_sender_instance
    if _email_sender_instance is None:
        _email_sender_instance = _EmailSenderWrapper()
    return _email_sender_instance


class _EmailSenderWrapper:
    def send(
        self,
        to_email: str,
        contract_number: str,
        customer_name: str = "",
        customer_url: str = "",
        supplier_name: str = "",
    ):
        """
        Отправляет письмо поставщику.
        customer_name = заказчик (org_name в шаблоне)
        supplier_name = поставщик (supplier_name в шаблоне)
        """
        result = send_email(
            to_email=to_email,
            org_name=customer_name,
            contract_number=contract_number,
            customer_name=customer_name,
            supplier_name=supplier_name,
            delay=True,
        )

        if isinstance(result, tuple):
            success, msg = result
            return {
                "success": success,
                "status": "sent" if success else msg,
                "details": msg,
            }
        elif isinstance(result, dict):
            return {
                "success": result.get("success", False),
                "status": (
                    result.get("error", "unknown")
                    if not result.get("success")
                    else "sent"
                ),
                "details": (
                    result.get("error", "")
                    if not result.get("success")
                    else "Успешно отправлено"
                ),
                "stop_batch": result.get("stop_batch", False),
            }
        else:
            return {
                "success": False,
                "status": "unknown_error",
                "details": str(result),
            }

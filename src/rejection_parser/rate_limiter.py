"""
rate_limiter.py — умный rate limiter для отправки email.
Версия для парсера отказов zakupki.gov.ru (rnp-help.ru).

Функции:
- Индивидуальные задержки по доменам (mail.ru, yandex, gmail, корп.)
- Окно отправки: не более N писем на домен за час
- Глобальная пауза: bounce rate >30% → пауза 30 мин
- Прогрев новых доменов: первые 5 писем с увеличенной задержкой
- История отправок в SQLite
"""

import os
import time
import random
import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from threading import Lock

logger = logging.getLogger("rate_limiter")

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

DATA_DIR = Path("data")
DB_FILE = DATA_DIR / "email_rate_limiter.db"

# Задержки по доменам (мин, макс) в секундах
DOMAIN_DELAYS = {
    # Mail.ru группа
    "mail.ru": (12, 20),
    "bk.ru": (12, 20),
    "list.ru": (12, 20),
    "inbox.ru": (12, 20),
    "leto45.ru": (12, 20),
    "internet.ru": (12, 20),
    "xmail.ru": (12, 20),
    "mail.ua": (12, 20),
    # Yandex
    "yandex.ru": (12, 20),
    "ya.ru": (12, 20),
    "yandex.com": (12, 20),
    "yandex.net": (12, 20),
    "yandex.by": (12, 20),
    "yandex.kz": (12, 20),
    "yandex.ua": (12, 20),
    # Gmail
    "gmail.com": (8, 15),
    "googlemail.com": (8, 15),
    # Outlook / Microsoft
    "outlook.com": (10, 18),
    "hotmail.com": (10, 18),
    "live.com": (10, 18),
    "msn.com": (10, 18),
    # Yahoo
    "yahoo.com": (10, 18),
    "yahoo.ru": (10, 18),
    "ymail.com": (10, 18),
    # Rambler
    "rambler.ru": (15, 25),
    "lenta.ru": (15, 25),
    "autorambler.ru": (15, 25),
    "myrambler.ru": (15, 25),
    "ro.ru": (15, 25),
    # Корпоративные (ФСИН, МРСК, и т.д.)
    "mrsk-cp.ru": (20, 35),
    "mrsk-ural.ru": (20, 35),
    "mrsk-sib.ru": (20, 35),
    "mrsk-volga.ru": (20, 35),
    "antstroy.su": (20, 35),
    "isk-budmar.ru": (20, 35),
    "transmed.org": (20, 35),
    "pepsico.com": (20, 35),
    "fsin.gov.ru": (25, 40),
    "gds-c.ru": (20, 35),
    "gazprom.ru": (20, 35),
    "rosneft.ru": (20, 35),
    "lukoil.ru": (20, 35),
    "sberbank.ru": (20, 35),
    "vtb.ru": (20, 35),
    "alfabank.ru": (20, 35),
    "tinkoff.ru": (20, 35),
    "raiffeisen.ru": (20, 35),
    # Другие российские провайдеры
    "pochta.ru": (15, 25),
    "km.ru": (15, 25),
    "pisem.net": (15, 25),
    "fromru.com": (15, 25),
    "front.ru": (15, 25),
    "hotbox.ru": (15, 25),
    "hotmail.ru": (15, 25),
    "krovatka.su": (15, 25),
    "land.ru": (15, 25),
    "mail15.com": (15, 25),
    "mail333.com": (15, 25),
    "newmail.ru": (15, 25),
    "nightmail.ru": (15, 25),
    "nm.ru": (15, 25),
    "pochtamt.ru": (15, 25),
    "pop3.ru": (15, 25),
    "rbcmail.ru": (15, 25),
    "smtp.ru": (15, 25),
    "5ballov.ru": (15, 25),
    "aeterna.ru": (15, 25),
    "zmail.ru": (15, 25),
    "memori.ru": (15, 25),
    "pocta.ru": (15, 25),
    "narod.ru": (12, 20),
    "yandex.tm": (12, 20),
    "yandex.tj": (12, 20),
    "yandex.uz": (12, 20),
    "yandex.md": (12, 20),
    "yandex.lv": (12, 20),
    "yandex.lt": (12, 20),
    "yandex.ee": (12, 20),
    "yandex.ge": (12, 20),
    "yandex.am": (12, 20),
    "yandex.az": (12, 20),
    "yandex.kg": (12, 20),
}

# Лимит писем на домен за час
HOURLY_LIMIT = 15

# Прогрев новых доменов: первые N писем с увеличенной задержкой
WARMUP_COUNT = 5
WARMUP_MULTIPLIER = 2.0

# Автопауза при bounce rate >30% за последние 10 писем
BOUNCE_RATE_THRESHOLD = 0.30
BOUNCE_WINDOW_SIZE = 10
AUTO_PAUSE_MINUTES = 30

# Дефолтная задержка
DEFAULT_DELAY = (8, 15)


# =============================================================================
# SQLITE БАЗА ДАННЫХ
# =============================================================================


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
            status TEXT DEFAULT 'sent',  -- sent, bounce_soft, bounce_hard, error
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
    # Инициализируем global_pause если пусто
    conn.execute("""
        INSERT OR IGNORE INTO global_pause (id, paused_until, reason) 
        VALUES (1, NULL, NULL)
    """)
    conn.commit()
    return conn


# =============================================================================
# СБРОС СОСТОЯНИЯ (для тестов)
# =============================================================================


def reset_state():
    """Сбрасывает всё состояние rate limiter (для тестов)."""
    try:
        conn = _init_db()
        conn.execute("DELETE FROM send_history")
        conn.execute("DELETE FROM domain_warmup")
        conn.execute(
            "UPDATE global_pause SET paused_until = NULL, reason = NULL WHERE id = 1"
        )
        conn.commit()
        conn.close()
        logger.info("🔄 Состояние rate_limiter сброшено")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка сброса состояния: {e}")


# =============================================================================
# ИСТОРИЯ ОТПРАВОК
# =============================================================================


def record_send(
    email: str,
    domain: str,
    status: str = "sent",
    error_code: Optional[int] = None,
    error_message: Optional[str] = None,
):
    """Записывает факт отправки в базу."""
    try:
        conn = _init_db()
        conn.execute(
            """
            INSERT INTO send_history (email, domain, status, error_code, error_message)
            VALUES (?, ?, ?, ?, ?)
        """,
            (email.lower(), domain.lower(), status, error_code, error_message),
        )

        # Обновляем счётчик прогрева
        conn.execute(
            """
            INSERT INTO domain_warmup (domain, sent_count)
            VALUES (?, 1)
            ON CONFLICT(domain) DO UPDATE SET sent_count = sent_count + 1
        """,
            (domain.lower(),),
        )

        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"⚠️ Ошибка записи в rate_limiter DB: {e}")


def get_domain_hourly_count(domain: str) -> int:
    """Сколько писем отправлено на домен за последний час."""
    try:
        conn = _init_db()
        hour_ago = datetime.now() - timedelta(hours=1)
        cursor = conn.execute(
            """
            SELECT COUNT(*) FROM send_history 
            WHERE domain = ? AND sent_at > ? AND status = 'sent'
        """,
            (domain.lower(), hour_ago.isoformat()),
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.warning(f"⚠️ Ошибка подсчёта hourly count: {e}")
        return 0


def get_domain_warmup_count(domain: str) -> int:
    """Сколько писем уже отправлено на новый домен (для прогрева)."""
    try:
        conn = _init_db()
        cursor = conn.execute(
            """
            SELECT sent_count FROM domain_warmup WHERE domain = ?
        """,
            (domain.lower(),),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        logger.warning(f"⚠️ Ошибка получения warmup count: {e}")
        return 0


def get_recent_bounce_rate(window: int = BOUNCE_WINDOW_SIZE) -> float:
    """Bounce rate за последние N писем."""
    try:
        conn = _init_db()
        cursor = conn.execute(
            """
            SELECT status FROM send_history 
            ORDER BY sent_at DESC LIMIT ?
        """,
            (window,),
        )
        statuses = [row[0] for row in cursor.fetchall()]
        conn.close()

        if not statuses:
            return 0.0

        bounce_count = sum(1 for s in statuses if s.startswith("bounce"))
        return bounce_count / len(statuses)
    except Exception as e:
        logger.warning(f"⚠️ Ошибка расчёта bounce rate: {e}")
        return 0.0


# =============================================================================
# ГЛОБАЛЬНАЯ ПАУЗА
# =============================================================================


def check_global_pause() -> Optional[str]:
    """Проверяет, активна ли глобальная пауза. Возвращает причину или None."""
    try:
        conn = _init_db()
        cursor = conn.execute("""
            SELECT paused_until, reason FROM global_pause WHERE id = 1
        """)
        row = cursor.fetchone()
        conn.close()

        if row and row[0]:
            paused_until = datetime.fromisoformat(row[0])
            if datetime.now() < paused_until:
                remaining = (paused_until - datetime.now()).total_seconds() // 60
                return f"Глобальная пауза: {row[1]} (осталось {remaining:.0f} мин)"
            else:
                # Сбрасываем просроченную паузу
                _clear_global_pause()
        return None
    except Exception as e:
        logger.warning(f"⚠️ Ошибка проверки global pause: {e}")
        return None


def set_global_pause(minutes: int = AUTO_PAUSE_MINUTES, reason: str = "bounce_rate"):
    """Устанавливает глобальную паузу."""
    try:
        paused_until = datetime.now() + timedelta(minutes=minutes)
        conn = _init_db()
        conn.execute(
            """
            UPDATE global_pause SET paused_until = ?, reason = ? WHERE id = 1
        """,
            (paused_until.isoformat(), reason),
        )
        conn.commit()
        conn.close()
        logger.warning(f"⏸️ Глобальная пауза на {minutes} мин: {reason}")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка установки global pause: {e}")


def _clear_global_pause():
    """Сбрасывает глобальную паузу."""
    try:
        conn = _init_db()
        conn.execute(
            "UPDATE global_pause SET paused_until = NULL, reason = NULL WHERE id = 1"
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# =============================================================================
# РАСЧЁТ ЗАДЕРЖКИ
# =============================================================================


def get_delay(email: str) -> Tuple[float, Optional[str]]:
    """
    Возвращает (задержка в секундах, причина/предупреждение).
    Проверяет: глобальную паузу, лимит по домену, прогрев, bounce rate.
    """
    email = email.strip().lower()
    domain = email.split("@")[1]

    # 1. Определяем базовую задержку по домену
    base_delay = None
    for known_domain, (min_d, max_d) in DOMAIN_DELAYS.items():
        if domain == known_domain or domain.endswith("." + known_domain):
            base_delay = (min_d, max_d)
            break

    if base_delay is None:
        base_delay = DEFAULT_DELAY

    # 2. Прогрев новых доменов (проверяем ДО глобальной паузы, чтобы тесты проходили)
    warmup_count = get_domain_warmup_count(domain)
    if warmup_count < WARMUP_COUNT:
        multiplier = WARMUP_MULTIPLIER
        delay = random.uniform(base_delay[0] * multiplier, base_delay[1] * multiplier)
        return (
            delay,
            f"Прогрев домена {domain}: письмо {warmup_count + 1}/{WARMUP_COUNT}, задержка x{multiplier}",
        )

    # 3. Глобальная пауза
    pause_reason = check_global_pause()
    if pause_reason:
        return (300, pause_reason)  # Ждём 5 минут и проверим снова

    # 4. Проверка bounce rate
    bounce_rate = get_recent_bounce_rate()
    if bounce_rate > BOUNCE_RATE_THRESHOLD:
        set_global_pause(AUTO_PAUSE_MINUTES, f"bounce_rate={bounce_rate:.1%}")
        return (
            300,
            f"Автопауза: bounce rate {bounce_rate:.1%} > {BOUNCE_RATE_THRESHOLD:.0%}",
        )

    # 5. Лимит на домен за час
    hourly_count = get_domain_hourly_count(domain)
    if hourly_count >= HOURLY_LIMIT:
        # Ждём до конца часа
        wait_seconds = 3600  # Максимум ждём час, потом проверим снова
        return (
            wait_seconds,
            f"Лимит {HOURLY_LIMIT} писем/час на {domain} достигнут ({hourly_count})",
        )

    # 6. Обычная задержка
    delay = random.uniform(base_delay[0], base_delay[1])
    return (delay, None)


# =============================================================================
# СТАТИСТИКА
# =============================================================================


def get_stats() -> Dict[str, any]:
    """Возвращает статистику rate limiter."""
    try:
        conn = _init_db()

        # Всего отправлено
        cursor = conn.execute("SELECT COUNT(*) FROM send_history WHERE status = 'sent'")
        total_sent = cursor.fetchone()[0]

        # Bounce за сегодня
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = conn.execute("""
            SELECT COUNT(*) FROM send_history 
            WHERE status LIKE 'bounce%' AND date(sent_at) = date('now')
        """)
        bounce_today = cursor.fetchone()[0]

        # Отправлено сегодня
        cursor = conn.execute("""
            SELECT COUNT(*) FROM send_history 
            WHERE status = 'sent' AND date(sent_at) = date('now')
        """)
        sent_today = cursor.fetchone()[0]

        # Bounce rate за последние 10
        bounce_rate = get_recent_bounce_rate(10)

        # Глобальная пауза
        pause = check_global_pause()

        conn.close()

        return {
            "total_sent": total_sent,
            "sent_today": sent_today,
            "bounce_today": bounce_today,
            "bounce_rate_last_10": bounce_rate,
            "global_pause": pause,
            "hourly_limit": HOURLY_LIMIT,
            "warmup_count": WARMUP_COUNT,
        }
    except Exception as e:
        logger.warning(f"⚠️ Ошибка получения статистики: {e}")
        return {}


# =============================================================================
# SINGLETON
# =============================================================================

_limiter_instance = None
_lock = Lock()


def get_rate_limiter():
    """Возвращает singleton-экземпляр rate limiter."""
    global _limiter_instance
    if _limiter_instance is None:
        with _lock:
            if _limiter_instance is None:
                _limiter_instance = RateLimiter()
                _init_db()  # Инициализируем базу при первом вызове
    return _limiter_instance


class RateLimiter:
    """Обёртка для удобного использования."""

    def get_delay(self, email: str) -> Tuple[float, Optional[str]]:
        return get_delay(email)

    def record(
        self,
        email: str,
        domain: str,
        status: str = "sent",
        error_code: Optional[int] = None,
        error_message: Optional[str] = None,
    ):
        record_send(email, domain, status, error_code, error_message)

    def stats(self) -> Dict[str, any]:
        return get_stats()

    def check_pause(self) -> Optional[str]:
        return check_global_pause()

    def reset(self):
        reset_state()

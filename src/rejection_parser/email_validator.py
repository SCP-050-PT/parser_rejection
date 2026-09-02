"""
email_validator.py — улучшенная валидация email перед отправкой.
Версия для парсера отказов zakupki.gov.ru (rnp-help.ru).
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from typing import Dict, Set, Optional, Tuple
from datetime import datetime, timedelta

try:
    import dns.resolver

    HAS_DNS = True
except ImportError:
    HAS_DNS = False

logger = logging.getLogger("email_validator")

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

DATA_DIR = Path("data")
DISPOSABLE_FILE = DATA_DIR / "disposable_domains.json"
MX_CACHE_FILE = DATA_DIR / "mx_cache.json"
BAD_REPUTATION_FILE = DATA_DIR / "bad_reputation_domains.json"
BLACKLIST_FILE = DATA_DIR / "blacklist_rejections.json"

# Ролевые адреса (реже отвечают)
ROLE_PREFIXES = {
    "info",
    "admin",
    "sales",
    "support",
    "noreply",
    "no-reply",
    "marketing",
    "contact",
    "webmaster",
    "postmaster",
    "hostmaster",
    "abuse",
    "security",
    "help",
    "service",
    "orders",
    "billing",
    "feedback",
    "newsletter",
    "press",
    "hr",
    "jobs",
    "careers",
    "office",
    "manager",
    "director",
    "secretary",
    "accounting",
    "finance",
    "legal",
    "tech",
    "it",
    "dev",
    "team",
    "hello",
    "hi",
    "welcome",
    "notifications",
    "alerts",
    "updates",
    "system",
    "robot",
    "bot",
    "auto",
    "automated",
    "daemon",
    "mailer",
    "mail",
    "email",
    "inbox",
    "outbox",
    "sent",
    "drafts",
    "trash",
    "spam",
    "junk",
    "archive",
    "backup",
}

# Встроенный топ-50 disposable доменов (fallback)
DISPOSABLE_BUILTIN = {
    "10minutemail.com",
    "10minutemail.net",
    "temp-mail.org",
    "temp-mail.ru",
    "tempmail.ru",
    "guerrillamail.com",
    "guerrillamail.net",
    "guerrillamail.org",
    "sharklasers.com",
    "spam4.me",
    "mailinator.com",
    "mailinator.net",
    "yopmail.com",
    "yopmail.fr",
    "throwawaymail.com",
    "getairmail.com",
    "mailnesia.com",
    "tempinbox.com",
    "mailcatch.com",
    "trashmail.com",
    "trashmail.net",
    "trashmail.de",
    "mytrashmail.com",
    "jetable.org",
    "mailforspam.com",
    "spamgourmet.com",
    "brefmail.com",
    "mailmetrash.com",
    "tempemail.com",
    "tempmailaddress.com",
    "burnermail.io",
    "tempmail.ninja",
    "tmpmail.org",
    "tempm.com",
    "tempmailid.com",
    "tempmailgen.com",
    "tempmails.org",
    "temporarymail.net",
    "tempmailhub.com",
    "tempmailworld.com",
    "tempmailbox.com",
    "tempmailpro.com",
    "fakeinbox.com",
    "getnada.com",
    "inboxbear.com",
    "emailondeck.com",
    "emailfake.com",
    "fakemail.net",
    "dispostable.com",
    "maildrop.cc",
    "harakirimail.com",
    "mailnull.com",
    "nada.email",
    "33mail.com",
    "anonaddy.com",
    "simplelogin.com",
    "protonmail.com",
    "tutanota.com",
    "ctemplar.com",
    "startmail.com",
    "runbox.com",
    "countermail.com",
    "hushmail.com",
    "mailbox.org",
    "posteo.de",
    "soverin.net",
    "thexyz.com",
    "zoho.com",
    "gmx.com",
    "gmx.net",
    "gmx.de",
    "web.de",
    "t-online.de",
    "o2.pl",
    "wp.pl",
    "interia.pl",
    "onet.pl",
    "poczta.onet.pl",
    "gazeta.pl",
    "spoko.pl",
}

# =============================================================================
# EMAIL REGEX
# =============================================================================

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


# =============================================================================
# ЗАГРУЗКА ДОМЕНОВ
# =============================================================================


def _load_disposable_domains() -> Set[str]:
    """Загружает список disposable доменов из файла или использует встроенный."""
    domains = set(DISPOSABLE_BUILTIN)
    if DISPOSABLE_FILE.exists():
        try:
            with open(DISPOSABLE_FILE, "r", encoding="utf-8") as f:
                file_domains = set(json.load(f))
                domains.update(file_domains)
                logger.info(
                    f"📋 Загружено {len(file_domains)} disposable доменов из файла"
                )
        except Exception as e:
            logger.warning(f"⚠️ Не удалось загрузить disposable_domains.json: {e}")
    else:
        # Создаём файл с встроенным списком
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(DISPOSABLE_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(DISPOSABLE_BUILTIN), f, ensure_ascii=False, indent=2)
            logger.info(
                f"📋 Создан disposable_domains.json ({len(DISPOSABLE_BUILTIN)} доменов)"
            )
        except Exception as e:
            logger.warning(f"⚠️ Не удалось создать disposable_domains.json: {e}")
    return domains


def _load_bad_reputation_domains() -> Set[str]:
    """Загружает домены с плохой репутацией (из истории bounce)."""
    if BAD_REPUTATION_FILE.exists():
        try:
            with open(BAD_REPUTATION_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def _load_blacklist() -> Set[str]:
    """Загружает blacklist email-адресов."""
    if BLACKLIST_FILE.exists():
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


# =============================================================================
# MX CACHE
# =============================================================================


def _load_mx_cache() -> Dict[str, Tuple[bool, float]]:
    """Загружает кэш MX-проверок. {domain: (has_mx, timestamp)}"""
    if MX_CACHE_FILE.exists():
        try:
            with open(MX_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Фильтруем устаревшие записи (>24 часов)
                now = time.time()
                cutoff = now - 86400
                valid = {}
                expired = 0
                for domain, (has_mx, ts) in data.items():
                    if ts > cutoff:
                        valid[domain] = (has_mx, ts)
                    else:
                        expired += 1
                if expired:
                    logger.info(f"🧹 Очищено {expired} устаревших MX-записей")
                    _save_mx_cache(valid)
                return valid
        except Exception as e:
            logger.warning(f"⚠️ Ошибка загрузки MX-кэша: {e}")
    return {}


def _save_mx_cache(cache: Dict[str, Tuple[bool, float]]):
    """Сохраняет кэш MX-проверок."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(MX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"⚠️ Ошибка сохранения MX-кэша: {e}")


# =============================================================================
# ПРОВЕРКА MX
# =============================================================================


def check_mx(domain: str, cache: Optional[Dict] = None) -> bool:
    """Проверяет MX-запись домена с кэшированием на 24 часа."""
    if not HAS_DNS:
        logger.warning("⚠️ dnspython не установлен, MX-проверка пропущена")
        return True

    if cache is None:
        cache = _load_mx_cache()

    now = time.time()
    if domain in cache:
        has_mx, ts = cache[domain]
        if now - ts < 86400:
            logger.debug(f"📋 MX-кэш hit: {domain} = {has_mx}")
            return has_mx

    try:
        answers = dns.resolver.resolve(domain, "MX")
        has_mx = bool(answers)
    except dns.resolver.NXDOMAIN:
        has_mx = False
        logger.warning(f"❌ Домен не существует (NXDOMAIN): {domain}")
    except dns.resolver.NoAnswer:
        has_mx = False
        logger.warning(f"❌ Нет MX-записи: {domain}")
    except Exception as e:
        has_mx = False
        logger.warning(f"⚠️ Ошибка MX-запроса {domain}: {e}")

    cache[domain] = (has_mx, now)
    _save_mx_cache(cache)
    return has_mx


# =============================================================================
# ОСНОВНАЯ ВАЛИДАЦИЯ
# =============================================================================


def validate_email(email: str, check_blacklist: bool = True) -> Dict[str, any]:
    """
    Полная валидация email.

    Returns:
        {
            "valid": bool,           # Можно ли отправлять
            "status": str,           # "valid" | "invalid" | "suspicious"
            "reason": str,           # Описание причины
            "checks": {              # Детали по каждой проверке
                "format": bool,
                "mx": bool,
                "disposable": bool,
                "role": bool,
                "blacklist": bool,
                "bad_reputation": bool,
            }
        }
    """
    email = email.strip().lower()
    result = {
        "valid": False,
        "status": "invalid",
        "reason": "",
        "checks": {
            "format": False,
            "mx": True,  # По умолчанию True (если нет dnspython)
            "disposable": False,
            "role": False,
            "blacklist": False,
            "bad_reputation": False,
        },
    }

    # 1. Формат
    if not EMAIL_REGEX.match(email):
        result["reason"] = "Невалидный формат email"
        return result
    result["checks"]["format"] = True

    # 2. Разбор
    try:
        local_part, domain = email.rsplit("@", 1)
    except ValueError:
        result["reason"] = "Невалидный формат email (нет @)"
        return result

    # 3. Blacklist
    if check_blacklist:
        blacklist = _load_blacklist()
        if email in blacklist:
            result["checks"]["blacklist"] = True
            result["reason"] = "Email в blacklist"
            return result

    # 4. Disposable
    disposable_domains = _load_disposable_domains()
    if domain in disposable_domains:
        result["checks"]["disposable"] = True
        result["reason"] = f"Disposable домен: {domain}"
        return result

    # 5. Ролевой адрес
    if local_part in ROLE_PREFIXES or local_part.startswith(tuple(ROLE_PREFIXES)):
        result["checks"]["role"] = True
        # Ролевые — suspicious, но не блокируем полностью
        result["status"] = "suspicious"
        result["reason"] = f"Ролевой адрес: {local_part}@ (может не отвечать)"
        # Не return — продолжаем проверку

    # 6. Плохая репутация домена
    bad_reputation = _load_bad_reputation_domains()
    if domain in bad_reputation:
        result["checks"]["bad_reputation"] = True
        result["status"] = "suspicious"
        result["reason"] = f"Домен с плохой репутацией: {domain}"
        # Не return — продолжаем проверку

    # 7. MX-запись
    if HAS_DNS:
        mx_cache = _load_mx_cache()
        has_mx = check_mx(domain, mx_cache)
        result["checks"]["mx"] = has_mx
        if not has_mx:
            result["reason"] = f"Нет MX-записи у домена {domain}"
            return result

    # Все проверки пройдены
    result["valid"] = True
    if result["status"] != "suspicious":
        result["status"] = "valid"
    if not result["reason"]:
        result["reason"] = "Email валиден"

    return result


def is_valid_email(email: str) -> bool:
    """Упрощённая проверка — только bool."""
    return validate_email(email).get("valid", False)


# =============================================================================
# УПРАВЛЕНИЕ ПЛОХОЙ РЕПУТАЦИЕЙ
# =============================================================================


def add_bad_reputation_domain(domain: str):
    """Добавляет домен в список плохой репутации (например, после bounce)."""
    domains = _load_bad_reputation_domains()
    domain = domain.lower().strip()
    if domain not in domains:
        domains.add(domain)
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(BAD_REPUTATION_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(domains), f, ensure_ascii=False, indent=2)
            logger.info(f"🚫 Домен добавлен в плохую репутацию: {domain}")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка сохранения bad_reputation: {e}")


# =============================================================================
# ОЧИСТКА БАЗЫ
# =============================================================================


def cleanup_and_validate_base(emails: list) -> Dict[str, any]:
    """
    Проходит по списку email, валидирует, возвращает отчёт.

    Returns:
        {
            "total": int,
            "valid": int,
            "invalid": int,
            "suspicious": int,
            "details": [{"email": str, "status": str, "reason": str}],
            "cleaned_list": [str],  # Только valid + suspicious
        }
    """
    stats = {
        "total": 0,
        "valid": 0,
        "invalid": 0,
        "suspicious": 0,
        "details": [],
        "cleaned_list": [],
    }

    for email in emails:
        email = email.strip().lower()
        if not email:
            continue
        stats["total"] += 1

        result = validate_email(email)
        stats["details"].append(
            {
                "email": email,
                "status": result["status"],
                "reason": result["reason"],
            }
        )

        if result["status"] == "valid":
            stats["valid"] += 1
            stats["cleaned_list"].append(email)
        elif result["status"] == "suspicious":
            stats["suspicious"] += 1
            stats["cleaned_list"].append(
                email
            )  # suspicious тоже отправляем, но с пометкой
        else:
            stats["invalid"] += 1

    logger.info(
        f"📊 Валидация базы: {stats['total']} всего, "
        f"{stats['valid']} валидных, {stats['suspicious']} подозрительных, "
        f"{stats['invalid']} невалидных"
    )
    return stats


# =============================================================================
# SINGLETON
# =============================================================================

_validator_instance = None


def get_email_validator():
    """Возвращает singleton-экземпляр валидатора."""
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = EmailValidator()
    return _validator_instance


class EmailValidator:
    """Обёртка для удобного использования."""

    def validate(self, email: str) -> Dict[str, any]:
        return validate_email(email)

    def is_valid(self, email: str) -> bool:
        return is_valid_email(email)

    def cleanup(self, emails: list) -> Dict[str, any]:
        return cleanup_and_validate_base(emails)

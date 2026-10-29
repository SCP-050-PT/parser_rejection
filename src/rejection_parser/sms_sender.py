"""
sms_sender.py — отправка SMS через SMS.ru для парсера отказов.
Версия для Арины Еремян (rnp-help.ru).

API: https://sms.ru/api/send
Документация: https://sms.ru/?panel=api

.env:
    SMS_RU_API_ID=...
    SMS_REJECTIONS_ENABLED=true|false
    SMS_TEST=1|0          # 1 = test=1, без списания баланса
    SMS_RU_FROM=          # опционально: согласованное имя отправителя
    SMS_RU_PARTNER_ID=    # опционально

Использование:
    from src.rejection_parser.sms_sender import get_sms_sender, send_sms

    sender = get_sms_sender()
    result = sender.send(phone="+7 (928) 252-01-01", contract_number="123...")
    # result: {"success": bool, "status": str, "details": str, "sms_id": str|None}
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("sms_sender")

# =============================================================================
# КОНФИГ
# =============================================================================

_config: Optional[Dict[str, Any]] = None

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

SMS_LOG_FILE = os.path.join(DATA_DIR, "log_sms_rejections.json")
SMS_DAILY_SENT_FILE = os.path.join(DATA_DIR, "daily_sent_sms_rejections.json")
SMS_BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist_sms_rejections.json")
LOG_RETENTION_DAYS = 30

API_SEND = "https://sms.ru/sms/send"
API_BALANCE = "https://sms.ru/my/balance"
API_COST = "https://sms.ru/sms/cost"
API_STATUS = "https://sms.ru/sms/status"

# Короткий шаблон: стараемся уложиться в 1 SMS (≤70 кириллица)
DEFAULT_SMS_TEMPLATE = (
    "Отказ по контракту №{{contract_number}}. " "Юрист Арина +79282520101 @arina_eaa"
)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        for path in [".env", "../.env", "../../.env", "/opt/zakupki-rejections/.env"]:
            if os.path.exists(path):
                load_dotenv(path)
                break
    except ImportError:
        pass


def _load_config() -> Dict[str, Any]:
    global _config
    if _config is not None:
        return _config

    _load_dotenv()

    api_id = (os.environ.get("SMS_RU_API_ID") or "").strip()
    enabled = (os.environ.get("SMS_REJECTIONS_ENABLED") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    test_mode = (os.environ.get("SMS_TEST") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    from_name = (os.environ.get("SMS_RU_FROM") or "").strip() or None
    partner_id = (os.environ.get("SMS_RU_PARTNER_ID") or "").strip() or None
    template = (os.environ.get("SMS_TEMPLATE") or DEFAULT_SMS_TEMPLATE).strip()

    _config = {
        "api_id": api_id,
        "enabled": enabled,
        "test": test_mode,
        "from": from_name,
        "partner_id": partner_id,
        "template": template,
        "timeout": int(os.environ.get("SMS_REQUEST_TIMEOUT", "30")),
    }
    return _config


# =============================================================================
# НОРМАЛИЗАЦИЯ ТЕЛЕФОНА
# =============================================================================


def normalize_phone(raw: str) -> Optional[str]:
    """
    Приводит номер к виду 79XXXXXXXXX (11 цифр).
    Принимает: +7..., 8..., 7..., с пробелами/скобками/дефисами.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None

    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("7"):
        pass
    else:
        return None

    if not re.match(r"^79\d{9}$", digits):
        # Допускаем 7 + любой код (не только мобильные 9xx), но для РФ мобильных чаще 79
        if not re.match(r"^7\d{10}$", digits):
            return None

    return digits


def is_valid_phone(raw: str) -> bool:
    return normalize_phone(raw) is not None


# =============================================================================
# ШАБЛОН
# =============================================================================


def render_sms(template: str, contract_number: str = "") -> str:
    text = (template or DEFAULT_SMS_TEMPLATE).replace(
        "{{contract_number}}", contract_number or ""
    )
    # Убираем лишние пробелы
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


def sms_segment_hint(text: str) -> Tuple[int, str]:
    """Грубая оценка: кириллица → 70/67, латиница → 160/153."""
    if not text:
        return 0, "empty"
    has_cyr = bool(re.search(r"[А-Яа-яЁё]", text))
    if has_cyr:
        if len(text) <= 70:
            return 1, f"cyr {len(text)}/70"
        # длинные: по ~67
        segs = 1 + (len(text) - 1) // 67
        return segs, f"cyr {len(text)} chars ≈ {segs} SMS"
    if len(text) <= 160:
        return 1, f"lat {len(text)}/160"
    segs = 1 + (len(text) - 1) // 153
    return segs, f"lat {len(text)} chars ≈ {segs} SMS"


# =============================================================================
# ЛОГИ / DEDUP / BLACKLIST
# =============================================================================


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_sms_log() -> list:
    entries = _load_json(SMS_LOG_FILE, [])
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
        _save_json(SMS_LOG_FILE, cleaned)
    return cleaned


def log_sms(phone: str, status: str, details: str = "", sms_id: str = "") -> None:
    entries = load_sms_log()
    entries.append(
        {
            "timestamp": datetime.now().isoformat(),
            "phone": phone,
            "status": status,
            "details": details,
            "sms_id": sms_id or "",
        }
    )
    _save_json(SMS_LOG_FILE, entries)


def was_sent_today(phone: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    data = _load_json(SMS_DAILY_SENT_FILE, {})
    return phone in data.get(today, [])


def mark_sent_today(phone: str) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    data = _load_json(SMS_DAILY_SENT_FILE, {})
    if today not in data:
        data[today] = []
    if phone not in data[today]:
        data[today].append(phone)
    # чистим старые дни (>14)
    keys = sorted(data.keys())
    for k in keys[:-14]:
        del data[k]
    _save_json(SMS_DAILY_SENT_FILE, data)


def load_blacklist() -> set:
    return set(_load_json(SMS_BLACKLIST_FILE, []))


def add_to_blacklist(phone: str) -> None:
    bl = load_blacklist()
    if phone not in bl:
        bl.add(phone)
        _save_json(SMS_BLACKLIST_FILE, sorted(bl))
        logger.info(f"🚫 SMS blacklist: {phone}")


# =============================================================================
# HTTP к SMS.ru
# =============================================================================


def _api_request(url: str, params: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    """POST form-urlencoded, ответ JSON."""
    data = urlencode({k: v for k, v in params.items() if v is not None}).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "rnp-help-sms-sender/1.0")

    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        logger.error(f"SMS.ru HTTPError {e.code}: {body[:200]}")
        return {"status": "ERROR", "status_code": e.code, "status_text": body[:300]}
    except URLError as e:
        logger.error(f"SMS.ru URLError: {e}")
        return {"status": "ERROR", "status_code": -1, "status_text": str(e.reason)}
    except Exception as e:
        logger.error(f"SMS.ru request error: {e}")
        return {"status": "ERROR", "status_code": -1, "status_text": str(e)}

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # Иногда plain text
        return {
            "status": "ERROR",
            "status_code": -2,
            "status_text": body[:500],
            "raw": body,
        }


def get_balance() -> Dict[str, Any]:
    """Баланс аккаунта SMS.ru."""
    cfg = _load_config()
    if not cfg["api_id"]:
        return {"ok": False, "error": "SMS_RU_API_ID не задан"}
    data = _api_request(
        API_BALANCE,
        {"api_id": cfg["api_id"], "json": 1},
        timeout=cfg["timeout"],
    )
    if data.get("status") == "OK":
        return {"ok": True, "balance": data.get("balance"), "raw": data}
    return {
        "ok": False,
        "error": data.get("status_text") or str(data.get("status_code")),
        "raw": data,
    }


def get_cost(phone: str, message: str) -> Dict[str, Any]:
    """Стоимость отправки до факта (sms/cost)."""
    cfg = _load_config()
    phone_n = normalize_phone(phone)
    if not phone_n:
        return {"ok": False, "error": "invalid_phone"}
    if not cfg["api_id"]:
        return {"ok": False, "error": "SMS_RU_API_ID не задан"}

    params = {
        "api_id": cfg["api_id"],
        "to": phone_n,
        "msg": message,
        "json": 1,
    }
    data = _api_request(API_COST, params, timeout=cfg["timeout"])
    if data.get("status") == "OK":
        return {
            "ok": True,
            "total_cost": data.get("total_cost"),
            "total_sms": data.get("total_sms"),
            "raw": data,
        }
    return {"ok": False, "error": data.get("status_text"), "raw": data}


# =============================================================================
# ОСНОВНАЯ ОТПРАВКА
# =============================================================================


def send_sms(
    phone: str,
    message: str = None,
    contract_number: str = None,
    delay: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Отправить SMS.

    Returns:
        {
          "success": bool,
          "status": str,       # sent | disabled | test_ok | invalid_phone | ...
          "details": str,
          "sms_id": str|None,
          "phone": str|None,   # нормализованный
          "segments_hint": str,
        }
    """
    result: Dict[str, Any] = {
        "success": False,
        "status": "unknown",
        "details": "",
        "sms_id": None,
        "phone": None,
        "segments_hint": "",
    }

    cfg = _load_config()

    if not cfg["enabled"] and not force:
        result["status"] = "disabled"
        result["details"] = "SMS_REJECTIONS_ENABLED=false"
        logger.info("⏭ SMS отключены (SMS_REJECTIONS_ENABLED)")
        return result

    if not cfg["api_id"]:
        result["status"] = "no_api_id"
        result["details"] = "Задайте SMS_RU_API_ID в .env"
        logger.error("❌ SMS_RU_API_ID не задан")
        return result

    phone_n = normalize_phone(phone)
    if not phone_n:
        result["status"] = "invalid_phone"
        result["details"] = f"Невалидный номер: {phone!r}"
        logger.warning(f"❌ SMS invalid phone: {phone!r}")
        log_sms(str(phone), "invalid_phone", result["details"])
        return result

    result["phone"] = phone_n

    if phone_n in load_blacklist():
        result["status"] = "blacklist"
        result["details"] = "Номер в SMS blacklist"
        log_sms(phone_n, "skipped_blacklist")
        return result

    if was_sent_today(phone_n) and not force:
        result["status"] = "already_sent_today"
        result["details"] = "Уже отправляли SMS на этот номер сегодня"
        log_sms(phone_n, "skipped_duplicate")
        logger.info(f"🔁 SMS уже сегодня: {phone_n}")
        return result

    text = (
        message
        if message is not None
        else render_sms(cfg["template"], contract_number or "")
    )
    segs, hint = sms_segment_hint(text)
    result["segments_hint"] = hint
    if segs > 1:
        logger.warning(f"⚠️ SMS длиннее 1 сегмента ({hint}): «{text[:80]}…»")

    if delay:
        time.sleep(0.3)  # лёгкая пауза, без жёсткого rate limit как у email

    params: Dict[str, Any] = {
        "api_id": cfg["api_id"],
        "to": phone_n,
        "msg": text,
        "json": 1,
    }
    if cfg["from"]:
        params["from"] = cfg["from"]
    if cfg["partner_id"]:
        params["partner_id"] = cfg["partner_id"]
    if cfg["test"]:
        params["test"] = 1

    data = _api_request(API_SEND, params, timeout=cfg["timeout"])

    # Успех запроса
    if data.get("status") != "OK":
        err = data.get("status_text") or f"code={data.get('status_code')}"
        result["status"] = "api_error"
        result["details"] = str(err)
        log_sms(phone_n, "api_error", str(err))
        logger.error(f"❌ SMS.ru API: {err}")
        return result

    # Статус по конкретному номеру
    sms_block = (data.get("sms") or {}).get(phone_n) or {}
    phone_status = sms_block.get("status")
    phone_code = sms_block.get("status_code")
    sms_id = sms_block.get("sms_id")
    phone_text = sms_block.get("status_text") or ""

    if phone_status == "OK" or phone_code == 100:
        result["success"] = True
        result["sms_id"] = sms_id
        if cfg["test"]:
            result["status"] = "test_ok"
            result["details"] = f"TEST OK (без списания), sms_id={sms_id}"
            log_sms(phone_n, "test_ok", result["details"], sms_id or "")
            logger.info(f"✅ SMS TEST → {phone_n} | {hint}")
        else:
            result["status"] = "sent"
            result["details"] = f"Отправлено, sms_id={sms_id}"
            log_sms(phone_n, "sent", result["details"], sms_id or "")
            mark_sent_today(phone_n)
            logger.info(f"✅ SMS → {phone_n} id={sms_id} | {hint}")
        return result

    # Ошибки на номер
    code = phone_code or 0
    # 207 — нельзя слать на номер / >100 номеров
    if code in (202, 207, 220):
        add_to_blacklist(phone_n)
        result["status"] = "rejected_number"
    else:
        result["status"] = f"error_{code}" if code else "error"
    result["details"] = phone_text or str(sms_block)
    log_sms(phone_n, result["status"], result["details"])
    logger.error(f"❌ SMS {phone_n}: {result['status']} {result['details']}")
    return result


def get_sms_stats() -> Dict[str, Any]:
    entries = load_sms_log()
    today = datetime.now().strftime("%Y-%m-%d")
    sent_today = sum(
        1
        for e in entries
        if e.get("timestamp", "").startswith(today)
        and e.get("status") in ("sent", "test_ok")
    )
    return {
        "total_log": len(entries),
        "sent_today": sent_today,
        "blacklist_size": len(load_blacklist()),
        "enabled": _load_config()["enabled"],
        "test_mode": _load_config()["test"],
    }


# =============================================================================
# ФАБРИКА (как get_email_sender)
# =============================================================================

_sms_sender_instance = None


def get_sms_sender():
    global _sms_sender_instance
    if _sms_sender_instance is None:
        _sms_sender_instance = _SmsSenderWrapper()
    return _sms_sender_instance


class _SmsSenderWrapper:
    """Обёртка для processing.py / очереди."""

    def send(
        self,
        phone: str,
        contract_number: str = "",
        message: str = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        r = send_sms(
            phone=phone,
            message=message,
            contract_number=contract_number,
            delay=True,
            force=force,
        )
        return {
            "success": r.get("success", False),
            "status": r.get("status", "unknown"),
            "details": r.get("details", ""),
            "sms_id": r.get("sms_id"),
            "phone": r.get("phone"),
        }

    def balance(self) -> Dict[str, Any]:
        return get_balance()

    def cost(self, phone: str, contract_number: str = "") -> Dict[str, Any]:
        cfg = _load_config()
        text = render_sms(cfg["template"], contract_number)
        return get_cost(phone, text)

    def stats(self) -> Dict[str, Any]:
        return get_sms_stats()


# =============================================================================
# CLI-тест
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
    )
    import sys

    cfg = _load_config()
    print(
        "enabled:",
        cfg["enabled"],
        "test:",
        cfg["test"],
        "api_id set:",
        bool(cfg["api_id"]),
    )

    if len(sys.argv) < 2:
        print("Usage: python sms_sender.py <phone> [contract_number]")
        print("Example: python sms_sender.py 79282520101 1234567890123456789")
        bal = get_balance()
        print("balance:", bal)
        sys.exit(0)

    phone = sys.argv[1]
    contract = sys.argv[2] if len(sys.argv) > 2 else "TEST-CONTRACT"
    # CLI всегда force + можно с SMS_TEST=1
    r = send_sms(phone, contract_number=contract, force=True)
    print(json.dumps(r, ensure_ascii=False, indent=2))

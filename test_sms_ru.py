#!/usr/bin/env python3
"""
test_sms_ru.py — быстрый тест SMS.ru на 10 ₽ баланса.

1) balance
2) cost (сколько стоило бы)
3) send с test=1 (БЕЗ списания)
4) опционально: реальная отправка (спишет ~8–9 ₽)

Использование на сервере:

  export SMS_RU_API_ID='ваш_ключ'
  # или прописать в .env

  python test_sms_ru.py                  # только balance + test на номер по умолчанию
  python test_sms_ru.py 79282520101      # test=1 на ваш номер
  python test_sms_ru.py 79282520101 --real   # РЕАЛЬНАЯ SMS (деньги!)

Зависимости: только стандартная библиотека Python 3.
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_BALANCE = "https://sms.ru/my/balance"
API_COST = "https://sms.ru/sms/cost"
API_SEND = "https://sms.ru/sms/send"

# Подставьте свой номер для теста, если не передаёте аргументом
DEFAULT_PHONE = "79681059472"
TEST_MSG = "Тест SMS от rnp-help. Отказ-парсер. Арина +79282520101"


def load_api_id() -> str:
    api_id = (os.environ.get("SMS_RU_API_ID") or "").strip()
    if api_id:
        return api_id
    # .env рядом
    for path in (".env", "../.env", "/opt/zakupki-rejections/.env"):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SMS_RU_API_ID="):
                        val = line.split("=", 1)[1].strip().strip("'\"")
                        if val:
                            return val
        except OSError:
            pass
    return ""


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    if re.match(r"^7\d{10}$", digits or ""):
        return digits
    return None


def api_post(url: str, params: dict, timeout: int = 30) -> dict:
    data = urlencode({k: v for k, v in params.items() if v is not None}).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "rnp-help-sms-test/1.0")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        return {"status": "ERROR", "status_code": e.code, "status_text": body[:400]}
    except URLError as e:
        return {"status": "ERROR", "status_code": -1, "status_text": str(e.reason)}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"status": "ERROR", "status_code": -2, "status_text": body[:400]}


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    real = "--real" in args
    args = [a for a in args if a != "--real"]
    phone_raw = args[0] if args else DEFAULT_PHONE

    api_id = load_api_id()
    if not api_id:
        print("❌ Не найден SMS_RU_API_ID")
        print("   export SMS_RU_API_ID='...'  или добавьте в .env")
        return 1

    phone = normalize_phone(phone_raw)
    if not phone:
        print(f"❌ Невалидный номер: {phone_raw!r}")
        return 1

    print("=" * 50)
    print("SMS.ru test")
    print(f"  api_id: {api_id[:8]}…{api_id[-4:] if len(api_id) > 12 else ''}")
    print(f"  phone:  {phone}")
    print(f"  mode:   {'REAL (деньги!)' if real else 'TEST (test=1, без списания)'}")
    print("=" * 50)

    # 1) Баланс
    print("\n[1] Баланс…")
    bal = api_post(API_BALANCE, {"api_id": api_id, "json": 1})
    print(json.dumps(bal, ensure_ascii=False, indent=2))
    if bal.get("status") != "OK":
        print("❌ Баланс не получен — проверьте api_id")
        return 1
    balance = float(bal.get("balance") or 0)
    print(f"→ Баланс: {balance} ₽")

    # 2) Стоимость
    print("\n[2] Стоимость (cost)…")
    cost = api_post(
        API_COST,
        {"api_id": api_id, "to": phone, "msg": TEST_MSG, "json": 1},
    )
    print(json.dumps(cost, ensure_ascii=False, indent=2))
    if cost.get("status") == "OK":
        print(
            f"→ total_cost={cost.get('total_cost')}  total_sms={cost.get('total_sms')}"
        )

    # 3) Отправка
    params = {
        "api_id": api_id,
        "to": phone,
        "msg": TEST_MSG,
        "json": 1,
    }
    if not real:
        params["test"] = 1
        print("\n[3] Отправка TEST (test=1)…")
    else:
        if balance < 5:
            print(f"\n❌ Баланс слишком мал для REAL: {balance} ₽")
            return 1
        print("\n[3] Отправка REAL (будет списание)…")
        confirm = input("Напишите YES чтобы отправить: ").strip()
        if confirm != "YES":
            print("Отменено.")
            return 0

    send = api_post(API_SEND, params)
    print(json.dumps(send, ensure_ascii=False, indent=2))

    if send.get("status") == "OK":
        sms = (send.get("sms") or {}).get(phone) or {}
        if sms.get("status") == "OK" or sms.get("status_code") == 100:
            print(f"\n✅ Успех | sms_id={sms.get('sms_id')} | test={not real}")
            if not real:
                print("   Баланс НЕ списан (test=1).")
            else:
                bal2 = api_post(API_BALANCE, {"api_id": api_id, "json": 1})
                print(f"   Баланс после: {bal2.get('balance')} ₽")
            return 0
        print(f"\n⚠️ Запрос OK, но номер: {sms}")
        return 2

    print("\n❌ Ошибка API")
    return 1


if __name__ == "__main__":
    sys.exit(main())

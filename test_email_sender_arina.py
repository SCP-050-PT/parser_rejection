"""
test_email_sender_arina.py — тестовый скрипт v5.0
Свой SMTP (127.0.0.1:25, без SSL/auth)
1 шаблон на 1 адрес (4 письма)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import time
import random
import smtplib
from datetime import datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from loguru import logger

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

# 4 адреса, 4 письма (по одному шаблону на адрес)
TEST_RECIPIENTS = [
    {
        "email": "test-74stdpz4p@srv1.mail-tester.com",
        "contract": "2772401520526000585",
        "supplier": "ООО СтройМонтаж",
        "template": 0,
    },
    {
        "email": "kostyan.babushkin@list.ru",
        "contract": "2525801616326000083",
        "supplier": "ИП Иванов Иван Иванович",
        "template": 1,
    },
    {
        "email": "babushkin20022002@gmail.com",
        "contract": "2544845215026000003",
        "supplier": "ООО ТехноПоставка",
        "template": 2,
    },
    {
        "email": "Ant1dote7@yandex.ru",
        "contract": "2010700762626000102",
        "supplier": "АО РегионСтрой",
        "template": 0,
    },
]

# SMTP — свой сервер (Postfix на localhost)
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 25
SMTP_LOGIN = None  # Без авторизации на localhost
SMTP_PASSWORD = None  # Без авторизации

FROM_NAME = "Arina Eremyan"
FROM_EMAIL = "lawyer@rnp-help.ru"
REPLY_TO = "lawyer@rnp-help.ru"
DOMAIN = "rnp-help.ru"

logger.info(f"📡 SMTP: {SMTP_HOST}:{SMTP_PORT} (локальный Postfix)")
logger.info(f"📤 From: {FROM_NAME} <{FROM_EMAIL}>")

# =============================================================================
# ШАБЛОНЫ (русский текст, темы на латинице)
# =============================================================================

EMAIL_SUBJECTS = [
    "Vopros po dogovoru No.{{contract_number}}",
    "Informacija o rastorzhenii dogovora No.{{contract_number}}",
    "Konsultacija po zashchite ot RNP, dogovor No.{{contract_number}}",
]

EMAIL_TEMPLATES = [
    # --- Шаблон 0: Мягкий, деловой ---
    """Здравствуйте, {{supplier_name}}!

Меня зовут Арина Еремян, я юрист в сфере госзакупок. Увидела информацию о вашем контракте № {{contract_number}} на портале zakupki.gov.ru.

Если у вас возникли вопросы по процедуре или нужна помощь в подготовке документов — буду рада помочь. Первичная консультация бесплатная, без обязательств.

Связаться со мной:
• Телефон: +7 (928) 252-01-01
• Telegram: @arina_eaa
• Email: lawyer@rnp-help.ru

С уважением,
Еремян Арина Арнольдовна
Юрист в сфере госзакупок""",
    # --- Шаблон 1: С акцентом на сроки ---
    """Здравствуйте, {{supplier_name}}!

Пишу вам по поводу контракта № {{contract_number}}, размещённого на zakupki.gov.ru.

Специализируюсь на сопровождении поставщиков в спорах по 44-ФЗ и 223-ФЗ. Помогаю с подготовкой возражений, представлением интересов в ФАС и защитой от включения в реестр.

Условия работы:
• Первый этап: 20 000 руб. (подготовка документов, анализ)
• Второй этап: 30 000 руб. (только после положительного результата)

Сроки на подачу документов ограничены. Если интересно — ответьте на это письмо или позвоните: +7 (928) 252-01-01.

С уважением,
Еремян Арина Арнольдовна
Юрист в сфере госзакупок
Telegram: @arina_eaa""",
    # --- Шаблон 2: Короткий ---
    """Здравствуйте, {{supplier_name}}!

По контракту № {{contract_number}} у вас есть возможность обжаловать решение заказчика. Сроки ограничены.

Я — Арина Еремян, юрист по госзакупкам. Помогаю поставщикам защитить свои интересы.

Бесплатная консультация: +7 (928) 252-01-01 или @arina_eaa в Telegram.

lawyer@rnp-help.ru""",
]


def render(tpl: str, contract: str = "", supplier: str = "") -> str:
    return tpl.replace("{{contract_number}}", contract).replace(
        "{{supplier_name}}", supplier or "Уважаемый поставщик"
    )


# =============================================================================
# ОТПРАВКА ЧЕРЕЗ ЛОКАЛЬНЫЙ POSTFIX (127.0.0.1:25)
# =============================================================================


def send_email(
    to_email: str,
    subject: str,
    body: str,
    contract: str,
    tidx: int,
    supplier: str = "",
) -> dict:
    logger.info(f"📧 [REAL] -> {to_email} | contract {contract} | template #{tidx+1}")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email.strip().lower()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=DOMAIN)
    msg["Reply-To"] = REPLY_TO

    try:
        # Локальный Postfix: без SSL, без авторизации
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as srv:
            # Без srv.login() — localhost не требует auth
            srv.sendmail(FROM_EMAIL, [to_email], msg.as_string())

        logger.info(f"✅ OK: {to_email}")
        return {
            "success": True,
            "status": "sent",
            "template_index": tidx,
            "from": msg["From"],
            "message_id": msg["Message-ID"],
        }
    except Exception as e:
        logger.error(f"❌ FAIL: {to_email} — {e}")
        return {
            "success": False,
            "status": "error",
            "details": str(e),
            "template_index": tidx,
        }


def get_delay(email: str) -> float:
    dom = email.split("@")[1].lower()
    big = {
        "mail.ru",
        "list.ru",
        "gmail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "rambler.ru",
        "yandex.ru",
    }
    return (
        random.uniform(10, 20)
        if any(dom.endswith(d) for d in big)
        else random.uniform(15, 25)
    )


# =============================================================================
# MAIN
# =============================================================================


def main():
    logger.info("=" * 60)
    logger.info("🧪 TEST v5.0 — ЛОКАЛЬНЫЙ POSTFIX (127.0.0.1:25)")
    logger.info("=" * 60)

    results = []
    for i, rec in enumerate(TEST_RECIPIENTS):
        tidx = rec["template"]
        subject = render(EMAIL_SUBJECTS[tidx], rec["contract"])
        body = render(EMAIL_TEMPLATES[tidx], rec["contract"], rec["supplier"])

        logger.info(f"--- {i+1}/{len(TEST_RECIPIENTS)}: {rec['email']} ---")
        logger.info(f"   Шаблон: #{tidx+1} | Контракт: {rec['contract']}")
        logger.info(f"   Поставщик: {rec['supplier']}")

        delay = get_delay(rec["email"])
        logger.info(f"⏳ Задержка {delay:.1f}с...")
        time.sleep(delay)

        result = send_email(
            to_email=rec["email"],
            subject=subject,
            body=body,
            contract=rec["contract"],
            tidx=tidx,
            supplier=rec["supplier"],
        )

        results.append({**rec, **result})

        if i < len(TEST_RECIPIENTS) - 1:
            pause = random.uniform(10, 20)
            logger.info(f"⏳ Пауза {pause:.1f}с...")
            time.sleep(pause)

    # Сохраняем результаты
    Path("data").mkdir(exist_ok=True)
    with open("data/test_send_results_v5.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info("📊 RESULTS:")
    sent = sum(1 for r in results if r.get("success"))
    failed = sum(1 for r in results if not r.get("success"))
    logger.info(f"  Total: {len(TEST_RECIPIENTS)} | Sent: {sent} | Failed: {failed}")
    for r in results:
        status = "✅" if r.get("success") else "❌"
        logger.info(f"  {status} {r['email']} | template #{r['template']+1}")
    logger.info("=" * 60)

    # Показываем отправленные письма
    logger.info("")
    logger.info("📧 ОТПРАВЛЕННЫЕ ПИСЬМА:")
    for r in results:
        if r.get("success"):
            logger.info(f"\n--- {r['email']} ---")
            logger.info(f"Тема: {render(EMAIL_SUBJECTS[r['template']], r['contract'])}")
            logger.info("-" * 40)
            text = render(EMAIL_TEMPLATES[r["template"]], r["contract"], r["supplier"])
            logger.info(text[:500] + "..." if len(text) > 500 else text)


if __name__ == "__main__":
    main()

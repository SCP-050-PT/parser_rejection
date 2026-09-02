"""
test_email_sender_fasexpert_v3.py — тестовый скрипт v3.0
Условный List-Unsubscribe: есть для Gmail/Mail.ru, НЕТ для Яндекса
Проверка: попадёт ли Яндекс во Входящие без List-Unsubscribe?
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import time
import random
import smtplib
import ssl
from datetime import datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formatdate, make_msgid

from loguru import logger

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================

TEST_RECIPIENTS = [
    {
        "email": "kostyan.babushkin@list.ru",
        "contract": "2772401520526000585",
        "supplier": "ООО СтройМонтаж",
        "org": "ГБУ «Московский метрополитен»",
        "template": 0,
        "list_unsub": True,  # Mail.ru — оставляем
    },
    {
        "email": "babushkin20022002@gmail.com",
        "contract": "2525801616326000083",
        "supplier": "ИП Иванов Иван Иванович",
        "org": "АО «РЖД»",
        "template": 1,
        "list_unsub": True,  # Gmail — оставляем
    },
    {
        "email": "Ant1dote7@yandex.ru",
        "contract": "2544845215026000003",
        "supplier": "ООО ТехноПоставка",
        "org": "Минобороны России",
        "template": 2,
        "list_unsub": False,  # Яндекс — убираем (ТЕСТ!)
    },
    {
        "email": "sout@acesafety.ru",
        "contract": "2010700762626000102",
        "supplier": "АО РегионСтрой",
        "org": "ФАС России",
        "template": 0,
        "list_unsub": True,  # Корп. — оставляем
    },
]

SMTP_HOST = "smtp.timeweb.ru"
SMTP_PORT = 465

FROM_NAME = "Елизавета Борисова"
FROM_EMAIL = "contract@fasexpert24.ru"
REPLY_TO = "contract@fasexpert24.ru"
BOUNCE_EMAIL = "bounce@fasexpert24.ru"
DOMAIN = "fasexpert24.ru"

logger.info(f"📡 SMTP: {SMTP_HOST}:{SMTP_PORT}")
logger.info(f"📤 From: {FROM_NAME} <{FROM_EMAIL}>")

# =============================================================================
# ЗАГРУЗКА .env
# =============================================================================


def load_env():
    try:
        from dotenv import load_dotenv

        for path in [
            ".env",
            "../.env",
            "../../.env",
            "/opt/zakupki-rejections-new/.env",
        ]:
            if os.path.exists(path):
                load_dotenv(path)
                logger.info(f"✅ .env загружен: {path}")
                return
        logger.warning("⚠️ .env не найден")
    except ImportError:
        logger.warning("⚠️ python-dotenv не установлен")


load_env()

SMTP_LOGIN = os.environ.get("SMTP_LOGIN", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

if not SMTP_LOGIN or not SMTP_PASSWORD:
    logger.error("❌ Не заданы SMTP_LOGIN или SMTP_PASSWORD в .env")
    sys.exit(1)

# =============================================================================
# ШАБЛОНЫ (все со ссылкой на практику и телефоном)
# =============================================================================

EMAIL_SUBJECTS = [
    "Вопрос по контракту № {{contract_number}}",
    "Контракт № {{contract_number}} — вопрос по процедуре",
    "По контракту № {{contract_number}}",
]

EMAIL_TEMPLATES = [
    # --- Шаблон A ---
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
— Предоплата: 30 000 руб. (анализ, подготовка документов, участие в заседании)
— Окончательный расчет: 20 000 руб. (после вынесения решения)

Срок подачи возражений — 10 календарных дней с момента публикации в ЕИС.
Пропуск срока существенно усложняет защиту.

Примеры успешных дел: https://disk.yandex.ru/d/3XCOPOCARw72Ww

Свяжитесь со мной: +7 (927) 188-31-82

С уважением,
Елизавета Борисова
ФасЭксперт 24
https://fasexpert24.ru""",
    # --- Шаблон B ---
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
— 30 000 руб. — начало работы (анализ, документы, заседание)
— 20 000 руб. — после получения положительного решения

Срок ограничен: 10 дней с момента публикации в ЕИС.

Примеры успешных дел: https://disk.yandex.ru/d/3XCOPOCARw72Ww

Свяжитесь со мной: +7 (927) 188-31-82

С уважением,
Елизавета Борисова
ФасЭксперт 24
https://fasexpert24.ru""",
    # --- Шаблон C ---
    """Здравствуйте, {{supplier_name}}.

По контракту № {{contract_number}} с {{org_name}} у вас есть риск включения
в реестр недобросовестных поставщиков. Срок на защиту — 10 дней.

Я — Елизавета Борисова, юрист ФасЭксперт 24. Помогаю поставщикам оспаривать
одностороннее расторжение и защищать репутацию.

Условия:
— 30 000 руб. — анализ и подготовка документов
— 20 000 руб. — после положительного решения

Первое знакомство с делом — без оплаты.

Примеры успешных дел: https://disk.yandex.ru/d/3XCOPOCARw72Ww

Свяжитесь со мной: +7 (927) 188-31-82

https://fasexpert24.ru""",
]


def render(tpl: str, contract: str = "", supplier: str = "", org: str = "") -> str:
    return (
        tpl.replace("{{contract_number}}", contract)
        .replace("{{supplier_name}}", supplier or "Уважаемый поставщик")
        .replace("{{org_name}}", org or "Заказчик")
    )


# =============================================================================
# СОЗДАНИЕ ПИСЬМА С УСЛОВНЫМ List-Unsubscribe
# =============================================================================


def create_email_with_headers(
    to_email: str, subject: str, body: str, use_list_unsub: bool = True
) -> tuple:
    """Создаёт письмо. List-Unsubscribe только если use_list_unsub=True."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8").encode()
    msg["From"] = f"{Header(FROM_NAME, 'utf-8').encode()} <{FROM_EMAIL}>"
    msg["To"] = to_email.strip().lower()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=DOMAIN)
    msg["Reply-To"] = REPLY_TO

    if use_list_unsub:
        msg["List-Unsubscribe"] = f"<mailto:{BOUNCE_EMAIL}?subject=unsubscribe>"
        logger.info("   📎 List-Unsubscribe: ДА")
    else:
        logger.info("   📎 List-Unsubscribe: НЕТ (тест для Яндекса)")

    headers_text = ""
    for key, value in msg.items():
        headers_text += f"{key}: {value}\n"

    return msg, headers_text


# =============================================================================
# ОТПРАВКА
# =============================================================================


def send_email(
    to_email: str,
    subject: str,
    body: str,
    contract: str,
    tidx: int,
    supplier: str = "",
    org: str = "",
    use_list_unsub: bool = True,
) -> dict:
    logger.info(f"📧 [REAL] -> {to_email} | contract {contract} | template #{tidx+1}")

    msg, headers_text = create_email_with_headers(
        to_email, subject, body, use_list_unsub
    )

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as srv:
            srv.login(SMTP_LOGIN, SMTP_PASSWORD)
            srv.sendmail(FROM_EMAIL, [to_email], msg.as_string())

        logger.info(f"✅ OK: {to_email}")
        return {
            "success": True,
            "status": "sent",
            "template_index": tidx,
            "from": msg["From"],
            "message_id": msg["Message-ID"],
            "headers": headers_text,
            "list_unsubscribe": use_list_unsub,
        }
    except Exception as e:
        logger.error(f"❌ FAIL: {to_email} — {e}")
        return {
            "success": False,
            "status": "error",
            "details": str(e),
            "template_index": tidx,
            "headers": headers_text,
            "list_unsubscribe": use_list_unsub,
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
    logger.info("=" * 70)
    logger.info("🧪 TEST v3.0 — Условный List-Unsubscribe")
    logger.info("   Яндекс: БЕЗ List-Unsubscribe (проверим Входящие)")
    logger.info("   Остальные: С List-Unsubscribe")
    logger.info("=" * 70)
    logger.info(f"📡 SMTP: {SMTP_HOST}:{SMTP_PORT}")
    logger.info(f"📤 From: {FROM_NAME} <{FROM_EMAIL}>")
    logger.info("")

    results = []
    all_headers = []

    for i, rec in enumerate(TEST_RECIPIENTS):
        tidx = rec["template"]
        use_unsub = rec.get("list_unsub", True)
        subject = render(EMAIL_SUBJECTS[tidx], rec["contract"])
        body = render(
            EMAIL_TEMPLATES[tidx], rec["contract"], rec["supplier"], rec["org"]
        )

        logger.info(f"--- {i+1}/{len(TEST_RECIPIENTS)}: {rec['email']} ---")
        logger.info(f"   Шаблон: #{tidx+1} ({['A','B','C'][tidx]})")
        logger.info(f"   Контракт: {rec['contract']}")
        logger.info(f"   Поставщик: {rec['supplier']}")

        _, headers_preview = create_email_with_headers(
            rec["email"], subject, body, use_unsub
        )
        logger.info("📋 Заголовки письма:")
        for line in headers_preview.strip().split("\n"):
            logger.info(f"   {line}")

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
            org=rec["org"],
            use_list_unsub=use_unsub,
        )

        results.append({**rec, **result})

        all_headers.append(
            {
                "email": rec["email"],
                "template": tidx,
                "template_label": ["A", "B", "C"][tidx],
                "list_unsubscribe": use_unsub,
                "headers": result.get("headers", ""),
                "success": result.get("success", False),
                "timestamp": datetime.now().isoformat(),
            }
        )

        if i < len(TEST_RECIPIENTS) - 1:
            pause = random.uniform(10, 20)
            logger.info(f"⏳ Пауза {pause:.1f}с...")
            time.sleep(pause)

        logger.info("")

    Path("data").mkdir(exist_ok=True)

    with open("data/test_fasexpert_v3_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    with open("data/test_fasexpert_v3_headers.json", "w", encoding="utf-8") as f:
        json.dump(all_headers, f, ensure_ascii=False, indent=2)

    logger.info("=" * 70)
    logger.info("📊 RESULTS:")
    sent = sum(1 for r in results if r.get("success"))
    failed = sum(1 for r in results if not r.get("success"))
    logger.info(f"  Total: {len(TEST_RECIPIENTS)} | Sent: {sent} | Failed: {failed}")
    for r in results:
        status = "✅" if r.get("success") else "❌"
        tmpl = ["A", "B", "C"][r["template"]]
        unsub = "+LU" if r.get("list_unsubscribe") else "-LU"
        logger.info(f"  {status} {r['email']} | {tmpl} | {unsub}")
    logger.info("=" * 70)

    logger.info("")
    logger.info("📧 ОТПРАВЛЕННЫЕ ПИСЬМА:")
    for r in results:
        if r.get("success"):
            tmpl_label = ["A", "B", "C"][r["template"]]
            unsub = "+LU" if r.get("list_unsubscribe") else "-LU"
            logger.info(f"\n--- {r['email']} ({tmpl_label}, {unsub}) ---")
            logger.info(f"Тема: {render(EMAIL_SUBJECTS[r['template']], r['contract'])}")
            logger.info("-" * 40)
            text = render(
                EMAIL_TEMPLATES[r["template"]], r["contract"], r["supplier"], r["org"]
            )
            logger.info(text[:500] + "..." if len(text) > 500 else text)

    logger.info("")
    logger.info("📁 Файлы сохранены:")
    logger.info("   data/test_fasexpert_v3_results.json")
    logger.info("   data/test_fasexpert_v3_headers.json")


if __name__ == "__main__":
    main()

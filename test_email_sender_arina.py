"""
test_email_sender_arina.py — универсальный тестовый скрипт v6.1
Поддерживает переключение каналов через ACTIVE_CHANNEL в .env
ИСПРАВЛЕНО: Убрана жёсткая привязка к POSTFIX_*, добавлены fallback'и
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
from email.utils import formatdate, make_msgid, formataddr
from dotenv import load_dotenv

from loguru import logger

# Загружаем переменные окружения
for path in [".env", "../.env", "/opt/zakupki-rejections/.env"]:
    if os.path.exists(path):
        load_dotenv(path)
        break

# =============================================================================
# АВТОМАТИЧЕСКАЯ ЗАГРУЗКА КОНФИГА ПО АКТИВНОМУ КАНАЛУ
# =============================================================================


def get_channel_config():
    """Возвращает конфиг в зависимости от ACTIVE_CHANNEL"""
    channel = os.environ.get("ACTIVE_CHANNEL", "smtp").lower().strip()

    # ✅ ИСПРАВЛЕНО: Убрана жёсткая привязка к префиксам, используем SMTP_* как базовые
    if channel == "timeweb":
        # Для Timeweb используем отдельные переменные если есть, иначе SMTP_*
        host = os.environ.get("TIMEWEB_HOST", os.environ.get("SMTP_HOST", "127.0.0.1"))
        port = int(os.environ.get("TIMEWEB_PORT", os.environ.get("SMTP_PORT", "25")))
        user = os.environ.get("TIMEWEB_LOGIN", os.environ.get("SMTP_LOGIN", ""))
        password = os.environ.get(
            "TIMEWEB_PASSWORD", os.environ.get("SMTP_PASSWORD", "")
        )
        from_email = os.environ.get(
            "TIMEWEB_FROM_EMAIL",
            os.environ.get("FROM_EMAIL", "tender-info@rnp-help.ru"),
        )
        domain = os.environ.get(
            "TIMEWEB_DOMAIN", os.environ.get("DOMAIN", "rnp-help.ru")
        )
        logger.info(f" Тестовый режим: TIMEWEB ({domain})")
    else:
        # По умолчанию используем SMTP_* переменные
        host = os.environ.get("SMTP_HOST", "127.0.0.1")
        port = int(os.environ.get("SMTP_PORT", "25"))
        user = os.environ.get("SMTP_LOGIN", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        from_email = os.environ.get("FROM_EMAIL", "tender-info@rnp-help.ru")
        domain = os.environ.get("DOMAIN", "rnp-help.ru")
        logger.info(f" Тестовый режим: SMTP ({domain})")

    return {
        "CHANNEL": channel,
        "HOST": host,
        "PORT": port,
        "USER": user,
        "PASS": password,
        "FROM_NAME": os.environ.get("FROM_NAME", "Арина Еремян"),
        "FROM_EMAIL": from_email,
        "BOUNCE_EMAIL": os.environ.get("BOUNCE_EMAIL", from_email),
        "REPLY_TO": os.environ.get("REPLY_TO", from_email),
        "DOMAIN": domain,
    }


CFG = get_channel_config()

# =============================================================================
# ТЕСТОВЫЕ ПОЛУЧАТЕЛИ
# =============================================================================

TEST_RECIPIENTS = [
    {
        "email": "babushkin.kostyan@inbox.ru",
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

logger.info(f" SMTP: {CFG['HOST']}:{CFG['PORT']}")
logger.info(f" From: {CFG['FROM_NAME']} <{CFG['FROM_EMAIL']}>")
logger.info(f" Bounce: {CFG['BOUNCE_EMAIL']}")

# =============================================================================
# ШАБЛОНЫ ПИСЕМ
# =============================================================================

EMAIL_SUBJECTS = [
    "Информация по контракту №{{contract_number}}",
    "Документы по договору №{{contract_number}}",
    "Уведомление по контракту №{{contract_number}}",
]

EMAIL_TEMPLATES = [
    """Здравствуйте!

По вашему контракту №{{contract_number}} на портале госзакупок размещена информация, требующая внимания. Сроки для реагирования ограничены, поэтому пишу вам напрямую.

Меня зовут Арина Еремян, я юрист в сфере госзакупок. Более 5 лет помогаю поставщикам обжаловать решения о расторжении и избегать попадания в РНП.

Что предлагаю прямо сейчас:
• Бесплатная первичная консультация - оценю вашу ситуацию и перспективы.
• Индивидуальный план действий с указанием сроков.
• Полное сопровождение: от подготовки документов до взаимодействия с ФАС.
• Работа до результата, без передачи дела другим специалистам.

Если сейчас не готовы обсуждать детали - напишите «позже». Но не откладывайте: пропуск срока без уважительных причин делает обжалование практически невозможным.

Для консультации просто ответьте на это письмо или свяжитесь со мной.

С уважением,
Еремян Арина Арнольдовна
Юрист в сфере госзакупок
Мобильный телефон: +7 (928) 252-01-01
Telegram: @arina_eaa""",
    """По контракту №{{contract_number}} на портале госзакупок размещена информация, требующая внимания. Сроки для реагирования ограничены, поэтому связываюсь с вами напрямую.

Меня зовут Арина Еремян, я юрист по госзакупкам. Более 5 лет занимаюсь защитой поставщиков при расторжении контрактов и предотвращением включения в РНП.

Могу помочь на всех этапах:
• бесплатно оценю вашу ситуацию и перспективы;
• разработаю индивидуальный план действий;
• подготовлю необходимые документы;
• представлю ваши интересы при взаимодействии с ФАС;
• лично буду сопровождать дело до результата.

Если пока не готовы обсуждать детали — ответьте «позже». Но не затягивайте с решением: пропуск срока без уважительных причин может значительно осложнить обжалование.

С уважением,
Еремян Арина Арнольдовна
Юрист в сфере госзакупок
Мобильный телефон: +7 (928) 252-01-01
Telegram: @arina_eaa""",
    """Здравствуйте!

На официальном сайте портале госзакупок опубликовано решение об одностороннем отказе от исполнения контракта №{{contract_number}}.

Меня зовут Арина Еремян, я юрист в сфере госзакупок. Специализируюсь на защите поставщиков от попадания в реестр недобросовестных поставщиков и сопровождении споров по 44-ФЗ и 223-ФЗ более 5 лет.

Чем могу помочь:
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
]


def render(tpl: str, contract: str = "", supplier: str = "") -> str:
    return tpl.replace("{{contract_number}}", contract).replace(
        "{{supplier_name}}", supplier or "Уважаемый поставщик"
    )


# =============================================================================
# ОТПРАВКА ПИСЬМА
# =============================================================================


def send_email(
    to_email: str, subject: str, body: str, contract: str, tidx: int, supplier: str = ""
) -> dict:
    logger.info(
        f" [{CFG['CHANNEL'].upper()}] -> {to_email} | contract {contract} | template #{tidx+1}"
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((CFG["FROM_NAME"], CFG["FROM_EMAIL"]))
    msg["To"] = to_email.strip().lower()
    msg["Date"] = formatdate(localtime=True)

    # Message-ID соответствует домену
    msg["Message-ID"] = make_msgid(domain=CFG["DOMAIN"])
    msg["Reply-To"] = CFG["REPLY_TO"]

    # List-Unsubscribe динамический под текущий домен
    msg["List-Unsubscribe"] = f"<mailto:unsubscribe@{CFG['DOMAIN']}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["Feedback-ID"] = f"{CFG['DOMAIN']}:contract:{contract}"
    msg["X-Mailer"] = f"RNP-Help Test Mailer v6.1 ({CFG['CHANNEL']})"

    try:
        # Локальный Postfix без SSL
        if CFG["HOST"] in ("127.0.0.1", "localhost"):
            with smtplib.SMTP(CFG["HOST"], CFG["PORT"], timeout=30) as srv:
                srv.sendmail(CFG["BOUNCE_EMAIL"], [to_email], msg.as_string())
        else:
            # Внешний SMTP с SSL
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                CFG["HOST"], CFG["PORT"], context=context, timeout=30
            ) as srv:
                if CFG["USER"] and CFG["PASS"]:
                    srv.login(CFG["USER"], CFG["PASS"])
                srv.sendmail(CFG["FROM_EMAIL"], [to_email], msg.as_string())

        logger.info(f" OK: {to_email}")
        return {
            "success": True,
            "status": "sent",
            "template_index": tidx,
            "message_id": msg["Message-ID"],
        }

    except Exception as e:
        logger.error(f" FAIL: {to_email} — {e}")
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
    logger.info(f" TEST v6.1 — КАНАЛ: {CFG['CHANNEL'].upper()}")
    logger.info("=" * 60)

    results = []
    for i, rec in enumerate(TEST_RECIPIENTS):
        tidx = rec["template"]
        subject = render(EMAIL_SUBJECTS[tidx], rec["contract"])
        body = render(EMAIL_TEMPLATES[tidx], rec["contract"], rec["supplier"])

        logger.info(f"--- {i+1}/{len(TEST_RECIPIENTS)}: {rec['email']} ---")
        logger.info(f"   Шаблон: #{tidx+1} | Контракт: {rec['contract']}")

        delay = get_delay(rec["email"])
        logger.info(f" Задержка {delay:.1f}с...")
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
            logger.info(f" Пауза {pause:.1f}с...")
            time.sleep(pause)

    Path("data").mkdir(exist_ok=True)
    with open("data/test_send_results_v61.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    sent = sum(1 for r in results if r.get("success"))
    failed = sum(1 for r in results if not r.get("success"))
    logger.info(f"  Total: {len(TEST_RECIPIENTS)} | Sent: {sent} | Failed: {failed}")
    for r in results:
        status = "✅" if r.get("success") else "❌"
        logger.info(f"  {status} {r['email']} | template #{r['template']+1}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()

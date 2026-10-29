from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field
import json
from typing import List, Dict, Any, Optional

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
USERS_DIR = DATA_DIR / "users"

for directory in [DATA_DIR, LOGS_DIR, USERS_DIR]:
    directory.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    # === VK BOT ===
    vk_group_token: str = Field(alias="VK_GROUP_TOKEN")
    admin_vk_id: str = Field(alias="ADMIN_VK_ID")

    # === YANDEX.DISK ===
    yandex_disk_token: str = Field(alias="YANDEX_DISK_TOKEN")

    # === Прокси (только для zakupki.gov.ru если нужен) ===
    proxy_url: str | None = Field(default=None, alias="PROXY_URL")
    rejections_proxy_url: str | None = Field(default=None, alias="REJECTIONS_PROXY_URL")

    # === Общие настройки ===
    timezone: str = Field(default="Europe/Moscow", alias="TIMEZONE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # === Парсер отказов — поиск ===
    max_parse_pages_rejections: int = Field(
        default=100, alias="MAX_PARSE_PAGES_REJECTIONS"
    )
    max_workers_rejections: int = Field(default=3, alias="MAX_WORKERS_REJECTIONS")
    max_check_workers: int = Field(default=5, alias="MAX_CHECK_WORKERS")
    delay_between_pages_min: float = Field(default=0.1, alias="DELAY_BETWEEN_PAGES_MIN")
    delay_between_pages_max: float = Field(default=0.3, alias="DELAY_BETWEEN_PAGES_MAX")
    delay_between_contracts_min: float = Field(
        default=1.0, alias="DELAY_BETWEEN_CONTRACTS_MIN"
    )
    delay_between_contracts_max: float = Field(
        default=3.0, alias="DELAY_BETWEEN_CONTRACTS_MAX"
    )
    search_page_batch_size: int = Field(default=20, alias="SEARCH_PAGE_BATCH_SIZE")
    records_per_page: str = Field(default="50", alias="RECORDS_PER_PAGE")

    multi_ip_config_json: str = Field(default="[]", alias="MULTI_IP_CONFIG_JSON")
    ip_ban_duration_seconds: int = Field(default=900, alias="IP_BAN_DURATION_SECONDS")
    proxy_request_timeout: int = Field(default=45, alias="PROXY_REQUEST_TIMEOUT")
    local_request_timeout: int = Field(default=30, alias="LOCAL_REQUEST_TIMEOUT")

    @property
    def multi_ip_config(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.multi_ip_config_json)
            if not isinstance(data, list):
                return []
            return data
        except Exception:
            return []

    # === SMTP — отправка писем поставщикам при отказе (Арина Еремян, rnp-help.ru) ===
    smtp_host: str = Field(default="smtp.timeweb.ru", alias="SMTP_HOST")
    smtp_port: int = Field(default=465, alias="SMTP_PORT")
    smtp_login: str = Field(default="lawyer@rnp-help.ru", alias="SMTP_LOGIN")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    email_rejections_enabled: bool = Field(
        default=False, alias="EMAIL_REJECTIONS_ENABLED"
    )
    email_send_delay: int = Field(default=5, alias="EMAIL_SEND_DELAY")
    email_resend_days: int = Field(default=30, alias="EMAIL_RESEND_DAYS")

    # === Корпоративные заголовки писем ===
    from_name: str = Field(default="Еремян Арина", alias="FROM_NAME")
    from_email: str = Field(default="lawyer@rnp-help.ru", alias="FROM_EMAIL")
    reply_to: str = Field(default="lawyer@rnp-help.ru", alias="REPLY_TO")
    unsubscribe_email: str = Field(
        default="lawyer@rnp-help.ru", alias="UNSUBSCRIBE_EMAIL"
    )
    bounce_email: str = Field(default="bounce@rnp-help.ru", alias="BOUNCE_EMAIL")
    dmarc_email: str = Field(default="dmarc@rnp-help.ru", alias="DMARC_EMAIL")

    class Config:
        env_file = BASE_DIR / ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()

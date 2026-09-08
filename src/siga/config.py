"""Конфигурация из переменных окружения.

Единственное место, которое читает окружение. Всё остальное получает готовый
`Settings` аргументом — так код тестируется без подмены `os.environ`.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_TOKEN_RE = re.compile(r"^\d+:[\w-]{30,}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["local", "prod"] = "local"
    log_level: str = "INFO"

    bot_token: SecretStr
    bot_mode: Literal["polling", "webhook"] = "polling"

    webhook_base_url: str | None = None
    webhook_path: str = "/telegram/webhook"
    webhook_secret: SecretStr | None = None
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080

    database_url: str = "postgresql+asyncpg://siga:siga@localhost:5432/siga"

    @field_validator("bot_token")
    @classmethod
    def _check_token_shape(cls, value: SecretStr) -> SecretStr:
        # Проверяем форму, а не валидность: пустой или обрезанный токен —
        # самая частая ошибка при первом запуске, и падать нужно понятно.
        if not _TOKEN_RE.match(value.get_secret_value()):
            raise ValueError(
                "BOT_TOKEN не похож на токен Telegram (ожидается вид 123456789:AA…). "
                "Получить или перевыпустить: @BotFather → /mybots → API Token"
            )
        return value

    @model_validator(mode="after")
    def _check_webhook_config(self) -> Settings:
        if self.bot_mode != "webhook":
            return self
        missing = [
            name
            for name, value in (
                ("WEBHOOK_BASE_URL", self.webhook_base_url),
                ("WEBHOOK_SECRET", self.webhook_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"BOT_MODE=webhook требует {', '.join(missing)}")
        if not self.webhook_base_url.startswith("https://"):  # type: ignore[union-attr]
            raise ValueError("WEBHOOK_BASE_URL должен быть https — Telegram не примет http")
        if not self.webhook_path.startswith("/"):
            raise ValueError("WEBHOOK_PATH должен начинаться со слэша")
        return self

    @property
    def webhook_url(self) -> str:
        """Полный адрес, который регистрируется в Telegram."""
        if not self.webhook_base_url:
            raise RuntimeError("webhook_url доступен только при BOT_MODE=webhook")
        return self.webhook_base_url.rstrip("/") + self.webhook_path


@lru_cache
def get_settings() -> Settings:
    return Settings()  # значения приходят из окружения и .env

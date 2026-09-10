"""Конфигурация из переменных окружения.

Единственное место, которое читает окружение. Всё остальное получает готовый
`Settings` аргументом — так код тестируется без подмены `os.environ`.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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

    #: Кого пускать в бота — номера Telegram через запятую. Пустой список
    #: означает «открыт всем»: локальный запуск и тесты не должны требовать
    #: настройки, а про открытого бота предупреждает `runner` на старте.
    #:
    #: Номера, а не `@username`: имя владелец меняет и передаёт, номер — нет.
    #: Инвайты (§5.1, этап 6) встанут сверху этого списка, а не вместо него:
    #: тот, кто выдаёт коды, сам должен быть в нём.
    allowed_tg_user_ids: Annotated[frozenset[int], NoDecode] = frozenset()

    webhook_base_url: str | None = None
    webhook_path: str = "/telegram/webhook"
    webhook_secret: SecretStr | None = None
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080

    database_url: str = "postgresql+asyncpg://siga:siga@localhost:5432/siga"

    # --- LLM ---
    # По умолчанию выключена: без ключа бот должен подниматься и работать в
    # той части, где LLM не нужна, а не падать на старте.
    llm_provider: Literal["offline", "deepseek"] = "offline"
    deepseek_api_key: SecretStr | None = None
    #: Без `/v1` — так в документации DeepSeek и во всех их примерах.
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model_text: str = "deepseek-v4-flash"
    #: Единственная модель DeepSeek, принимающая картинки (R1, разбор фото).
    llm_model_vision: str = "deepseek-v4-flash-vision-exp"
    #: Щедро: ответ на 20 слов идёт секунд двадцать, а обрыв по таймауту стоит
    #: дороже ожидания — партию придётся спрашивать заново, снова за деньги.
    llm_timeout_s: float = 120.0
    llm_max_retries: int = 3

    @field_validator("allowed_tg_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        """Разобрать «237723839, 42» в набор номеров.

        Без этого pydantic-settings ждёт от множества строку JSON, а в `.env`
        по-человечески пишут через запятую (отсюда же и `NoDecode`). Мусор
        внутри не проглатываем: непонятный номер — это опечатка, из-за которой
        бот либо не пустит хозяина, либо пустит лишнего, и узнать об этом
        лучше на старте, чем из переписки.
        """
        if not isinstance(value, str):
            return value
        return [chunk.strip() for chunk in value.split(",") if chunk.strip()]

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

    @model_validator(mode="after")
    def _check_llm_config(self) -> Settings:
        # Падаем на старте, а не на первом обогащении: иначе человек узнает
        # про забытый ключ из «не получилось» посреди сценария импорта.
        if self.llm_provider == "deepseek" and not self.deepseek_api_key:
            raise ValueError(
                "LLM_PROVIDER=deepseek требует DEEPSEEK_API_KEY "
                "(ключ создаётся на platform.deepseek.com → API keys)"
            )
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

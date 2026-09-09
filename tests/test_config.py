"""Проверки конфигурации.

Везде `_env_file=None`: иначе тесты подхватят настоящий .env разработчика
и результат будет зависеть от машины.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from siga.config import Settings

FAKE_TOKEN = "123456789:AAHfake-token-for-tests-0123456789"
MINIMAL = {"_env_file": None, "bot_token": FAKE_TOKEN}


def test_polling_is_default() -> None:
    settings = Settings(**MINIMAL)  # type: ignore[arg-type]
    assert settings.bot_mode == "polling"


def test_webhook_requires_url_and_secret() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(**MINIMAL, bot_mode="webhook")  # type: ignore[arg-type]

    message = str(exc.value)
    assert "WEBHOOK_BASE_URL" in message
    assert "WEBHOOK_SECRET" in message


def test_webhook_rejects_plain_http() -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(  # type: ignore[arg-type]
            **MINIMAL,
            bot_mode="webhook",
            webhook_base_url="http://example.com",
            webhook_secret="s3cret",
        )


def test_webhook_url_is_joined_without_double_slash() -> None:
    settings = Settings(  # type: ignore[arg-type]
        **MINIMAL,
        bot_mode="webhook",
        webhook_base_url="https://example.com/",
        webhook_secret="s3cret",
        webhook_path="/telegram/webhook",
    )
    assert settings.webhook_url == "https://example.com/telegram/webhook"


def test_webhook_url_unavailable_in_polling_mode() -> None:
    settings = Settings(**MINIMAL)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        _ = settings.webhook_url


def test_token_is_not_leaked_by_repr() -> None:
    settings = Settings(**MINIMAL)  # type: ignore[arg-type]
    assert FAKE_TOKEN not in repr(settings)


@pytest.mark.parametrize("bad", ["", "abc", "123:short", "AAHfake-token-without-the-id-part"])
def test_malformed_token_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError, match="BotFather"):
        Settings(_env_file=None, bot_token=bad)  # type: ignore[arg-type]


# --- LLM ---------------------------------------------------------------------


def test_llm_is_off_until_asked() -> None:
    """Без ключа бот поднимается: импорт слов от модели не зависит."""
    assert Settings(**MINIMAL).llm_provider == "offline"  # type: ignore[arg-type]


def test_deepseek_without_a_key_fails_at_startup() -> None:
    """Лучше не подняться, чем сказать «не получилось» посреди импорта."""
    with pytest.raises(ValidationError, match="DEEPSEEK_API_KEY"):
        Settings(**MINIMAL, llm_provider="deepseek")  # type: ignore[arg-type]


def test_llm_key_is_not_leaked_by_repr() -> None:
    settings = Settings(**MINIMAL, llm_provider="deepseek", deepseek_api_key="sk-secret")  # type: ignore[arg-type]
    assert "sk-secret" not in repr(settings)


def test_vision_route_gets_the_only_model_that_takes_images() -> None:
    from siga.llm.base import Route
    from siga.llm.factory import route_models

    models = route_models(Settings(**MINIMAL))  # type: ignore[arg-type]

    assert set(models) == set(Route), "маршрут без модели — падение клиента на старте"
    assert models[Route.VISION_IMPORT] == "deepseek-v4-flash-vision-exp"
    assert models[Route.ENRICH] == "deepseek-v4-flash"

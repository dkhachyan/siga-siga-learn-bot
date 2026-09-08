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

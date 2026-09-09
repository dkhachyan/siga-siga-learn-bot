"""Слой LLM: разбор структурированного ответа и поведение клиента по HTTP.

Клиент проверяется против настоящего локального сервера aiohttp, а не мока:
интересны как раз коды ответа, заголовки и повторы, а мок на месте
`session.post` их и не покрывает.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from siga.llm.base import (
    Completion,
    LlmBadOutput,
    LlmError,
    LlmUnavailable,
    Message,
    Route,
    Usage,
    complete_json,
    parse_json,
    strip_fence,
)
from siga.llm.deepseek import DeepSeekClient
from siga.llm.offline import OfflineClient
from tests.fake_llm import ScriptedClient

MODELS = dict.fromkeys(Route, "test-model")


class Word(BaseModel):
    lemma: str
    gender: str | None = None


# --- разбор ответа -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"lemma": "νερό"}', '{"lemma": "νερό"}'),
        ('```json\n{"lemma": "νερό"}\n```', '{"lemma": "νερό"}'),
        ('```\n{"lemma": "νερό"}\n```', '{"lemma": "νερό"}'),
        ('  {"lemma": "νερό"}  ', '{"lemma": "νερό"}'),
    ],
)
def test_strip_fence_unwraps_a_code_block(raw: str, expected: str) -> None:
    assert strip_fence(raw) == expected


def test_parse_json_returns_the_schema() -> None:
    word = parse_json('{"lemma": "νερό", "gender": "n"}', Word)
    assert word.lemma == "νερό"
    assert word.gender == "n"


def test_parse_json_reads_through_a_fence() -> None:
    assert parse_json('```json\n{"lemma": "νερό"}\n```', Word).lemma == "νερό"


def test_parse_json_rejects_broken_json() -> None:
    with pytest.raises(LlmBadOutput, match="не разобрался"):
        parse_json('{"lemma": "νερό",}', Word)


def test_parse_json_rejects_the_wrong_shape() -> None:
    """Валидный JSON не той формы — такая же неудача, как и битый."""
    with pytest.raises(LlmBadOutput, match="схему Word"):
        parse_json('{"translation": "вода"}', Word)


def test_usage_adds_up() -> None:
    total = Usage(10, 20, 5) + Usage(1, 2, 3)
    assert (total.input_tokens, total.output_tokens, total.cached_input_tokens) == (11, 22, 8)
    assert total.total_tokens == 33


def test_completion_knows_it_was_cut_off() -> None:
    assert Completion("…", "m", finish_reason="length").truncated
    assert not Completion("…", "m", finish_reason="stop").truncated


# --- complete_json -----------------------------------------------------------


async def test_complete_json_returns_the_parsed_answer() -> None:
    client = ScriptedClient('{"lemma": "νερό"}')
    word, usage = await complete_json(
        client, route=Route.ENRICH, messages=[Message("user", "?")], schema=Word
    )
    assert word.lemma == "νερό"
    assert usage.total_tokens == 15
    assert len(client.asked) == 1


async def test_complete_json_always_asks_for_json_mode() -> None:
    """Без этого DeepSeek волен ответить прозой, и разбор развалится."""
    client = ScriptedClient("не JSON", '{"lemma": "νερό"}')
    await complete_json(client, route=Route.ENRICH, messages=[Message("user", "?")], schema=Word)
    assert client.json_modes == [True, True], "включая починочный вызов"


async def test_complete_json_repairs_a_broken_answer_once() -> None:
    client = ScriptedClient("не JSON вовсе", '{"lemma": "νερό"}')
    word, usage = await complete_json(
        client, route=Route.ENRICH, messages=[Message("user", "?")], schema=Word
    )

    assert word.lemma == "νερό"
    assert usage.total_tokens == 30, "оплачены оба вызова, а не последний"

    repair = client.asked[1]
    assert repair[1] == Message("assistant", "не JSON вовсе"), "модели вернули её же ответ"
    assert "не разобрался" in repair[2].text, "и претензию, а не пустое «попробуй снова»"


async def test_complete_json_gives_up_after_one_repair() -> None:
    """Второй починки нет: если модель не поняла дважды, виноват промпт."""
    client = ScriptedClient("мусор", "снова мусор")
    with pytest.raises(LlmBadOutput):
        await complete_json(
            client, route=Route.ENRICH, messages=[Message("user", "?")], schema=Word
        )
    assert len(client.asked) == 2


async def test_complete_json_can_refuse_to_repair() -> None:
    client = ScriptedClient("мусор")
    with pytest.raises(LlmBadOutput):
        await complete_json(
            client,
            route=Route.ENRICH,
            messages=[Message("user", "?")],
            schema=Word,
            repair=False,
        )
    assert len(client.asked) == 1


# --- клиент DeepSeek ---------------------------------------------------------


def reply(
    content: str | None = '{"lemma": "νερό"}',
    *,
    usage: dict[str, int] | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    return {
        "model": "test-model",
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": usage if usage is not None else {"prompt_tokens": 100, "completion_tokens": 20},
    }


class FakeApi:
    """Локальный сервер вместо api.deepseek.com.

    Ответы выдаются по очереди из `load()`; когда очередь пуста — обычный
    успешный ответ, чтобы тесты счастливого пути ничего не готовили.
    """

    def __init__(self) -> None:
        self.url = ""
        self.requests: list[dict[str, Any]] = []
        self.headers: list[Any] = []
        self._responses: list[tuple[int, Any]] = []

    def load(self, *responses: tuple[int, Any]) -> None:
        self._responses.extend(responses)

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        self.headers.append(request.headers)
        status, body = self._responses.pop(0) if self._responses else (200, reply())
        if isinstance(body, str):
            return web.Response(status=status, text=body)
        return web.json_response(body, status=status)


@pytest_asyncio.fixture
async def api() -> AsyncIterator[FakeApi]:
    fake = FakeApi()
    app = web.Application()
    app.router.add_post("/chat/completions", fake.handle)
    server = TestServer(app)
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторы проверяем без настоящих пауз."""
    monkeypatch.setattr(DeepSeekClient, "_backoff", lambda self, attempt: 0.0)


def client_for(api: FakeApi, **kwargs: Any) -> DeepSeekClient:
    return DeepSeekClient(api_key="test-key", models=MODELS, base_url=api.url, **kwargs)


async def test_client_sends_the_key_and_the_route_model(api: FakeApi) -> None:
    client = client_for(api)
    try:
        result = await client.complete(route=Route.ENRICH, messages=[Message("user", "привет")])
    finally:
        await client.close()

    assert api.headers[0]["Authorization"] == "Bearer test-key"
    sent = api.requests[0]
    assert sent["model"] == "test-model"
    assert sent["messages"] == [{"role": "user", "content": "привет"}]
    assert sent["stream"] is False
    assert "response_format" not in sent, "JSON-режим просят явно, а не всегда"
    assert result.text == '{"lemma": "νερό"}'


async def test_client_turns_thinking_off(api: FakeApi) -> None:
    """У DeepSeek думанье включено по умолчанию и оплачивается как выход.

    Словарной работе оно не нужно, а счёт от него растёт втрое.
    """
    client = client_for(api)
    try:
        await client.complete(route=Route.ENRICH, messages=[Message("user", "?")])
    finally:
        await client.close()

    assert api.requests[0]["thinking"] == {"type": "disabled"}


async def test_client_always_caps_the_output(api: FakeApi) -> None:
    """Без `max_tokens` модель в JSON-режиме умеет гнать пробелы до предела
    контекста: запрос «зависает» и оплачивается целиком."""
    client = client_for(api)
    try:
        await client.complete(route=Route.ENRICH, messages=[Message("user", "?")])
    finally:
        await client.close()

    assert api.requests[0]["max_tokens"] > 0


async def test_client_reports_a_body_that_is_not_json(api: FakeApi) -> None:
    """Страница от прокси с кодом 200 — ошибка должна называть её начало."""
    api.load((200, "<html>502 Bad Gateway</html>"))
    client = client_for(api)
    try:
        with pytest.raises(LlmBadOutput, match="Bad Gateway"):
            await client.complete(route=Route.TURN, messages=[Message("user", "?")])
    finally:
        await client.close()


async def test_client_asks_for_json_mode_when_told(api: FakeApi) -> None:
    client = client_for(api)
    try:
        await client.complete(
            route=Route.ENRICH,
            messages=[Message("user", "?")],
            json_mode=True,
            max_output_tokens=256,
        )
    finally:
        await client.close()

    assert api.requests[0]["response_format"] == {"type": "json_object"}
    assert api.requests[0]["max_tokens"] == 256


async def test_client_reads_usage_including_the_cache(api: FakeApi) -> None:
    api.load(
        (
            200,
            reply(
                usage={
                    "prompt_tokens": 1500,
                    "completion_tokens": 300,
                    "prompt_cache_hit_tokens": 1280,
                }
            ),
        )
    )
    client = client_for(api)
    try:
        result = await client.complete(route=Route.TURN, messages=[Message("user", "?")])
    finally:
        await client.close()

    assert result.usage == Usage(1500, 300, 1280)


async def test_client_survives_a_response_without_usage(api: FakeApi) -> None:
    """Учёт токенов не должен ломать сценарий, если провайдер сменит поля."""
    api.load((200, reply(usage={})))
    client = client_for(api)
    try:
        result = await client.complete(route=Route.TURN, messages=[Message("user", "?")])
    finally:
        await client.close()

    assert result.usage == Usage()
    assert result.text


async def test_client_retries_a_server_error_and_then_succeeds(api: FakeApi) -> None:
    api.load((500, "внутренняя ошибка"), (200, reply()))
    client = client_for(api)
    try:
        result = await client.complete(route=Route.TURN, messages=[Message("user", "?")])
    finally:
        await client.close()

    assert len(api.requests) == 2
    assert result.text


async def test_client_gives_up_on_rate_limit_with_llm_unavailable(api: FakeApi) -> None:
    api.load(*[(429, "slow down")] * 3)
    client = client_for(api, max_retries=2)
    try:
        with pytest.raises(LlmUnavailable, match="после 3 попыток"):
            await client.complete(route=Route.TURN, messages=[Message("user", "?")])
    finally:
        await client.close()

    assert len(api.requests) == 3


async def test_client_does_not_retry_a_bad_key(api: FakeApi) -> None:
    """401 повтором не лечится — только зря ждать человека в чате."""
    api.load((401, "Authentication Fails"))
    client = client_for(api)
    try:
        with pytest.raises(LlmError, match="401") as caught:
            await client.complete(route=Route.TURN, messages=[Message("user", "?")])
    finally:
        await client.close()

    assert not isinstance(caught.value, LlmUnavailable), "это не «недоступен», а «неверный ключ»"
    assert len(api.requests) == 1


async def test_client_reports_an_empty_answer(api: FakeApi) -> None:
    """Пустой content при обрезанном ответе — думающая модель съела лимит."""
    api.load((200, reply(content=None, finish_reason="length")))
    client = client_for(api)
    try:
        with pytest.raises(LlmBadOutput, match="обрезан"):
            await client.complete(route=Route.TURN, messages=[Message("user", "?")])
    finally:
        await client.close()


async def test_client_needs_a_model_for_every_route() -> None:
    with pytest.raises(ValueError, match="R1"):
        DeepSeekClient(api_key="k", models={Route.TURN: "m"}, base_url="http://localhost")


async def test_client_needs_a_key() -> None:
    with pytest.raises(ValueError, match="ключ"):
        DeepSeekClient(api_key="", models=MODELS, base_url="http://localhost")


# --- заглушка ----------------------------------------------------------------


async def test_offline_client_refuses_honestly() -> None:
    """Без ключа заглушка отказывает, а не выдумывает греческую грамматику."""
    with pytest.raises(LlmUnavailable, match="LLM_PROVIDER"):
        await OfflineClient().complete(route=Route.ENRICH, messages=[Message("user", "?")])

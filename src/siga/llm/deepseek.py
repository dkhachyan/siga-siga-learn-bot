"""Клиент DeepSeek: единственное место, которое знает про их HTTP-протокол.

API у них OpenAI-совместимый, и официального Python-SDK нет — документация
предлагает взять пакет `openai` и переставить ему base_url. Мы этого не
делаем: aiohttp уже в дереве зависимостей (его тянет aiogram), запрос тут
ровно один, а класс ошибок и политику повторов всё равно писать самим —
через чужой SDK они пробиваются хуже, чем напрямую.

Наружу отсюда торчит только `base.LlmClient`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

import aiohttp

from siga.llm.base import (
    Completion,
    LlmBadOutput,
    LlmError,
    LlmUnavailable,
    Message,
    Route,
    Usage,
)

log = logging.getLogger(__name__)

#: Коды, при которых повтор имеет смысл. Всё остальное — наша вина
#: (401 — ключ, 400 — payload), и повтор её не исправит.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})

#: Верхняя граница паузы между попытками: дальше ждать бессмысленно, человек
#: в чате уже решил, что бот сломался (§8.5).
_MAX_BACKOFF_S = 8.0

#: Потолок выхода по умолчанию. Документация DeepSeek прямо предупреждает:
#: в JSON-режиме без `max_tokens` модель может гнать пробелы до предела
#: контекста — запрос «зависает» и оплачивается целиком.
_DEFAULT_MAX_OUTPUT = 4096

#: У DeepSeek думанье включено по умолчанию (effort=high), а токены
#: рассуждения оплачиваются как выход — втрое дороже входа. Наши маршруты
#: рассуждения не требуют: это словарная работа, а не задачи на логику.
#: Когда появится маршрут, которому оно нужно, флаг переедет в `complete()`.
_THINKING_OFF = MappingProxyType({"type": "disabled"})


class DeepSeekClient:
    """Асинхронный клиент к `/chat/completions`."""

    def __init__(
        self,
        *,
        api_key: str,
        models: Mapping[Route, str],
        base_url: str,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT,
    ) -> None:
        if not api_key:
            raise ValueError("нужен ключ DeepSeek")
        missing = [route for route in Route if route not in models]
        if missing:
            raise ValueError(f"не задана модель для маршрутов: {', '.join(missing)}")

        self._api_key = api_key
        self._models = MappingProxyType(dict(models))
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._max_retries = max_retries
        self._max_output_tokens = max_output_tokens
        # Сессия создаётся при первом запросе, а не здесь: aiohttp привязывает
        # её к текущему event loop, а конструктор зовут до старта цикла.
        self._session: aiohttp.ClientSession | None = None

    async def complete(
        self,
        *,
        route: Route,
        messages: Sequence[Message],
        json_mode: bool = False,
        max_output_tokens: int | None = None,
    ) -> Completion:
        model = self._models[route]
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.text} for m in messages],
            "stream": False,
            "thinking": dict(_THINKING_OFF),
            "max_tokens": max_output_tokens or self._max_output_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = await self._post("/chat/completions", payload)
        return self._to_completion(data, fallback_model=model, route=route)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # --- HTTP --------------------------------------------------------------

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Запрос с повторами. Последняя ошибка становится `LlmUnavailable`."""
        session = self._ensure_session()
        url = self._base_url + path
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last: str = "причина неизвестна"

        for attempt in range(self._max_retries + 1):
            if attempt:
                await asyncio.sleep(self._backoff(attempt))
            try:
                async with session.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                ) as response:
                    if response.status == 200:
                        return _decode(await response.text())

                    body = (await response.text())[:500]
                    if response.status not in _RETRY_STATUSES:
                        # Ключ и payload повтором не лечатся — падаем сразу.
                        raise LlmError(f"DeepSeek ответил {response.status}: {body}")
                    last = f"{response.status}: {body}"
            except TimeoutError:
                last = f"таймаут {self._timeout.total:.0f} с"
            except aiohttp.ClientError as error:
                last = f"{type(error).__name__}: {error}"

            log.warning(
                "DeepSeek: попытка %s из %s не удалась (%s)",
                attempt + 1,
                self._max_retries + 1,
                last,
            )

        raise LlmUnavailable(f"DeepSeek недоступен после {self._max_retries + 1} попыток — {last}")

    def _backoff(self, attempt: int) -> float:
        """Экспонента с разбросом: без разброса повторы всех пользователей
        сходятся в один момент и добивают провайдера, который только встал."""
        window = min(_MAX_BACKOFF_S, 0.5 * 2**attempt)
        return random.uniform(window / 2, window)

    # --- разбор ответа -----------------------------------------------------

    def _to_completion(
        self, data: dict[str, Any], *, fallback_model: str, route: Route
    ) -> Completion:
        choices = data.get("choices") or []
        if not choices:
            raise LlmBadOutput("в ответе DeepSeek нет ни одного choice")

        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content")
        finish_reason = choice.get("finish_reason")

        if not text:
            # Пустой `content` при непустом `reasoning_content` — известное
            # поведение думающих моделей: рассуждение съело весь лимит выхода.
            hint = " (ответ обрезан лимитом выхода)" if finish_reason == "length" else ""
            raise LlmBadOutput(f"DeepSeek вернул пустой ответ{hint}")

        usage = _usage_from(data.get("usage") or {})
        log.info(
            "llm %s: модель=%s вход=%s (из кэша %s) выход=%s",
            route,
            data.get("model", fallback_model),
            usage.input_tokens,
            usage.cached_input_tokens,
            usage.output_tokens,
        )
        return Completion(
            text=text,
            model=str(data.get("model", fallback_model)),
            usage=usage,
            finish_reason=finish_reason,
        )


def _decode(body: str) -> dict[str, Any]:
    """Разобрать тело ответа.

    Читаем текстом, а не `response.json()`, по двум причинам. Пока DeepSeek
    ждёт очереди, он шлёт в тело пустые строки — `json.loads` их переживает.
    А если вместо ответа пришла страница от прокси, ошибка должна называть
    начало этой страницы, иначе искать причину не по чему.
    """
    try:
        data = json.loads(body)
    except ValueError as error:
        raise LlmBadOutput(f"DeepSeek вернул не JSON ({error}): {body[:200]!r}") from error
    if not isinstance(data, dict):
        raise LlmBadOutput(f"DeepSeek вернул {type(data).__name__}, а не объект")
    return data


def _usage_from(raw: Mapping[str, Any]) -> Usage:
    """Токены из ответа. Имена полей — OpenAI-совместимые плюс кэш DeepSeek.

    Отсутствующее поле — это ноль, а не падение: учёт токенов не должен
    ломать пользовательский сценарий, если провайдер переименует поле.
    """

    def number(key: str) -> int:
        value = raw.get(key)
        return value if isinstance(value, int) else 0

    return Usage(
        input_tokens=number("prompt_tokens"),
        output_tokens=number("completion_tokens"),
        cached_input_tokens=number("prompt_cache_hit_tokens"),
    )


__all__ = ["DeepSeekClient"]

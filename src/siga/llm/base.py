"""Интерфейс LLM-слоя: чем маршруты §8 говорят с провайдером.

Смысл этого файла — граница. Хендлеры и воркеры знают только `LlmClient`,
`Message` и `Completion`; какой именно провайдер за ними стоит, знает
`factory`. Провайдера мы уже один раз меняли (Anthropic → DeepSeek), и цена
той замены должна оставаться равной одному файлу в `llm/`.

Здесь нет ни сети, ни ключей: только типы и разбор ответа.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError


class Route(StrEnum):
    """Маршруты §8.1. Модель и параметры настраиваются на каждый отдельно."""

    VISION_IMPORT = "R1"
    ENRICH = "R2"
    FRAMES = "R3"
    TURN = "R4"
    FREE_CHAT = "R5"


Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    text: str


@dataclass(frozen=True, slots=True)
class Usage:
    """Токены одного вызова — для учёта цены (§8.6) и проверки кэша (§8.4)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    """Часть входа, прочитанная из кэша. Ноль на втором вызове подряд — повод
    искать изменчивое в начале промпта."""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """Ответ обрезан лимитом выхода — JSON в нём почти наверняка битый."""
        return self.finish_reason == "length"


class LlmError(Exception):
    """Базовая ошибка слоя. Вызывающий код ловит её и извиняется перед человеком."""


class LlmUnavailable(LlmError):
    """Провайдер не ответил: сеть, таймаут, 5xx, исчерпанный лимит.

    Отделено от `LlmBadOutput` потому, что реакция разная: здесь уместно
    предложить повторить позже, там — чинить промпт.
    """


class LlmBadOutput(LlmError):
    """Ответ пришёл, но это не то, что просили: не JSON или не та схема."""


class LlmClient(Protocol):
    """Минимум, который нужен маршрутам. Реализации — в `deepseek` и `offline`."""

    async def complete(
        self,
        *,
        route: Route,
        messages: Sequence[Message],
        json_mode: bool = False,
        max_output_tokens: int | None = None,
    ) -> Completion: ...

    async def close(self) -> None: ...


# --- разбор структурированного ответа ----------------------------------------

#: DeepSeek умеет только `json_object` — без схемы на стороне API. Значит
#: схему описываем в промпте, а проверяем сами, и модель иногда оборачивает
#: JSON в ```-блок.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)


def strip_fence(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group("body") if match else text.strip()


def parse_json[Model: BaseModel](text: str, schema: type[Model]) -> Model:
    """Разобрать ответ модели в pydantic-схему.

    Обе неудачи — невалидный JSON и валидный JSON не той формы — это одна и
    та же ошибка с точки зрения вызывающего: модель не выполнила инструкцию.
    """
    body = strip_fence(text)
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as error:
        raise LlmBadOutput(f"ответ не разобрался как JSON: {error}") from error

    try:
        return schema.model_validate(raw)
    except ValidationError as error:
        raise LlmBadOutput(f"JSON не подошёл под схему {schema.__name__}: {error}") from error


async def complete_json[Model: BaseModel](
    client: LlmClient,
    *,
    route: Route,
    messages: Sequence[Message],
    schema: type[Model],
    max_output_tokens: int | None = None,
    repair: bool = True,
) -> tuple[Model, Usage]:
    """Спросить модель и получить проверенную структуру.

    Одна попытка починки: невалидный ответ возвращается модели вместе с
    претензией. Это дешевле полного повтора и на практике чинит запятые и
    лишние поля. Вторая попытка не делается — если модель не поняла дважды,
    виноват промпт, и его надо править, а не крутить цикл за деньги.
    """
    first = await client.complete(
        route=route,
        messages=messages,
        json_mode=True,
        max_output_tokens=max_output_tokens,
    )
    try:
        return parse_json(first.text, schema), first.usage
    except LlmBadOutput as error:
        if not repair:
            raise
        complaint = str(error)

    retry = await client.complete(
        route=route,
        messages=[
            *messages,
            Message("assistant", first.text),
            Message(
                "user",
                "Твой ответ не разобрался: "
                f"{complaint}\nПришли только JSON, без пояснений и без ```-блока.",
            ),
        ],
        json_mode=True,
        max_output_tokens=max_output_tokens,
    )
    total = first.usage + retry.usage
    return parse_json(retry.text, schema), total


__all__ = [
    "Completion",
    "LlmBadOutput",
    "LlmClient",
    "LlmError",
    "LlmUnavailable",
    "Message",
    "Role",
    "Route",
    "Usage",
    "complete_json",
    "parse_json",
    "strip_fence",
]

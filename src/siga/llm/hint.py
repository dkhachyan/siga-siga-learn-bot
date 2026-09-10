"""Маршрут R7: чем ответить на вопрос Ника — 2–3 варианта по-русски.

Кнопка `💡 Что ответить` (FR-CHK-8) для случая, когда вопрос понят, а сказать
нечего. Это третий род непонимания, и первые два его не покрывают: `🔍 Разбор`
объясняет ответ ученика, которого ещё нет, а `🇷🇺 Перевод` — вопрос, который и
так понятен.

Подсказка говорит, **что** сказать, и никогда — **как** сказать это
по-гречески. Готовая греческая фраза превратила бы разговор в списывание: слово
считается выученным, когда человек сам его достал, а не скопировал из подсказки
строкой выше. Поэтому варианты по-русски, а греческое слово рисует по номерам
уже вызывающий — из базы, где оно лежит в правильной форме.

Персона в промпт не идёт: подсказку даёт интерфейс, а не Ник, — он в этот
момент ждёт ответа, а не суфлирует. Профиль тоже: то, что человек рассказал о
себе, для «что можно сказать» не нужно, а соблазн подсунуть в вариант чужой
факт — прямой путь к FR-MEM-2. Уровень нужен: вариант должен быть таким, чтобы
ученик его выговорил.

Отдельный маршрут, а не поле в `R4`, по тому же доводу, что и `R6`: подсказку
просят редко, и платить за неё на каждом ходу значит платить за выход, который
почти никто не прочитает.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from siga.core.enums import Level
from siga.llm.base import LlmClient, Message, Route, Usage, complete_json
from siga.llm.dialogue import WordBrief, level_note, words_block

log = logging.getLogger(__name__)

#: Больше трёх вариантов — уже не подсказка, а упражнение на чтение: человек
#: сидит и выбирает, вместо того чтобы говорить.
MAX_OPTIONS = 3

#: Потолок выхода. Три коротких русских фразы плюс номера слов; §8.2 требует
#: явный лимит всегда, иначе в JSON-режиме модель умеет гнать пробелы до
#: предела контекста.
MAX_OUTPUT = 350

SYSTEM_PROMPT = """\
Ты помогаешь ученику, который учит греческий и не знает, что ответить \
собеседнику. Ответ возвращаешь JSON-объектом.

Ты подсказываешь, **что** сказать, а не **как** это сказать по-гречески.

Правила:
- Варианты — по-русски. Греческих слов и фраз в ответе не пиши вовсе: готовую \
фразу ученик просто спишет, и слово останется невыученным.
- Каждый вариант — одно короткое предложение вида «скажи, что…», «спроси, …», \
«ответь, что…». Не диалог и не объяснение.
- Дай от двух до трёх вариантов, и пусть они будут о разном. Три пересказа \
одной мысли — это один вариант, а не три.
- Каждый вариант опирается хотя бы на одно целевое слово, и его номер едет в \
`word_ids`. Номера бери из списка целевых слов, других не придумывай.
- Не выдумывай факты о человеке. Если для ответа нужно личное, оставь выбор \
ему: «скажи, какой кофе любишь», а не «скажи, что любишь эспрессо».
- Ничего не разбирай и не объясняй грамматику: разбор — дело другой кнопки.

Формат ответа:

{"options": [{"ru": "Скажи, что пьёшь кофе каждое утро", "word_ids": [412]},
             {"ru": "Спроси, где он берёт кофе по дороге", "word_ids": [412]},
             {"ru": "Ответь, что предпочитаешь воду", "word_ids": [415]}]}"""


class HintOption(BaseModel):
    ru: str = ""
    word_ids: list[int] = Field(default_factory=list)


class HintResponse(BaseModel):
    options: list[HintOption] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Suggestion:
    """Один вариант ответа: что сказать и на какие слова опереться."""

    ru: str
    word_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class HintResult:
    options: tuple[Suggestion, ...]
    """Пусто, если модель ничего годного не прислала: такое не кэшируем."""
    usage: Usage


def _clean(response: HintResponse, *, allowed: Sequence[int]) -> tuple[Suggestion, ...]:
    """Отобрать годные варианты и выкинуть чужие номера слов.

    Номера приходят от модели, а слово по ним рисуется из базы — придуманный
    номер дал бы вариант с пустым местом там, где ученик ждёт слово.

    Вариант без единого годного номера всё же оставляем: «что сказать»
    по-русски полезно и без слова, а выбросить годную подсказку из-за того, что
    модель забыла номер, значит наказать человека за её невнимательность.
    """
    known = set(allowed)
    options: list[Suggestion] = []
    for option in response.options:
        ru = option.ru.strip()
        if not ru:
            continue
        options.append(
            Suggestion(
                ru=ru, word_ids=tuple(dict.fromkeys(w for w in option.word_ids if w in known))
            )
        )
        if len(options) == MAX_OPTIONS:
            break
    return tuple(options)


def build_messages(
    *,
    question: str,
    words: Sequence[WordBrief],
    level: Level,
    scene: str = "",
    user_text: str | None = None,
) -> list[Message]:
    """Промпт вызова: статика в `system`, всё изменчивое — в `user` (§8.4)."""
    parts = [level_note(level)]
    if scene.strip():
        parts.append(f"Дело происходит так: {scene.strip()}")
    if user_text and user_text.strip():
        parts.append(f"Перед этим ученик сказал: {user_text.strip()}")
    parts.append(f"Целевые слова:\n{words_block(words)}")
    parts.append(f"Ученику сказали, и он не знает, что ответить:\n{question.strip()}")

    return [
        Message("system", SYSTEM_PROMPT),
        Message("user", "\n\n".join(parts)),
    ]


async def suggest(
    client: LlmClient,
    *,
    question: str,
    words: Sequence[WordBrief],
    level: Level,
    scene: str = "",
    user_text: str | None = None,
) -> HintResult:
    """Придумать, чем ответить. Пустой ответ возвращается пустым, а не ошибкой.

    Пустоту разбирает вызывающий: её нельзя сохранять в `hint_ru`, иначе `NULL`
    перестанет значить «не спрашивали», и кнопка навсегда замолчит.
    """
    if not question.strip() or not words:
        return HintResult(options=(), usage=Usage())

    response, usage = await complete_json(
        client,
        route=Route.HINT,
        messages=build_messages(
            question=question, words=words, level=level, scene=scene, user_text=user_text
        ),
        schema=HintResponse,
        max_output_tokens=MAX_OUTPUT,
    )

    options = _clean(response, allowed=[word.word_id for word in words])
    if not options:
        log.warning("R7: модель не прислала ни одного годного варианта")
    return HintResult(options=options, usage=usage)


__all__ = [
    "MAX_OPTIONS",
    "MAX_OUTPUT",
    "SYSTEM_PROMPT",
    "HintOption",
    "HintResponse",
    "HintResult",
    "Suggestion",
    "build_messages",
    "suggest",
]

"""Маршрут R6: русский перевод реплики Ника по нажатию кнопки.

Отдельный маршрут, а не поле в ответе `R4`, потому что перевод нужен редко:
просить его у каждого хода — это платить за выход, который в девяти случаях
из десяти никто не прочитает. Здесь платим ровно за те реплики, которые
человек не понял, и результат ложится в `turns.bot_text_ru` — второе нажатие
той же кнопки уже бесплатно.

Персона в промпт не идёт: переводчик — не Ник, и его манера речи переводу не
нужна. Уровень и профиль тоже: русский перевод от них не зависит. Зато идёт
сцена и предыдущая реплика человека — без них ломаются рекасты (Ник повторяет
мысль человека правильно, и в отрыве от неё это просто фраза), эллипсисы
(«Κι εγώ.») и род сказуемого (ωραίος или ωραία — зависит от того, к кому
обращаются).

Ответ нужен один-в-один по смыслу, а не «литературно»: человек читает его
затем, чтобы понять греческую фразу, и вольный пересказ тут вреден.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel

from siga.llm.base import LlmClient, Message, Route, Usage, complete_json

log = logging.getLogger(__name__)

#: Потолок выхода. Реплика Ника — не длиннее 25 слов (FR-PER-2), значит и
#: перевод такой же; §8.2 требует явный лимит всегда, иначе в JSON-режиме
#: модель умеет гнать пробелы до предела контекста.
MAX_OUTPUT = 250

SYSTEM_PROMPT = """\
Ты переводишь с греческого на русский реплики из учебного разговора. Ответ \
возвращаешь JSON-объектом.

Правила:
- Переводи только ту фразу, которая помечена «Перевести». Всё остальное — \
контекст, он нужен, чтобы снять двусмысленность, и переводить его не надо.
- Переводи близко к тексту: человек читает перевод, чтобы понять греческую \
фразу, а не вместо неё. Вольный пересказ здесь мешает.
- Ничего не объясняй и не разбирай грамматику: ни примечаний, ни скобок с \
пояснениями, ни вариантов через слэш.
- Греческий в ответе не оставляй.
- Ответ — одна фраза по-русски, такой же длины, как исходная.

Формат ответа:

{"ru": "Доброе утро! Я иду за кофе, хочешь?"}"""


class TranslationResponse(BaseModel):
    ru: str = ""


@dataclass(frozen=True, slots=True)
class TranslationResult:
    ru: str
    """Пусто, если модель ничего не прислала: такое не кэшируем."""
    usage: Usage


def build_messages(*, text: str, scene: str = "", user_text: str | None = None) -> list[Message]:
    """Промпт вызова: статика в `system`, всё изменчивое — в `user` (§8.4)."""
    parts: list[str] = []
    if scene.strip():
        parts.append(f"Дело происходит так: {scene.strip()}")
    if user_text and user_text.strip():
        parts.append(f"Перед этим ученик сказал: {user_text.strip()}")
    parts.append(f"Перевести:\n{text.strip()}")

    return [
        Message("system", SYSTEM_PROMPT),
        Message("user", "\n\n".join(parts)),
    ]


async def translate(
    client: LlmClient,
    *,
    text: str,
    scene: str = "",
    user_text: str | None = None,
) -> TranslationResult:
    """Перевести одну реплику. Пустой ответ возвращается пустым, а не ошибкой.

    Пустоту разбирает вызывающий: её нельзя сохранять в `bot_text_ru`, иначе
    `NULL` перестанет значить «ещё не переводили», и кнопка навсегда станет
    показывать пустое окно.
    """
    if not text.strip():
        return TranslationResult(ru="", usage=Usage())

    response, usage = await complete_json(
        client,
        route=Route.TRANSLATE,
        messages=build_messages(text=text, scene=scene, user_text=user_text),
        schema=TranslationResponse,
        max_output_tokens=MAX_OUTPUT,
    )

    ru = response.ru.strip()
    if not ru:
        log.warning("R6: модель вернула пустой перевод")
    return TranslationResult(ru=ru, usage=usage)


__all__ = [
    "MAX_OUTPUT",
    "SYSTEM_PROMPT",
    "TranslationResponse",
    "TranslationResult",
    "build_messages",
    "translate",
]

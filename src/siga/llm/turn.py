"""Маршрут R4: ответ ученика → реплика Ника, оценки и решение о закрытии.

Единственный горячий маршрут (§8.1): двенадцать вызовов в сутки на активного
пользователя против одного у `R3`. Поэтому здесь всё скупо — история эпизода
короткая, потолок выхода низкий, а статический блок собирается ровно так же,
как в `R3`, чтобы префиксный кэш держался (§8.4).

Одним вызовом делается четыре вещи сразу: реплика, оценка целевых слов,
разбор по-русски и заметки в память. Разносить их по разным вызовам значило бы
платить четыре раза за один и тот же контекст.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from siga.core.enums import EpisodeIntent, ErrorType, Level, Verdict
from siga.core.memory import Profile
from siga.llm.base import LlmClient, Message, Route, Usage, complete_json
from siga.llm.dialogue import (
    WordBrief,
    gap_note,
    level_note,
    profile_block,
    system_prompt,
    words_block,
)
from siga.persona import Persona

log = logging.getLogger(__name__)

#: Потолок выхода. Реплика короткая (§3, 25 слов), но с ней едут разбор и
#: оценки; 600 токенов хватает с запасом, а обрезанный JSON стоит починочного
#: вызова — то есть вдвое дороже, чем щедрый лимит.
MAX_OUTPUT = 600

#: FR-CHK-5: больше двух исправлений за реплику — уже вычитка, а не разговор.
MAX_CORRECTIONS = 2

TASK = """\
Ты ведёшь короткий разговор с учеником, который учит греческий. Тебе дают
рамку разговора, целевые слова, историю эпизода и последний ответ ученика.
Ты отвечаешь одной репликой и заодно оцениваешь, как ученик употребил целевые
слова.

Как исправлять ошибки:
- Рекастом. Повтори мысль ученика правильно и продолжи разговор. Так \
переспрашивают в жизни: «— Πίνω νερώ κάθε πρωί. — Α, πίνεις νερό κάθε πρωί! \
Πόσα ποτήρια;»
- Не объясняй грамматику в реплике и не пиши «правильно будет». Объяснение \
идёт отдельным полем `analysis_ru`, ученик откроет его сам, если захочет.
- Не больше двух исправлений за реплику, даже если ошибок больше. Остальное \
уходит в `analysis_ru`.
- Если слово выбрано неверно или смысл потерян — не угадывай. Честно не пойми \
и переспроси по-гречески проще.
- Ответ латиницей: попроси переключить раскладку, оставаясь в характере.

Как оценивать (`word_assessments`, по одному объекту на каждое целевое слово, \
которое ученик употребил):
- `verdict: "correct"` — слово употреблено верно.
- `verdict: "almost"` — то самое слово, но не в той форме или с опечаткой.
- `verdict: "incorrect"` — не то слово или потерян смысл.
- `word_id` бери из списка целевых слов. Не оценивай слова, которых там нет.
- `unused_target_words` — те целевые слова, которых в ответе не было.

Когда закрывать разговор (`episode_action`):
- `"close"` — все целевые слова уже прозвучали, или разговор естественно \
кончился, или ученик прощается. Тогда реплика — прощание, а не новый вопрос.
- `"continue"` — во всех остальных случаях.
- Тебе говорят, сколько ходов осталось. На последнем ходу закрывай.

`analysis_ru` — короткий разбор по-русски: что было не так и как правильно. \
Одна-две фразы. Если всё верно — пустая строка.

`memory_updates` — только то, что ученик сообщил о себе сам. Ничего не \
додумывать. Пусто — нормально.

Ответ — JSON-объект, без markdown и без пояснений."""

SCHEMA = """\
Формат ответа:

{"reply_text": "Α, πίνεις νερό κάθε πρωί! Πόσα ποτήρια;",
 "reply_contains_recast": true,
 "word_assessments": [{"word_id": 412, "used": true, "verdict": "almost",
   "error_type": "case", "user_form": "νερώ", "correct_form": "νερό"}],
 "unused_target_words": [415],
 "episode_action": "continue",
 "analysis_ru": "После πίνω нужен винительный: νερό, без ударения на конце.",
 "memory_updates": {"facts": ["пьёт воду по утрам"], "recurring_errors": [],
   "recent_topics": ["кофе"], "tone_notes": ""}}

`error_type` — одно из: case, gender, article, verb_form, accent, spelling, \
wrong_word, latin, other; или null, если ошибки нет."""


class Assessment(BaseModel):
    """Оценка одного целевого слова в одном ответе (§5.4)."""

    word_id: int
    used: bool = True
    verdict: Verdict = Verdict.INCORRECT
    error_type: ErrorType | None = None
    user_form: str | None = None
    correct_form: str | None = None

    counts_as_correct: bool | None = None
    """Мнение модели о зачёте. В SRS не участвует.

    §8.3 держит это поле в схеме, а FR-CHK-1 объясняет зачем: правила зачёта
    продуктовые и живут в коде (§5.7), а не в промпте. Нормативен `verdict`;
    поле остаётся, потому что расхождение между ним и нашим правилом — готовый
    сигнал, что промпт и продукт разъехались.
    """

    def as_json(self) -> dict[str, Any]:
        """То, что уезжает в `turns.assessments`."""
        return self.model_dump(mode="json", exclude_none=True)


class MemoryUpdates(BaseModel):
    facts: list[str] = Field(default_factory=list)
    recurring_errors: list[str] = Field(default_factory=list)
    recent_topics: list[str] = Field(default_factory=list)
    tone_notes: str | None = None

    def is_empty(self) -> bool:
        return not (self.facts or self.recurring_errors or self.recent_topics or self.tone_notes)


class TurnResponse(BaseModel):
    reply_text: str = ""
    reply_contains_recast: bool = False
    word_assessments: list[Assessment] = Field(default_factory=list)
    unused_target_words: list[int] = Field(default_factory=list)
    episode_action: Literal["continue", "close"] = "continue"
    analysis_ru: str = ""
    memory_updates: MemoryUpdates = Field(default_factory=MemoryUpdates)

    @property
    def wants_close(self) -> bool:
        return self.episode_action == "close"

    def corrections(self) -> list[dict[str, Any]]:
        """Исправления для `turns.corrections` и для `🔍 Разбор` (FR-CHK-2)."""
        return [
            {
                "word_id": item.word_id,
                "error_type": item.error_type.value if item.error_type else None,
                "user_form": item.user_form,
                "correct_form": item.correct_form,
            }
            for item in self.word_assessments
            if item.verdict is not Verdict.CORRECT and item.correct_form
        ]


@dataclass(frozen=True, slots=True)
class Exchange:
    """Один прошедший ход эпизода — так, как его видит модель."""

    bot_text: str
    user_text: str | None = None


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Всё изменчивое, что нужно одному вызову `R4`."""

    level: Level
    intent: EpisodeIntent
    frame: dict[str, Any] = field(default_factory=dict)
    words: Sequence[WordBrief] = ()
    history: Sequence[Exchange] = ()
    profile: Profile = field(default_factory=Profile)
    user_text: str = ""
    turns_left: int = 1
    now: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    answer: TurnResponse
    usage: Usage


def frame_block(frame: dict[str, Any]) -> str:
    """Рамка из `episodes.frame` в человеческом виде.

    Рамку писала модель, и полей в ней может не оказаться. Отсутствующая
    рамка — не повод отменять ход: разговор просто пойдёт без плана.
    """
    lines: list[str] = []
    scene = frame.get("scene")
    goal = frame.get("goal")
    plan = frame.get("plan")
    if isinstance(scene, str) and scene.strip():
        lines.append(f"Сцена: {scene.strip()}")
    if isinstance(goal, str) and goal.strip():
        lines.append(f"Цель: {goal.strip()}")
    if isinstance(plan, list):
        steps = [str(step).strip() for step in plan if str(step).strip()]
        if steps:
            lines.append("План: " + "; ".join(steps))
    return "\n".join(lines) if lines else "Рамки нет — веди разговор по обстановке."


def history_block(history: Sequence[Exchange]) -> str:
    """История эпизода. Ходов максимум четыре, поэтому влезает целиком."""
    lines: list[str] = []
    for exchange in history:
        lines.append(f"Ты: {exchange.bot_text}")
        if exchange.user_text:
            lines.append(f"Ученик: {exchange.user_text}")
    return "\n".join(lines)


def _turns_note(turns_left: int) -> str:
    if turns_left <= 1:
        return "Это последний ход эпизода: попрощайся и верни episode_action=close."
    return f"Ходов осталось: {turns_left}."


def build_messages(*, persona: Persona, context: TurnContext) -> list[Message]:
    """Промпт вызова. Порядок частей — от самого редко меняющегося (§8.4).

    Сначала статика в `system`, потом профиль, потом рамка и история, и в
    самом конце — ответ ученика. Внутри `messages` кэш всё равно не работает,
    но порядок «общее → частное» читается моделью как приоритет, и последнее
    сообщение она держит острее всего.
    """
    gap = gap_note(context.profile.last_seen_at, context.now) if context.now else ""
    parts = [
        level_note(context.level),
        f"Тип эпизода: {context.intent.value}.",
        _turns_note(context.turns_left),
        gap,
        f"\nЧто ты помнишь о человеке:\n{profile_block(context.profile)}",
        f"\nРамка разговора:\n{frame_block(context.frame)}",
        f"\nЦелевые слова:\n{words_block(context.words)}",
    ]
    history = history_block(context.history)
    if history:
        parts.append(f"\nЧто уже сказано:\n{history}")

    return [
        Message("system", system_prompt(persona, TASK, SCHEMA)),
        Message("user", "\n".join(part for part in parts if part)),
        Message("user", f"Ответ ученика:\n{context.user_text}"),
    ]


def _clean(answer: TurnResponse, *, target_word_ids: Sequence[int]) -> TurnResponse:
    """Выбросить оценки слов, которых в эпизоде нет.

    Модель иногда оценивает слово, которое сама же и употребила, — и тогда в
    SRS уехал бы прогресс по слову, которое человек не проходил. Заодно
    схлопываем дубли: если слово оценено дважды, берём первую оценку.
    """
    allowed = set(target_word_ids)
    kept: list[Assessment] = []
    seen: set[int] = set()
    for item in answer.word_assessments:
        if item.word_id not in allowed:
            log.warning("R4: оценка по чужому слову %s, пропускаю", item.word_id)
            continue
        if item.word_id in seen:
            continue
        seen.add(item.word_id)
        kept.append(item)

    unused = [word_id for word_id in target_word_ids if word_id not in seen]
    if kept == answer.word_assessments and unused == answer.unused_target_words:
        return answer
    return answer.model_copy(update={"word_assessments": kept, "unused_target_words": unused})


async def make_turn(
    client: LlmClient,
    *,
    persona: Persona,
    context: TurnContext,
) -> TurnResult:
    """Сходить в модель за репликой и оценками."""
    answer, usage = await complete_json(
        client,
        route=Route.TURN,
        messages=build_messages(persona=persona, context=context),
        schema=TurnResponse,
        max_output_tokens=MAX_OUTPUT,
    )
    cleaned = _clean(answer, target_word_ids=[word.word_id for word in context.words])
    return TurnResult(answer=cleaned, usage=usage)


__all__ = [
    "MAX_CORRECTIONS",
    "MAX_OUTPUT",
    "Assessment",
    "Exchange",
    "MemoryUpdates",
    "TurnContext",
    "TurnResponse",
    "TurnResult",
    "build_messages",
    "frame_block",
    "history_block",
    "make_turn",
]

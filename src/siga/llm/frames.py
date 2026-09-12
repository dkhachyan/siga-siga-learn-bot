"""Маршрут R3: рамка эпизода и первая реплика.

Рамка — это сцена, цель и план на два-четыре хода. Она нужна затем, что без
неё каждый ход придумывается заново, и разговор рассыпается на несвязанные
вопросы. `R4` рамку получает готовой и только держится её.

По FR-EP-4 рамки на день делаются одним ночным вызовом — так и есть: план дня
собирает планировщик, одним запросом на все эпизоды. Тот же маршрут зовёт
`/next`, но на одну рамку и прямо сейчас: человек, попросивший разговор вне
очереди, ждать до завтра не должен. Формат ответа рассчитан на список, поэтому
оба случая — это один и тот же запрос разной длины.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from siga.core.enums import EpisodeIntent, Level
from siga.core.memory import Profile
from siga.llm.base import LlmClient, Message, Route, Usage, complete_json
from siga.llm.dialogue import WordBrief, level_note, profile_block, system_prompt, words_block
from siga.persona import Persona

log = logging.getLogger(__name__)

#: Потолок выхода на одну рамку. Рамка — это четыре коротких поля; всё, что
#: длиннее, модель дописывает от себя, и платим мы за это по ставке выхода.
MAX_OUTPUT_PER_EPISODE = 500

TASK = """\
Тебе дают несколько греческих слов, которые ученик сейчас учит. Придумай \
короткий разговор, в котором эти слова возникают естественно, и напиши первую \
реплику — свою.

Что такое «рамка»:
- `scene` — где вы и что происходит. Одна фраза по-русски, для нас, не для \
ученика. Обычная жизнь: очередь в пекарне, сообщение в мессенджере, разговор \
о выходных. Не урок и не экзамен.
- `goal` — чего ты хочешь добиться в этом разговоре. По-русски.
- `plan` — два-четыре пункта по-русски: как разговор может пойти. Это набросок, \
а не сценарий: ученик ответит не так, как ты думаешь, и это нормально.
- `opening` — твоя первая реплика по-гречески. Её увидит ученик.

Про первую реплику:
- Она должна открывать разговор, а не проверять знания. «Καλημέρα! Πάω για \
καφέ, θέλεις;» — да. «Πώς λέγεται "кофе" στα ελληνικά;» — нет.
- На неё должно быть легко ответить. Вопрос, на который отвечают одним словом, \
разговора не начинает.
- Никаких переводов в скобках и никаких подсказок: ученик может переспросить сам.

Тип разговора приходит для каждой группы отдельно:
- D1 — слово ученику незнакомо. Употреби его сам, в понятном контексте, и дай \
ученику шанс переспросить или повторить.
- D2 — слово ученик уже видел. Задай вопрос, на который без этого слова не \
ответить, и не подсказывай его.
- T — разговор по теме вместо слов (FR-EP-9): ученик сам попросил поболтать, \
и целевых слов нет. Придумай сцену на его тему и заведи разговор, как в \
жизни, подстраиваясь под уровень ученика. Целевых слов у такого разговора \
нет и не будет — просто болтаете.

Слово «JSON» здесь не случайно: ответ должен быть JSON-объектом, без markdown \
и без пояснений."""

SCHEMA = """\
Формат ответа:

{"episodes": [{"index": 0, "scene": "Ник зовёт выпить кофе после работы", \
"goal": "спросить, что человек обычно пьёт", "plan": ["позвать на кофе", \
"спросить, что он берёт", "рассказать про свой заказ"], \
"opening": "Καλημέρα! Πάω για καφέ. Εσύ τι πίνεις το πρωί;"}]}

`index` — номер группы слов из запроса. Верни по одному объекту на каждую \
группу, в том же порядке."""


class Frame(BaseModel):
    """Рамка одного эпизода — то, что вернула модель."""

    index: int = 0
    scene: str = ""
    goal: str = ""
    plan: list[str] = Field(default_factory=list)
    opening: str = ""

    def as_frame_json(self) -> dict[str, object]:
        """То, что уезжает в `episodes.frame`. Реплика хранится отдельно."""
        return {"scene": self.scene, "goal": self.goal, "plan": self.plan}


class FramesResponse(BaseModel):
    episodes: list[Frame] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EpisodeRequest:
    """Одна будущая рамка: слова и тип разговора, который мы для них выбрали.

    У запроса типа `T` (FR-EP-9) слов нет — вместо них тема в `topic`.
    """

    intent: EpisodeIntent
    words: Sequence[WordBrief] = ()
    topic: str = ""


@dataclass(frozen=True, slots=True)
class FramesResult:
    frames: list[Frame]
    usage: Usage


def _group_line(index: int, request: EpisodeRequest) -> str:
    """Одна группа запроса: список слов или, для темы, сама тема (FR-EP-9)."""
    if request.intent is EpisodeIntent.TOPIC:
        return f"Группа {index} — тип {request.intent.value}:\nТема разговора: {request.topic}"
    return f"Группа {index} — тип {request.intent.value}:\n{words_block(request.words)}"


def build_messages(
    *,
    persona: Persona,
    level: Level,
    profile: Profile,
    requests: Sequence[EpisodeRequest],
) -> list[Message]:
    """Промпт вызова: статика в `system`, всё изменчивое — в `user` (§8.4)."""
    groups = "\n\n".join(_group_line(index, request) for index, request in enumerate(requests))
    # Заголовок честный: у тематического запроса слов нет, и «слова, вокруг
    # которых» сбило бы модель.
    header = (
        "Темы, на которые нужны разговоры"
        if all(r.intent is EpisodeIntent.TOPIC for r in requests)
        else "Слова, вокруг которых нужны разговоры"
    )
    return [
        Message("system", system_prompt(persona, TASK, SCHEMA)),
        Message(
            "user",
            f"{level_note(level)}\n\n"
            f"Что ты помнишь о человеке:\n{profile_block(profile)}\n\n"
            f"{header}:\n{groups}",
        ),
    ]


async def make_frames(
    client: LlmClient,
    *,
    persona: Persona,
    level: Level,
    profile: Profile,
    requests: Sequence[EpisodeRequest],
) -> FramesResult:
    """Попросить у модели рамки. Порядок ответа выравнивается по `index`.

    Рамка, которой модель не прислала, заменяется пустой: эпизод без сцены —
    это всё ещё разговор, пусть и более плоский, а вот отсутствие эпизода
    человек замечает сразу.
    """
    if not requests:
        return FramesResult(frames=[], usage=Usage())

    response, usage = await complete_json(
        client,
        route=Route.FRAMES,
        messages=build_messages(persona=persona, level=level, profile=profile, requests=requests),
        schema=FramesResponse,
        max_output_tokens=MAX_OUTPUT_PER_EPISODE * len(requests),
    )

    by_index = {frame.index: frame for frame in response.episodes}
    frames: list[Frame] = []
    for index in range(len(requests)):
        frame = by_index.get(index)
        if frame is None:
            log.warning("R3 не вернул рамку для группы %s", index)
            frame = Frame(index=index)
        frames.append(frame)

    return FramesResult(frames=frames, usage=usage)


__all__ = [
    "EpisodeRequest",
    "Frame",
    "FramesResponse",
    "FramesResult",
    "build_messages",
    "make_frames",
]

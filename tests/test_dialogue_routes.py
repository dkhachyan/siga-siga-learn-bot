"""Маршруты R3 и R4: промпт, разбор ответа, защита от вранья модели.

Сеть не задействована. Проверяется то, что мы делаем с ответом, и то, что
уезжает в промпт: порядок блоков — это цена каждого вызова (§8.4), а фильтр
оценок — единственное, что стоит между выдумкой модели и записью в прогресс.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from siga.core.enums import EpisodeIntent, ErrorType, Level, Verdict
from siga.core.memory import Profile
from siga.llm.dialogue import WordBrief, gap_note, profile_block, words_block
from siga.llm.frames import TASK as FRAMES_TASK
from siga.llm.frames import EpisodeRequest, make_frames
from siga.llm.turn import TASK as TURN_TASK
from siga.llm.turn import (
    Exchange,
    TurnContext,
    TurnResponse,
    build_messages,
    frame_block,
    history_block,
    make_turn,
)
from siga.persona import load as load_persona
from tests.fake_llm import ScriptedClient

NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)

NERO = WordBrief(word_id=412, lemma="το νερό", translation_ru="вода", box=0)
KAFES = WordBrief(word_id=415, lemma="ο καφές", translation_ru="кофе", box=2)

FRAME = {
    "scene": "Ник зовёт выпить кофе",
    "goal": "узнать, что человек пьёт по утрам",
    "plan": ["позвать на кофе", "спросить про утро"],
}


@pytest.fixture
def persona() -> Any:
    return load_persona()


def turn_answer(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "reply_text": "Α, πίνεις νερό κάθε πρωί! Πόσα ποτήρια;",
        "reply_contains_recast": True,
        "word_assessments": [
            {
                "word_id": 412,
                "used": True,
                "verdict": "almost",
                "counts_as_correct": True,
                "error_type": "case",
                "user_form": "νερώ",
                "correct_form": "νερό",
            }
        ],
        "unused_target_words": [],
        "episode_action": "continue",
        "analysis_ru": "После πίνω нужен винительный.",
        "memory_updates": {"facts": ["пьёт воду по утрам"]},
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def context(**overrides: Any) -> TurnContext:
    values: dict[str, Any] = {
        "level": Level.A2,
        "intent": EpisodeIntent.D2,
        "frame": FRAME,
        "words": [NERO, KAFES],
        "user_text": "Πίνω νερώ κάθε πρωί.",
        "turns_left": 3,
        "now": NOW,
    }
    values.update(overrides)
    return TurnContext(**values)


# --- общие блоки промпта ------------------------------------------------------


def test_words_carry_their_ids() -> None:
    """Оценка приходит по `word_id`: по написанию сопоставить не выйдет."""
    block = words_block([NERO, KAFES])
    assert "[412] το νερό — вода (новое)" in block
    assert "[415] ο καφές — кофе (бокс 2)" in block


def test_empty_profile_says_so_plainly() -> None:
    """Пустой профиль — повод не выдумывать, а не повод молчать (FR-PER-5)."""
    assert "Ничего о человеке не выдумывай" in profile_block(Profile())


def test_profile_goes_as_json() -> None:
    block = profile_block(Profile(facts=["работает в IT"]))
    assert json.loads(block) == {"facts": ["работает в IT"]}


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(20, ""), (300, "5 ч"), (60 * 24 * 3, "3 дн")],
)
def test_gap_note_rounds_coarsely(minutes: int, expected: str) -> None:
    """FR-MEM-5: разрыв — часть разговора, но не отчёт с точностью до минуты."""
    note = gap_note(NOW - dt.timedelta(minutes=minutes), NOW)
    assert expected in note
    if not expected:
        assert note == ""


def test_static_block_holds_nothing_variable(persona: Any) -> None:
    """§8.4: уровень, имя и даты в системном блоке обнуляют кэш префикса."""
    answer = "Δουλεύω στη Λεμεσό."
    messages = build_messages(persona=persona, context=context(user_text=answer))
    system = messages[0].text
    for level in Level:
        assert level.value not in system
    assert answer not in system, "ответ ученика — точно не статика"
    assert FRAME["scene"] not in system, "рамка меняется каждый эпизод"
    assert "Νίκος" in system, "а персона — статика, ей там и место"


# --- R3: рамки ----------------------------------------------------------------


def test_frames_task_explains_both_intents() -> None:
    assert "D1" in FRAMES_TASK
    assert "D2" in FRAMES_TASK
    assert "JSON" in FRAMES_TASK, "требование провайдера, §8.3"


async def test_frames_come_back_in_asked_order(persona: Any) -> None:
    """Модель вольна перепутать порядок — выравниваем по `index`."""
    client = ScriptedClient(
        json.dumps(
            {
                "episodes": [
                    {"index": 1, "scene": "вторая", "opening": "Δεύτερη"},
                    {"index": 0, "scene": "первая", "opening": "Πρώτη"},
                ]
            },
            ensure_ascii=False,
        )
    )
    result = await make_frames(
        client,
        persona=persona,
        level=Level.A2,
        profile=Profile(),
        requests=[
            EpisodeRequest(intent=EpisodeIntent.D1, words=[NERO]),
            EpisodeRequest(intent=EpisodeIntent.D2, words=[KAFES]),
        ],
    )
    assert [frame.scene for frame in result.frames] == ["первая", "вторая"]
    assert result.usage.output_tokens > 0


async def test_missing_frame_becomes_empty_one(persona: Any) -> None:
    """Эпизод без сцены — плоский разговор; эпизода вовсе нет — пропавший день."""
    client = ScriptedClient(json.dumps({"episodes": [{"index": 0, "opening": "Γεια"}]}))
    result = await make_frames(
        client,
        persona=persona,
        level=Level.A1,
        profile=Profile(),
        requests=[
            EpisodeRequest(intent=EpisodeIntent.D1, words=[NERO]),
            EpisodeRequest(intent=EpisodeIntent.D2, words=[KAFES]),
        ],
    )
    assert len(result.frames) == 2
    assert result.frames[1].opening == ""


async def test_no_requests_costs_nothing(persona: Any) -> None:
    client = ScriptedClient()
    result = await make_frames(
        client, persona=persona, level=Level.A1, profile=Profile(), requests=[]
    )
    assert result.frames == []
    assert client.calls == 0


def test_frame_json_keeps_opening_out() -> None:
    """`opening` живёт своей колонкой: его отправляют, а рамку — нет."""
    from siga.llm.frames import Frame

    frame = Frame(index=0, scene="кофе", goal="узнать", plan=["позвать"], opening="Γεια")
    assert "opening" not in frame.as_frame_json()


# --- R3: тема (FR-EP-9) -------------------------------------------------------


async def test_a_topic_request_asks_for_a_topic_not_for_words(persona: Any) -> None:
    """Тема уезжает в промпт словами, а не пустым списком слов."""
    client = ScriptedClient(
        json.dumps(
            {"episodes": [{"index": 0, "scene": "кухня", "opening": "Καλημέρα!"}]},
            ensure_ascii=False,
        )
    )
    result = await make_frames(
        client,
        persona=persona,
        level=Level.A1,
        profile=Profile(),
        requests=[EpisodeRequest(intent=EpisodeIntent.TOPIC, topic="готовим ужин")],
    )
    assert result.frames[0].scene == "кухня"

    prompt = "\n".join(message.text or "" for message in client.asked[0])
    assert "Тема разговора: готовим ужин" in prompt
    assert "Темы, на которые нужны разговоры" in prompt, "заголовок честный: слов нет"
    assert "Слова, вокруг которых" not in prompt
    assert "тип T" in prompt


def test_frames_task_explains_the_topic_intent() -> None:
    assert "T — разговор по теме" in FRAMES_TASK


# --- R4: ход ------------------------------------------------------------------


def test_turn_task_demands_recast_without_grammar_lecture() -> None:
    assert "екаст" in TURN_TASK
    assert "Не объясняй грамматику" in TURN_TASK
    assert "двух исправлений" in TURN_TASK, "FR-CHK-5"


def test_turn_task_tells_the_model_that_topic_talks_have_no_words() -> None:
    """Без явной оговорки модель ищет целевые слова в пустом списке."""
    assert "разговор без целевых слов" in TURN_TASK


def test_history_reads_as_a_conversation() -> None:
    block = history_block([Exchange("Καλημέρα!", "Γεια"), Exchange("Τι πίνεις;")])
    assert block.splitlines() == ["Ты: Καλημέρα!", "Ученик: Γεια", "Ты: Τι πίνεις;"]


def test_frame_without_fields_does_not_break_the_turn() -> None:
    assert "по обстановке" in frame_block({})
    assert "по обстановке" in frame_block({"scene": "   "})


def test_last_turn_is_announced(persona: Any) -> None:
    """Модель должна знать, что ход последний, иначе оборвётся на вопросе."""
    variable = build_messages(persona=persona, context=context(turns_left=1))[1].text
    assert "последний ход" in variable


def test_user_answer_comes_last(persona: Any) -> None:
    messages = build_messages(persona=persona, context=context())
    assert messages[-1].text.endswith("Πίνω νερώ κάθε πρωί.")


async def test_turn_parses_assessment(persona: Any) -> None:
    client = ScriptedClient(turn_answer())
    result = await make_turn(client, persona=persona, context=context())

    assessment = result.answer.word_assessments[0]
    assert assessment.word_id == 412
    assert assessment.verdict is Verdict.ALMOST
    assert assessment.error_type is ErrorType.CASE
    assert result.answer.wants_close is False
    assert result.answer.memory_updates.facts == ["пьёт воду по утрам"]


async def test_unused_words_are_recomputed(persona: Any) -> None:
    """Список неупотреблённых слов считаем сами: модель про него забывает."""
    client = ScriptedClient(turn_answer(unused_target_words=[]))
    result = await make_turn(client, persona=persona, context=context())
    assert result.answer.unused_target_words == [KAFES.word_id]


async def test_assessment_of_a_foreign_word_is_dropped(persona: Any) -> None:
    """Иначе в прогресс уехало бы слово, которого человек не проходил."""
    client = ScriptedClient(
        turn_answer(
            word_assessments=[
                {"word_id": 999, "verdict": "correct"},
                {"word_id": 412, "verdict": "correct"},
            ]
        )
    )
    result = await make_turn(client, persona=persona, context=context())
    assert [item.word_id for item in result.answer.word_assessments] == [412]


async def test_duplicate_assessment_keeps_the_first(persona: Any) -> None:
    client = ScriptedClient(
        turn_answer(
            word_assessments=[
                {"word_id": 412, "verdict": "correct"},
                {"word_id": 412, "verdict": "incorrect"},
            ]
        )
    )
    result = await make_turn(client, persona=persona, context=context())
    assert len(result.answer.word_assessments) == 1
    assert result.answer.word_assessments[0].verdict is Verdict.CORRECT


async def test_close_is_understood(persona: Any) -> None:
    client = ScriptedClient(turn_answer(episode_action="close"))
    result = await make_turn(client, persona=persona, context=context(turns_left=1))
    assert result.answer.wants_close is True


def test_counts_as_correct_is_not_the_verdict() -> None:
    """FR-CHK-1: правило зачёта живёт в коде (§5.7), а не в ответе модели."""
    answer = TurnResponse.model_validate(
        json.loads(turn_answer(word_assessments=[{"word_id": 412, "verdict": "almost"}]))
    )
    assessment = answer.word_assessments[0]
    assert assessment.counts_as_correct is None
    assert assessment.verdict is Verdict.ALMOST


def test_corrections_skip_the_correct_ones() -> None:
    answer = TurnResponse.model_validate(json.loads(turn_answer()))
    corrections = answer.corrections()
    assert len(corrections) == 1
    assert corrections[0]["correct_form"] == "νερό"

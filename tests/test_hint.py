"""Маршрут R7: чем ответить на вопрос Ника. Сеть не задействована.

Главное здесь — не формат, а обещание: подсказка говорит, что сказать, и не
даёт готовой греческой фразы. Проверить его тестом целиком нельзя (текст
пишет модель), поэтому проверяется то, что в нашей власти: промпт это
запрещает, а придуманные номера слов до человека не доходят.
"""

from __future__ import annotations

import json

import pytest

from siga.core.enums import Level
from siga.llm.base import Route
from siga.llm.dialogue import WordBrief
from siga.llm.hint import (
    MAX_OPTIONS,
    MAX_OUTPUT,
    SYSTEM_PROMPT,
    build_messages,
    suggest,
)
from tests.fake_llm import ScriptedClient

QUESTION = "Καλημέρα! Τι πίνεις το πρωί;"

WORDS = [
    WordBrief(word_id=412, lemma="ο καφές", translation_ru="кофе", box=0),
    WordBrief(word_id=415, lemma="το νερό", translation_ru="вода", box=2),
]


def answer(*options: dict[str, object]) -> str:
    return json.dumps({"options": list(options)}, ensure_ascii=False)


def option(ru: str = "Скажи, что пьёшь кофе", ids: list[int] | None = None) -> dict[str, object]:
    return {"ru": ru, "word_ids": [412] if ids is None else ids}


# --- промпт -------------------------------------------------------------------


def test_the_prompt_says_the_word_json() -> None:
    """Требование провайдера, §8.3: без него `json_object` не включается."""
    assert "JSON" in SYSTEM_PROMPT


def test_the_prompt_forbids_greek_in_the_answer() -> None:
    """Суть фичи: готовую фразу ученик спишет, и слово останется невыученным."""
    assert "Греческих слов и фраз в ответе не пиши" in SYSTEM_PROMPT


def test_the_prompt_forbids_inventing_facts_about_the_learner() -> None:
    """FR-MEM-2: то, чего человек не говорил, ему и подсказывать нельзя."""
    assert "Не выдумывай факты о человеке" in SYSTEM_PROMPT


def test_the_question_is_marked_and_the_words_are_listed() -> None:
    variable = build_messages(
        question=QUESTION,
        words=WORDS,
        level=Level.A1,
        scene="утро, кухня",
        user_text="Καλημέρα.",
    )[1].text

    assert variable.endswith(QUESTION)
    assert "не знает, что ответить" in variable
    assert "[412] ο καφές — кофе" in variable
    assert "утро, кухня" in variable
    assert "Καλημέρα." in variable
    assert "A1" in variable


def test_static_block_holds_nothing_variable() -> None:
    """§8.4: уровень или сцена в системном блоке обнуляли бы кэш префикса."""
    first = build_messages(question=QUESTION, words=WORDS, level=Level.A1, scene="кухня")
    second = build_messages(question="Τι κάνεις;", words=WORDS[:1], level=Level.B1, scene="автобус")

    assert first[0].text == second[0].text
    assert QUESTION not in first[0].text
    assert "кухня" not in first[0].text
    assert "A1" not in first[0].text


def test_context_is_optional() -> None:
    """Под вводной репликой ни сцены, ни предыдущего ответа может не быть."""
    variable = build_messages(question=QUESTION, words=WORDS, level=Level.A1)[1].text

    assert "Дело происходит так" not in variable
    assert "Перед этим ученик сказал" not in variable


# --- вызов --------------------------------------------------------------------


async def test_the_call_goes_by_its_own_route() -> None:
    """Своя строка в §8.1 — значит, и своя модель с параметрами в настройках."""
    client = ScriptedClient(answer(option()))

    await suggest(client, question=QUESTION, words=WORDS, level=Level.A1)

    assert client.routes == [Route.HINT]
    assert client.json_modes == [True]
    assert client.max_output == [MAX_OUTPUT], "§8.2: потолок выхода задаём всегда"


async def test_options_come_back_stripped() -> None:
    client = ScriptedClient(answer(option(ru="  Скажи, что пьёшь кофе  ")))

    result = await suggest(client, question=QUESTION, words=WORDS, level=Level.A1)

    assert [item.ru for item in result.options] == ["Скажи, что пьёшь кофе"]
    assert result.usage.output_tokens > 0


async def test_a_word_that_was_not_offered_is_dropped() -> None:
    """Номер придумала модель, а слово по нему рисуется из базы.

    Оставить чужой номер значит показать вариант с пустым местом там, где
    ученик ждёт слово.
    """
    client = ScriptedClient(answer(option(ids=[412, 999])))

    result = await suggest(client, question=QUESTION, words=WORDS, level=Level.A1)

    assert result.options[0].word_ids == (412,)


async def test_a_repeated_word_is_listed_once() -> None:
    client = ScriptedClient(answer(option(ids=[412, 412])))

    result = await suggest(client, question=QUESTION, words=WORDS, level=Level.A1)

    assert result.options[0].word_ids == (412,)


async def test_an_option_without_words_survives() -> None:
    """«Что сказать» полезно и без слова — выбрасывать его было бы обиднее."""
    client = ScriptedClient(answer(option(ids=[])))

    result = await suggest(client, question=QUESTION, words=WORDS, level=Level.A1)

    assert len(result.options) == 1
    assert result.options[0].word_ids == ()


async def test_a_flood_of_options_is_cut() -> None:
    """Больше трёх — уже упражнение на чтение, а человек хотел говорить."""
    client = ScriptedClient(answer(*[option(ru=f"Вариант {n}") for n in range(10)]))

    result = await suggest(client, question=QUESTION, words=WORDS, level=Level.A1)

    assert len(result.options) == MAX_OPTIONS


@pytest.mark.parametrize("bad", [[], [{"ru": "  ", "word_ids": [412]}]])
async def test_nothing_usable_stays_empty(bad: list[dict[str, object]]) -> None:
    """Пустое в `hint_ru` писать нельзя: `NULL` значит «не спрашивали»."""
    client = ScriptedClient(json.dumps({"options": bad}, ensure_ascii=False))

    result = await suggest(client, question=QUESTION, words=WORDS, level=Level.A1)

    assert result.options == ()


async def test_without_words_there_is_nothing_to_hint_at() -> None:
    """Слова удалили вместе с пачкой — звать модель незачем."""
    client = ScriptedClient()

    result = await suggest(client, question=QUESTION, words=[], level=Level.A1)

    assert result.options == ()
    assert client.calls == 0


async def test_an_empty_question_costs_nothing() -> None:
    client = ScriptedClient()

    result = await suggest(client, question="   ", words=WORDS, level=Level.A1)

    assert result.options == ()
    assert client.calls == 0

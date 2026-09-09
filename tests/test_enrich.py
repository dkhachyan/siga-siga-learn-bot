"""Маршрут R2: промпт, сопоставление ответа со списком, партии, отказы.

Сеть не задействована: клиент отдаёт заготовленные ответы. Проверяется то,
что мы делаем с ответом модели, — включая случаи, когда она отвечает не то,
что просили.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from siga.core.enums import Gender, Level, PartOfSpeech
from siga.llm.base import LlmBadOutput, LlmUnavailable
from siga.llm.enrich import (
    ENRICH_BATCH,
    MAX_EXAMPLES,
    SYSTEM_PROMPT,
    build_messages,
    enrich,
)
from tests.fake_llm import ScriptedClient


def word(
    lemma: str,
    *,
    pos: str = "noun",
    article: str | None = "το",
    gender: str | None = "n",
    verb_form: str | None = None,
    examples: int = 1,
    translation: str = "вода",
) -> dict[str, Any]:
    return {
        "lemma": lemma,
        "lemma_accented": f"{article} {lemma}" if article else lemma,
        "translation_ru": translation,
        "pos": pos,
        "article": article,
        "gender": gender,
        "verb_form": verb_form,
        "examples": [{"el": f"Θέλω {lemma}.", "ru": "Хочу."} for _ in range(examples)],
    }


def answer(*words: dict[str, Any]) -> str:
    return json.dumps({"words": list(words)}, ensure_ascii=False)


# --- промпт ------------------------------------------------------------------


def test_system_prompt_holds_nothing_variable() -> None:
    """§8.4: любое изменчивое в системном блоке убивает кэш префикса."""
    for level in Level:
        assert level.value not in SYSTEM_PROMPT
    assert "νερό" in SYSTEM_PROMPT, "пример формата — статичен, он-то и нужен"


def test_system_prompt_says_json_because_deepseek_requires_it() -> None:
    assert "JSON" in SYSTEM_PROMPT


def test_user_message_carries_the_level_and_the_words() -> None:
    messages = build_messages(["το νερό", "ο καφές"], Level.A2)

    assert messages[0].role == "system"
    assert messages[0].text == SYSTEM_PROMPT

    ask = messages[1]
    assert ask.role == "user"
    assert "A2" in ask.text
    assert "το νερό" in ask.text
    assert "ο καφές" in ask.text


# --- сопоставление -----------------------------------------------------------


async def test_enrich_returns_grammar_by_lemma_key() -> None:
    client = ScriptedClient(answer(word("νερό")))
    result = await enrich(client, lemmas=["το νερό"], level=Level.A1)

    assert result.missing == ()
    assert result.enriched_count == 1
    got = result.words["νερο"]
    assert got.lemma_accented == "το νερό"
    assert got.pos is PartOfSpeech.NOUN
    assert got.gender is Gender.NEUTER
    assert got.article == "το"
    assert result.usage.total_tokens == 15


async def test_enrich_matches_despite_case_and_accents() -> None:
    """Модель вернула «ΝΕΡΟ» на «το νερό» — это то же слово (§ключ леммы)."""
    client = ScriptedClient(answer(word("ΝΕΡΟ")))
    result = await enrich(client, lemmas=["το νερό"], level=Level.A1)

    assert result.missing == ()
    assert "νερο" in result.words


async def test_enrich_reports_a_word_the_model_skipped() -> None:
    client = ScriptedClient(answer(word("νερό")))
    result = await enrich(client, lemmas=["το νερό", "ο καφές"], level=Level.A1)

    assert result.missing == ("ο καφές",)
    assert result.enriched_count == 1


async def test_enrich_drops_a_word_nobody_asked_for() -> None:
    """Иначе в пачке человека появится слово, которого он не загружал."""
    client = ScriptedClient(answer(word("νερό"), word("κρασί", translation="вино")))
    result = await enrich(client, lemmas=["το νερό"], level=Level.A1)

    assert list(result.words) == ["νερο"]


# --- чистка лишних полей -----------------------------------------------------


async def test_enrich_strips_gender_from_a_non_noun() -> None:
    client = ScriptedClient(answer(word("ωραίος", pos="adjective", article=None, gender="m")))
    result = await enrich(client, lemmas=["ωραίος"], level=Level.A1)

    got = result.words["ωραιοσ"]  # конечная сигма складывается в σ
    assert got.gender is None, "род бывает только у существительного"
    assert got.article is None


async def test_enrich_strips_a_verb_form_from_a_noun() -> None:
    client = ScriptedClient(answer(word("νερό", verb_form="πίνω")))
    result = await enrich(client, lemmas=["το νερό"], level=Level.A1)

    assert result.words["νερο"].verb_form is None


async def test_enrich_keeps_the_verb_form_of_a_verb() -> None:
    client = ScriptedClient(
        answer(
            word(
                "πίνω", pos="verb", article=None, gender=None, verb_form="πίνω", translation="пить"
            )
        )
    )
    result = await enrich(client, lemmas=["πίνω"], level=Level.A1)

    got = result.words["πινω"]
    assert got.pos is PartOfSpeech.VERB
    assert got.verb_form == "πίνω"


async def test_enrich_trims_a_flood_of_examples() -> None:
    client = ScriptedClient(answer(word("νερό", examples=5)))
    result = await enrich(client, lemmas=["το νερό"], level=Level.A1)

    assert len(result.words["νερο"].examples) == MAX_EXAMPLES


# --- партии и отказы ---------------------------------------------------------


async def test_enrich_splits_a_long_list_into_batches() -> None:
    """Сотня слов одним вызовом — это обрезанный ответ и потерянная пачка."""
    lemmas = [f"λέξη{n}" for n in range(ENRICH_BATCH + 5)]
    client = ScriptedClient(
        answer(*(word(lemma, article=None, gender=None) for lemma in lemmas[:ENRICH_BATCH])),
        answer(*(word(lemma, article=None, gender=None) for lemma in lemmas[ENRICH_BATCH:])),
    )

    result = await enrich(client, lemmas=lemmas, level=Level.A1)

    assert client.calls == 2
    assert result.missing == ()
    assert result.enriched_count == len(lemmas)


async def test_enrich_keeps_what_worked_when_one_batch_fails() -> None:
    lemmas = [f"λέξη{n}" for n in range(ENRICH_BATCH + 2)]
    client = ScriptedClient(
        LlmUnavailable("провайдер моргнул"),
        answer(*(word(lemma, article=None, gender=None) for lemma in lemmas[ENRICH_BATCH:])),
    )

    result = await enrich(client, lemmas=lemmas, level=Level.A1)

    assert result.enriched_count == 2, "вторая партия прошла"
    assert len(result.missing) == ENRICH_BATCH


async def test_enrich_raises_when_nothing_worked_at_all() -> None:
    """Пустой результат из-за аварии нельзя выдавать за «обогатил ноль слов»."""
    client = ScriptedClient(LlmUnavailable("нет ключа"))
    with pytest.raises(LlmUnavailable):
        await enrich(client, lemmas=["το νερό"], level=Level.A1)


async def test_enrich_survives_a_batch_the_model_answered_with_junk() -> None:
    """Битый JSON — та же частичная неудача, а не падение всей пачки."""
    lemmas = [f"λέξη{n}" for n in range(ENRICH_BATCH + 1)]
    client = ScriptedClient(
        "это не json",
        "и это тоже не json",  # починочная попытка
        answer(*(word(lemma, article=None, gender=None) for lemma in lemmas[ENRICH_BATCH:])),
    )

    result = await enrich(client, lemmas=lemmas, level=Level.A1)

    assert result.enriched_count == 1
    assert len(result.missing) == ENRICH_BATCH


async def test_enrich_raises_bad_output_when_every_batch_is_junk() -> None:
    client = ScriptedClient("мусор", "снова мусор")
    with pytest.raises(LlmBadOutput):
        await enrich(client, lemmas=["το νερό"], level=Level.A1)

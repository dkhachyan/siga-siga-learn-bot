"""Маршрут R6: перевод реплики Ника на русский.

Сеть не задействована. Проверяется промпт — контекст в него попадает, а
статика остаётся статикой (§8.4) — и то, что делается с ответом: пустой
перевод не должен уехать в кэш, иначе кнопка навсегда станет показывать
пустое окно.
"""

from __future__ import annotations

import json

import pytest

from siga.llm.base import Route
from siga.llm.translate import MAX_OUTPUT, SYSTEM_PROMPT, build_messages, translate
from tests.fake_llm import ScriptedClient

REPLY = "Καλημέρα! Πάω για καφέ. Εσύ τι πίνεις το πρωί;"
RUSSIAN = "Доброе утро! Я иду за кофе. А ты что пьёшь утром?"


def answer(ru: str = RUSSIAN) -> str:
    return json.dumps({"ru": ru}, ensure_ascii=False)


# --- промпт -------------------------------------------------------------------


def test_the_prompt_says_the_word_json() -> None:
    """Требование провайдера, §8.3: без него `json_object` не включается."""
    assert "JSON" in SYSTEM_PROMPT


def test_the_prompt_forbids_explanations() -> None:
    """Разбор — это `🔍 Разбор`. Кнопка перевода переводит, и только."""
    assert "Ничего не объясняй" in SYSTEM_PROMPT


def test_the_line_to_translate_is_marked() -> None:
    """Контекст в промпте есть, и модель должна понимать, что он не для перевода."""
    variable = build_messages(text=REPLY, scene="Ник зовёт выпить кофе", user_text="Πίνω νερό.")[
        1
    ].text

    assert variable.endswith(REPLY)
    assert "Перевести:" in variable
    assert "Ник зовёт выпить кофе" in variable
    assert "Πίνω νερό." in variable


def test_static_block_holds_nothing_variable() -> None:
    """§8.4: сцена и реплика в системном блоке обнуляли бы кэш префикса."""
    first = build_messages(text=REPLY, scene="пекарня")
    second = build_messages(text="Τι κάνεις;", scene="автобус", user_text="Καλά.")

    assert first[0].text == second[0].text
    assert REPLY not in first[0].text
    assert "пекарня" not in first[0].text


def test_context_is_optional() -> None:
    """Под вводной репликой предыдущего ответа ученика ещё нет."""
    variable = build_messages(text=REPLY)[1].text
    assert variable == f"Перевести:\n{REPLY}"


# --- вызов --------------------------------------------------------------------


async def test_translation_comes_back_stripped() -> None:
    client = ScriptedClient(answer(f"  {RUSSIAN}  "))

    result = await translate(client, text=REPLY)

    assert result.ru == RUSSIAN
    assert result.usage.output_tokens > 0


async def test_the_call_goes_by_its_own_route() -> None:
    """Своя строка в §8.1 — значит, и своя модель с параметрами в настройках."""
    client = ScriptedClient(answer())

    await translate(client, text=REPLY)

    assert client.routes == [Route.TRANSLATE]
    assert client.json_modes == [True]
    assert client.max_output == [MAX_OUTPUT], "§8.2: потолок выхода задаём всегда"


@pytest.mark.parametrize("ru", ["", "   "])
async def test_an_empty_translation_stays_empty(ru: str) -> None:
    """Пустое в `bot_text_ru` писать нельзя: `NULL` значит «ещё не переводили»."""
    client = ScriptedClient(answer(ru))

    result = await translate(client, text=REPLY)

    assert result.ru == ""


async def test_an_empty_reply_costs_nothing() -> None:
    """Переводить нечего — и спрашивать не о чем."""
    client = ScriptedClient()

    result = await translate(client, text="   ")

    assert result.ru == ""
    assert client.calls == 0

"""Просмотр пачки после подтверждения: список и листание карточек.

Апдейты идут через настоящий Dispatcher — как в `test_import_flow`. Здесь
важно то, что человек видит после «Всё верно»: что пачка не исчезла, что
грамматика доехала до экрана и что листание не заводит в тупик.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_asyncio

from siga.llm.base import LlmClient
from tests.fake_llm import ScriptedClient
from tests.fake_telegram import FakeTelegram
from tests.test_import_flow import ENRICHED_THREE, THREE_WORDS


@pytest_asyncio.fixture
async def active(telegram: FakeTelegram, use_llm: Callable[[LlmClient], None]) -> FakeTelegram:
    """Бот с одной активной обогащённой пачкой из трёх слов."""
    use_llm(ScriptedClient(ENRICHED_THREE))
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)
    await telegram.press("✅")
    return telegram


async def test_confirmed_pack_offers_the_cards(active: FakeTelegram) -> None:
    assert active.find_button("🃏") is not None, "после подтверждения карточки под рукой"


async def test_pack_shows_the_active_list(active: FakeTelegram) -> None:
    answers = await active.send("/pack")

    screen = answers[-1]
    assert "1. <b>το νερό</b> — вода" in screen
    assert "3. <b>το γάλα</b> — молоко" in screen
    assert "в работе" in screen


async def test_cards_show_the_grammar(active: FakeTelegram) -> None:
    answers = await active.press("🃏")

    card = answers[-1]
    assert "το νερό" in card
    assert "1 из 3" in card
    assert "существительное, средний род" in card
    assert "Θέλω νερό." in card, "пример из обогащения доезжает до карточки"


async def test_next_flips_in_place(active: FakeTelegram) -> None:
    """Листание правит то же сообщение, а не заваливает переписку новыми."""
    await active.press("🃏")
    sent = await active.press("→")

    assert sent == [], "новых сообщений при листании быть не должно"
    assert "2 из 3" in active.edits[-1]
    assert "ο καφές" in active.edits[-1]


async def test_previous_from_the_first_card_wraps_to_the_last(active: FakeTelegram) -> None:
    """Кольцо, а не тупик: стрелка, которая ничего не делает, читается поломкой."""
    await active.press("🃏")
    await active.press("←")

    assert "3 из 3" in active.edits[-1]


async def test_list_button_returns_the_whole_pack(active: FakeTelegram) -> None:
    await active.press("🃏")
    answers = await active.press("☰")

    assert "1. <b>το νερό</b>" in answers[-1]
    assert "3. <b>το γάλα</b>" in answers[-1]


async def test_pack_without_any_pack_says_so(telegram: FakeTelegram) -> None:
    answers = await telegram.send("/pack")
    assert "нет" in answers[-1].lower()


async def test_cards_of_a_pack_that_lost_its_words(active: FakeTelegram) -> None:
    """Кнопка помнит номер, которого в пачке уже нет — берём по модулю."""
    answers = await active.click("card:open:99")
    assert "1 из 3" in answers[-1]


@pytest.mark.parametrize("data", ["card:show:0", "card:list:0"])
async def test_cards_without_a_pack_complain_quietly(telegram: FakeTelegram, data: str) -> None:
    """Кнопка из старого сообщения после удаления пачки не должна ломать бот."""
    answers = await telegram.click(data)
    assert answers == [], "отказ уезжает всплывающим окном, а не сообщением"

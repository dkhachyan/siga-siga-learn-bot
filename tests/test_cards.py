"""Просмотр пачки после подтверждения: список, карточки и уборка слова.

Апдейты идут через настоящий Dispatcher — как в `test_import_flow`. Здесь
важно то, что человек видит после «Всё верно»: что пачка не исчезла, что
грамматика доехала до экрана, что листание не заводит в тупик и что слово
убирается с переспросом, а не одним случайным нажатием.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from siga.db.models import Word, WordProgress
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


# --- убрать слово (FR-IMP-11) --------------------------------------------------


@pytest.fixture
def db(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Сессия, чтобы заглянуть в базу мимо бота."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def _words(db: async_sessionmaker[AsyncSession]) -> list[Word]:
    async with db() as session:
        return list(await session.scalars(select(Word).order_by(Word.id)))


async def _progress(db: async_sessionmaker[AsyncSession]) -> list[WordProgress]:
    async with db() as session:
        return list(await session.scalars(select(WordProgress)))


async def test_the_active_list_offers_removal(active: FakeTelegram) -> None:
    await active.send("/pack")
    assert active.find_button("🗑") is not None, "убрать слово — с экрана списка"


async def test_removal_asks_which_word(active: FakeTelegram) -> None:
    await active.send("/pack")
    answers = await active.press("🗑")

    assert answers[-1] == "Какое слово убрать?"
    assert active.find_button("1") is not None
    assert active.find_button("3") is not None
    assert active.find_button("← Назад") is not None, "обратно ведёт к свежему списку"


async def test_removal_warns_before_it_burns_progress(
    active: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    """Переспрос обязателен: удаление необратимо, и жмут его по номеру."""
    await active.send("/pack")
    await active.press("🗑")
    await active.press("1")

    screen = active.edits[-1]
    assert "το νερό" in screen
    assert "вода" in screen
    assert "Прогресс" in screen, "о том, что прогресс сгорит, сказано прямо"

    assert len(await _words(db)) == 3, "выбор номера ещё ничего не удаляет"


async def test_cancellation_returns_to_the_picker(active: FakeTelegram) -> None:
    await active.send("/pack")
    await active.press("🗑")
    await active.press("1")
    await active.press("↩︎")

    assert "Какое слово убрать?" in active.edits[-1]
    assert active.find_button("3") is not None, "перезапрошенный выбор на месте"


async def test_confirming_removes_the_word_and_its_progress(
    active: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await active.send("/pack")
    await active.press("🗑")
    await active.press("1")
    answers = await active.press("🗑 Убрать")

    words = await _words(db)
    assert [word.lemma for word in words] == ["καφεσ", "γαλα"], "νερό ушло"
    assert len(await _progress(db)) == 2, "прогресс слова умер вместе с ним"

    # Свежий список: перенумерован, и убранного в нём нет.
    assert "1. <b>ο καφές</b>" in answers[-1]
    assert "νερό" not in answers[-1]
    assert active.find_button("🗑") is not None, "убрать можно ещё раз"


async def test_a_stale_confirm_button_removes_nothing_twice(
    active: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    """Кнопки живут в переписке вечно — и второе нажатие не падает."""
    await active.send("/pack")
    await active.press("🗑")
    await active.press("1")
    await active.press("🗑 Убрать")
    again = await active.click("word:drop_yes:1")

    assert again == [], "ничего нового: слово уже убрано"
    assert active.last_alert is not None and active.last_alert.text, "отказ окном"
    assert len(await _words(db)) == 2


async def test_a_forged_word_id_is_refused(
    active: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    """Данные кнопок подделываются: чужой номер не должен ничего удалить."""
    await active.send("/pack")
    await active.press("🗑")

    answers = await active.click("word:drop:99999")
    assert answers == []
    assert active.last_alert is not None and active.last_alert.text
    assert len(await _words(db)) == 3

    answers = await active.click("word:drop_yes:99999")
    assert answers == []
    assert active.last_alert is not None and active.last_alert.text
    assert len(await _words(db)) == 3


async def test_a_stranger_sees_nothing_of_someone_elses_pack(
    active: FakeTelegram,
) -> None:
    """Чужая кнопка показывает нажавшему его собственную (пустую) пачку."""
    answers = await active.click("pack:drop", from_user_id=502)

    assert answers == []
    assert active.last_alert is not None and "Активной пачки" in str(active.last_alert.text)


async def test_the_last_word_can_go_too(active: FakeTelegram) -> None:
    """Опустевшая пачка — не запрет, а честное «дальше говорить не о чем»."""
    await active.send("/pack")
    answers: list[str] = []
    for _ in range(3):
        await active.press("🗑")
        await active.press("1")
        answers = await active.press("🗑 Убрать")

    assert "не осталось слов" in answers[-1]

"""Сценарий импорта целиком: от /add до подтверждённой пачки.

Апдейты идут через настоящий Dispatcher — с фильтрами, состояниями и
мидлварью сессии. Проверяется то, что человек видит и нажимает, а не то,
как внутри устроены хендлеры.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from siga.core.enums import PackStatus
from siga.db.models import Pack, Word, WordProgress
from tests.fake_telegram import FakeTelegram

THREE_WORDS = "το νερό — вода\nο καφές — кофе\nτο γάλα — молоко"


@pytest.fixture
def db(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Сессия, чтобы заглянуть в базу мимо бота."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def _packs(db: async_sessionmaker[AsyncSession]) -> list[Pack]:
    async with db() as session:
        return list(await session.scalars(select(Pack)))


async def _words(db: async_sessionmaker[AsyncSession]) -> list[Word]:
    async with db() as session:
        return list(await session.scalars(select(Word).order_by(Word.id)))


async def test_add_asks_for_a_list(telegram: FakeTelegram) -> None:
    answers = await telegram.send("/add")
    assert len(answers) == 1
    assert answers[0]


async def test_a_list_becomes_a_draft_with_the_four_buttons(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await telegram.send("/add")
    answers = await telegram.send(THREE_WORDS)

    screen = answers[-1]
    assert "1. <b>το νερό</b> — вода" in screen
    assert "3. <b>το γάλα</b> — молоко" in screen

    # FR-IMP-8: четыре кнопки, ни одной меньше
    labels = [
        button.text
        for row in (telegram.last_keyboard.inline_keyboard if telegram.last_keyboard else [])
        for button in row
    ]
    assert len(labels) == 4
    assert any(label.startswith("✅") for label in labels)
    assert any(label.startswith("✏️") for label in labels)
    assert any(label.startswith("🗑") for label in labels)
    assert any(label.startswith("➕") for label in labels)

    packs = await _packs(db)
    assert len(packs) == 1
    assert packs[0].status == PackStatus.DRAFT


async def test_text_without_add_does_not_start_an_import(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    """Список слов «в холодную» не должен молча заводить пачку."""
    await telegram.send(THREE_WORDS)
    assert await _packs(db) == []


async def test_unparsed_lines_are_reported_not_swallowed(telegram: FakeTelegram) -> None:
    await telegram.send("/add")
    answers = await telegram.send("το νερό — вода\nпросто по-русски\nο καφές — кофе")

    report = "\n".join(answers)
    assert "просто по-русски" in report, "молча терять строку нельзя"
    assert "1. <b>το νερό</b>" in answers[-1]
    assert "2. <b>ο καφές</b>" in answers[-1]


async def test_delete_removes_the_word_from_the_screen(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)

    await telegram.press("🗑")
    words = await _words(db)
    answers = await telegram.click(f"word:delete:{words[1].id}")

    screen = answers[-1]
    assert "ο καφές" not in screen
    assert "1. <b>το νερό</b>" in screen
    assert "2. <b>το γάλα</b>" in screen, "нумерация пересчитывается"
    assert len(await _words(db)) == 2


async def test_edit_replaces_the_translation(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)

    await telegram.press("✏️")
    words = await _words(db)
    await telegram.click(f"word:edit:{words[0].id}")
    answers = await telegram.send("питьевая вода")

    assert "1. <b>το νερό</b> — питьевая вода" in answers[-1]


async def test_add_one_word_appends_it(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)

    await telegram.press("➕")
    answers = await telegram.send("ο ήλιος — солнце")

    assert "4. <b>ο ήλιος</b> — солнце" in answers[-1]
    assert len(await _words(db)) == 4


async def test_add_one_word_refuses_a_duplicate(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)

    await telegram.press("➕")
    answers = await telegram.send("ΝΕΡΟ — вода")

    assert "νερό" in answers[-1].lower() or "νερο" in answers[-1].lower()
    assert len(await _words(db)) == 3, "дубль по ключу не добавляется"


async def test_back_returns_to_the_confirmation_screen(telegram: FakeTelegram) -> None:
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)

    await telegram.press("🗑")
    answers = await telegram.press("← Назад")

    assert "1. <b>το νερό</b>" in answers[-1]


async def test_confirm_activates_the_pack(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)
    await telegram.press("✅")

    packs = await _packs(db)
    assert len(packs) == 1
    assert packs[0].status == PackStatus.ACTIVE
    assert packs[0].started_at is not None

    async with db() as session:
        progress = list(await session.scalars(select(WordProgress)))
    assert len(progress) == 3


async def test_pack_survives_a_restart(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    """FSM в памяти, черновик — в базе: /pack возвращает к брошенному импорту."""
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)

    telegram.forget_state()

    answers = await telegram.send("/pack")
    assert "1. <b>το νερό</b> — вода" in answers[-1]

    await telegram.press("✅")
    packs = await _packs(db)
    assert packs[0].status == PackStatus.ACTIVE


async def test_cancel_leaves_the_flow(telegram: FakeTelegram) -> None:
    await telegram.send("/add")
    await telegram.send("/cancel")

    # После отмены список слов — уже не список слов, а просто текст
    answers = await telegram.send(THREE_WORDS)
    assert "1. <b>το νερό</b>" not in "\n".join(answers)


async def test_photo_gets_an_honest_answer(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    """Фото — этап 2. Молчать в ответ нельзя, и пачку заводить тоже."""
    await telegram.send("/add")
    answers = await telegram.send_photo()

    assert answers, "бот не должен молча проглатывать фотографию"
    assert await _packs(db) == []

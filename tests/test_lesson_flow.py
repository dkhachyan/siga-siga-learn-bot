"""Пачка из уроков учебника (FR-IMP-12): от `/lesson` до активной пачки.

Как и импорт текстом, проверяется через настоящий Dispatcher с базой.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from siga import glossary
from siga.core.enums import PackStatus, WordSource
from siga.db.models import Import, Pack, Word, WordProgress
from tests.fake_telegram import FakeTelegram

THREE_WORDS = "το νερό — вода\nο καφές — кофе\nτο γάλα — молоко"


@pytest.fixture
def db(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _packs(db: async_sessionmaker[AsyncSession]) -> list[Pack]:
    async with db() as session:
        return list(await session.scalars(select(Pack).order_by(Pack.id)))


async def _words(db: async_sessionmaker[AsyncSession]) -> list[Word]:
    async with db() as session:
        return list(await session.scalars(select(Word).order_by(Word.id)))


async def test_lesson_without_a_number_shows_the_lesson_buttons(telegram: FakeTelegram) -> None:
    answers = await telegram.send("/lesson")

    assert "/lesson 1-4" in answers[-1]
    assert telegram.find_button("1") == "lsn:1:1"
    assert telegram.find_button("22") == "lsn:22:22"


async def test_one_lesson_becomes_a_draft_from_the_glossary(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    expected = glossary.load().words(1, 1)

    answers = await telegram.send("/lesson 1")

    assert f"1. <b>{expected[0].lemma}</b>" in "\n".join(answers)
    assert telegram.find_button("✅") is not None, "дальше — обычный экран подтверждения"

    [pack] = await _packs(db)
    assert pack.status == PackStatus.DRAFT
    assert pack.title == "Урок 1"

    words = await _words(db)
    assert len(words) == len(expected)
    assert {word.source for word in words} == {WordSource.GLOSSARY}
    assert words[0].translation_ru == expected[0].translation_ru

    async with db() as session:
        [record] = list(await session.scalars(select(Import)))
    assert record.source_type == WordSource.GLOSSARY


async def test_a_range_of_lessons_goes_into_one_pack(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await telegram.send("/lesson 2-3")

    [pack] = await _packs(db)
    assert pack.title == "Уроки 2–3"
    assert len(await _words(db)) == len(glossary.load().words(2, 3))


async def test_lesson_button_makes_the_same_draft(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await telegram.send("/lesson")
    await telegram.click("lsn:5:5")

    [pack] = await _packs(db)
    assert pack.title == "Урок 5"
    assert len(await _words(db)) == len(glossary.load().words(5, 5))


async def test_too_wide_a_range_is_refused_with_a_suggestion(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    answers = await telegram.send("/lesson 1-22")

    assert "/lesson 1-" in answers[-1]
    assert await _packs(db) == []


async def test_nonsense_range_is_explained(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    answers = await telegram.send("/lesson сорок")

    assert "/lesson 3" in answers[-1]
    assert await _packs(db) == []


async def test_lesson_works_even_while_add_waits_for_a_list(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    """Иначе импорт принял бы `/lesson 3` за список слов без греческого."""
    await telegram.send("/add")
    await telegram.send("/lesson 3")

    [pack] = await _packs(db)
    assert pack.title == "Урок 3"


async def test_confirm_activates_the_lesson_pack_and_archives_the_old_one(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    await telegram.send("/add")
    await telegram.send(THREE_WORDS)
    await telegram.press("✅")

    answers = await telegram.send("/lesson 1")
    assert any("уйдёт в архив" in answer for answer in answers), "предупреждаем заранее"

    await telegram.press("✅")

    old, new = await _packs(db)
    assert old.status == PackStatus.ARCHIVED
    assert new.status == PackStatus.ACTIVE
    async with db() as session:
        progress = list(
            await session.scalars(select(WordProgress).join(Word).where(Word.pack_id == new.id))
        )
    assert len(progress) == len(glossary.load().words(1, 1))


async def test_big_lesson_pack_pages_the_delete_numbers(
    telegram: FakeTelegram, db: async_sessionmaker[AsyncSession]
) -> None:
    """Сотня кнопок — предел Telegram; пачка из уроков его перерастает."""
    await telegram.send("/lesson 1-3")
    await telegram.press("🗑")

    assert telegram.last_keyboard is not None
    buttons = [b for row in telegram.last_keyboard.inline_keyboard for b in row]
    assert len(buttons) <= 100

    await telegram.press("51–")
    assert telegram.last_keyboard is not None
    texts = [b.text for row in telegram.last_keyboard.inline_keyboard for b in row]
    assert texts[0] == "51"

    words = await _words(db)
    await telegram.click(f"word:delete:{words[60].id}")
    assert len(await _words(db)) == len(words) - 1

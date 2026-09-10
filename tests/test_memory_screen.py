"""`/memory`: что человек видит и что может убрать.

Профиль сюда кладётся напрямую, без разговора: экран проверяется отдельно от
того, как память наполняется, — за наполнение отвечает `test_episode_flow`.
"""

from __future__ import annotations

import datetime as dt

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from siga.core.memory import Profile
from siga.db import memory as memory_db
from siga.db import users
from siga.db.models import DialogMemory
from tests.fake_telegram import TG_USER_ID, FakeTelegram

FILLED = Profile(
    facts=["живёт на Крите", "работает в IT", "есть дочь шести лет"],
    recurring_errors=["путает род существительных"],
    recent_topics=["кофе", "работа"],
    tone_notes="любит короткие реплики",
)


@pytest_asyncio.fixture
async def remembering(telegram: FakeTelegram, engine: AsyncEngine) -> FakeTelegram:
    """Бот, которому есть что рассказать о человеке."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user, _ = await users.get_or_create(session, tg_user_id=TG_USER_ID)
        await memory_db.save(session, user_id=user.id, profile=FILLED, now=dt.datetime.now(dt.UTC))
    return telegram


async def profile_now(engine: AsyncEngine) -> Profile:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        row = await session.scalar(select(DialogMemory))
    return Profile.from_dict(row.profile if row else None)


async def test_empty_memory_explains_itself(telegram: FakeTelegram) -> None:
    """Пустой экран — не «пусто», а объяснение, откуда память берётся."""
    answers = await telegram.send("/memory")

    assert "ничего не помню" in answers[-1]
    assert telegram.last_keyboard is None, "стирать нечего — и кнопки незачем"


async def test_memory_shows_everything_that_goes_to_the_model(
    remembering: FakeTelegram,
) -> None:
    screen = (await remembering.send("/memory"))[-1]

    assert "1. живёт на Крите" in screen
    assert "3. есть дочь шести лет" in screen
    assert "путает род существительных" in screen
    assert "кофе, работа" in screen
    assert "любит короткие реплики" in screen


async def test_a_wrong_fact_can_be_forgotten(
    remembering: FakeTelegram, engine: AsyncEngine
) -> None:
    """FR-MEM-4: неверный факт человек убирает сам, номером."""
    await remembering.send("/memory")

    await remembering.press("2")

    assert "работает в IT" not in remembering.edits[-1], "убранный факт с экрана пропал"
    assert "живёт на Крите" in remembering.edits[-1], "остальное на месте"

    profile = await profile_now(engine)
    assert profile.facts == ["живёт на Крите", "есть дочь шести лет"]


async def test_clearing_asks_first(remembering: FakeTelegram, engine: AsyncEngine) -> None:
    """Забвение необратимо, поэтому одного нажатия для него мало."""
    await remembering.send("/memory")

    await remembering.press("🧹")

    assert "Забыть всё" in remembering.edits[-1]
    assert (await profile_now(engine)).facts == FILLED.facts, "пока ничего не стёрли"


async def test_the_answer_can_be_no(remembering: FakeTelegram, engine: AsyncEngine) -> None:
    await remembering.send("/memory")
    await remembering.press("🧹 Забыть всё")

    await remembering.press("↩︎")

    assert "живёт на Крите" in remembering.edits[-1], "экран вернулся как был"
    assert (await profile_now(engine)).facts == FILLED.facts


async def test_confirmed_clearing_wipes_the_profile(
    remembering: FakeTelegram, engine: AsyncEngine
) -> None:
    await remembering.send("/memory")
    await remembering.press("🧹 Забыть всё")

    await remembering.press("🧹 Да")

    assert "забыл" in remembering.edits[-1]
    assert (await profile_now(engine)).is_empty()


async def test_memory_after_clearing_is_empty_again(
    remembering: FakeTelegram, engine: AsyncEngine
) -> None:
    """Строка в базе остаётся, но человеку она читается как чистый лист."""
    await remembering.send("/memory")
    await remembering.press("🧹 Забыть всё")
    await remembering.press("🧹 Да")

    answers = await remembering.send("/memory")

    assert "ничего не помню" in answers[-1]

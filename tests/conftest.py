"""Общие фикстуры.

Тесты, которым нужна настоящая база, просят фикстуру `session` (слой
хранения) или `telegram` (бот целиком). Без переменной `TEST_DATABASE_URL`
они пропускаются: `make check` должен работать на машине, где Postgres не
поднят, а в CI база есть.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio
from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from siga.bot.factory import create_dispatcher
from siga.db.base import Base
from tests.fake_telegram import FakeTelegram

SKIP_REASON = "нужна база: TEST_DATABASE_URL=postgresql+asyncpg://…"


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(SKIP_REASON)
    return url


async def _reset_schema(engine: AsyncEngine) -> None:
    """Пересоздать схему целиком.

    Функции слоя хранения сами делают `commit()`, поэтому «откатить
    транзакцию после теста» не сработает.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Свежий движок и чистая схема на каждый тест.

    Движок именно на тест, а не на прогон: pytest-asyncio даёт каждому тесту
    свой event loop, а соединения asyncpg привязаны к тому циклу, в котором
    открыты, — переиспользование даёт «another operation is in progress».
    """
    engine = create_async_engine(database_url)
    await _reset_schema(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session


class _CurrentEngineSessions:
    """Фабрика сессий, которая смотрит на движок текущего теста.

    Нужна, потому что диспетчер живёт весь прогон, а движок — один тест:
    подсунуть мидлвари `async_sessionmaker`, привязанный к движку, нельзя.
    """

    engine: AsyncEngine | None = None

    def __call__(self) -> AsyncSession:
        if self.engine is None:
            raise RuntimeError("движок не выставлен: фикстура telegram не отработала")
        return AsyncSession(self.engine, expire_on_commit=False)


@pytest.fixture(scope="session")
def _sessions() -> _CurrentEngineSessions:
    return _CurrentEngineSessions()


@pytest.fixture(scope="session")
def dispatcher(_sessions: _CurrentEngineSessions) -> Dispatcher:
    """Один Dispatcher на весь прогон.

    `create_dispatcher` в процессе можно позвать ровно один раз (см. её
    docstring), поэтому тесты делят диспетчер и чистят за собой FSM.
    """
    return create_dispatcher(cast("async_sessionmaker[AsyncSession]", _sessions))


@pytest_asyncio.fixture
async def telegram(
    engine: AsyncEngine,
    dispatcher: Dispatcher,
    _sessions: _CurrentEngineSessions,
) -> AsyncIterator[FakeTelegram]:
    """Бот целиком, но без сети: настоящие роутер, фильтры, FSM и база."""
    sessions = _sessions
    sessions.engine = engine
    dispatcher.fsm.storage = type(dispatcher.fsm.storage)()

    fake = FakeTelegram(dispatcher)
    yield fake

    await fake.bot.session.close()
    sessions.engine = None

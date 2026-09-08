"""Мидлвари aiogram."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    """Открывает сессию на апдейт и кладёт её в `data["session"]`.

    Одна сессия на апдейт, а не на приложение: иначе ошибка в одном хендлере
    оставляет транзакцию сломанной для всех последующих.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            return await handler(event, data)

"""Мидлвари aiogram."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.dispatcher.middlewares.user_context import EventContext
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from siga.bot import texts

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


class AllowlistMiddleware(BaseMiddleware):
    """Пускает только своих — список номеров Telegram из `ALLOWED_TG_USER_IDS`.

    Спрятать бота нельзя: имя `@SigaSigaLearnBot` публично, и написать ему
    может кто угодно. Пока запрета не было, незнакомец заводился в `users` и
    тратил ключ LLM — то есть дыра была не в приватности, а в кошельке.

    Внешняя мидлварь на `update`, а не фильтр в роутерах: до хендлеров и до
    сессии базы посторонний доходить не должен вовсе, иначе запрет пришлось бы
    повторять в каждом новом хендлере — и однажды забыть.

    `None` — гейта нет вовсе, проходят все: так собирается диспетчер в тестах,
    где Telegram поддельный и пускать некого. **Пустой список — это закрыто**,
    а не открыто: сначала было наоборот, ради удобства локального запуска, и
    ровно на этом бот в проде отработал открытым — `.env` там завели из
    `.env.example`, где строка пустая, и предупреждение в журнале никто не
    прочитал. Запертый снаружи хозяин исправляет это за минуту и сразу
    замечает; открытый бот не замечает никто.
    """

    def __init__(self, allowed: frozenset[int] | None) -> None:
        self._allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self._allowed is None:
            return await handler(event, data)

        context: EventContext | None = data.get("event_context")
        user_id = context.user_id if context is not None else None
        if user_id is not None and user_id in self._allowed:
            return await handler(event, data)

        log.warning("не пустил %s: не в ALLOWED_TG_USER_IDS", user_id)
        await self._refuse(event, data, context)
        return None

    async def _refuse(
        self, event: TelegramObject, data: dict[str, Any], context: EventContext | None
    ) -> None:
        """Сказать «закрыто» — молчание выглядело бы поломкой.

        Ошибку Telegram здесь глотаем: незнакомец мог и заблокировать бота, а
        падать на отказе посторонним — значит уронить обработку апдейта, до
        которой в этом случае и дела нет.
        """
        bot = data.get("bot")
        if not isinstance(bot, Bot) or not isinstance(event, Update):
            return

        with contextlib.suppress(TelegramAPIError):
            if event.callback_query is not None:
                # Без ответа кнопка в чужом клиенте крутится до таймаута.
                await bot.answer_callback_query(event.callback_query.id, text=texts.CLOSED_ALERT)
            elif event.message is not None and context is not None and context.chat_id is not None:
                await bot.send_message(context.chat_id, texts.CLOSED)

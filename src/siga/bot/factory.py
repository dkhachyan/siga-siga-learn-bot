"""Сборка Bot и Dispatcher."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from siga.bot.handlers import build_router
from siga.bot.middlewares import DbSessionMiddleware
from siga.config import Settings
from siga.llm.base import LlmClient

BOT_COMMANDS = [
    BotCommand(command="start", description="Начало"),
    BotCommand(command="add", description="Загрузить пачку слов"),
    BotCommand(command="pack", description="Показать текущую пачку"),
    BotCommand(command="help", description="Что умеет бот"),
]


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    llm: LlmClient,
) -> Dispatcher:
    """Собрать диспетчер. В процессе вызывается один раз.

    Роутеры хендлеров — объекты уровня модуля (обычный приём aiogram), а один
    роутер нельзя подключить к двум родителям. Боту это не мешает: диспетчер
    у процесса один. Тестам приходится делить общий — см. фикстуру
    `dispatcher` в `tests/conftest.py`.

    Клиент LLM живёт весь процесс, поэтому едет в workflow-данных, а не в
    мидлвари: хендлеру достаточно объявить параметр `llm`. Сессия базы — иначе,
    она своя на каждый апдейт.
    """
    dp = Dispatcher(llm=llm)

    db_session = DbSessionMiddleware(session_factory)
    dp.message.middleware(db_session)
    dp.callback_query.middleware(db_session)

    dp.include_router(build_router())
    return dp


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(BOT_COMMANDS)

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

BOT_COMMANDS = [
    BotCommand(command="start", description="Начало"),
    BotCommand(command="help", description="Что умеет бот"),
]


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(session_factory: async_sessionmaker[AsyncSession]) -> Dispatcher:
    dp = Dispatcher()

    db_session = DbSessionMiddleware(session_factory)
    dp.message.middleware(db_session)
    dp.callback_query.middleware(db_session)

    dp.include_router(build_router())
    return dp


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(BOT_COMMANDS)

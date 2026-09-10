"""Сборка Bot и Dispatcher."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from siga.bot.handlers import build_router
from siga.bot.middlewares import AllowlistMiddleware, DbSessionMiddleware
from siga.config import Settings
from siga.llm.base import LlmClient

#: Меню команд в Telegram. Порядок — по частоте, а не по алфавиту: разговор
#: сверху, потому что ради него всё и затевалось.
BOT_COMMANDS = [
    BotCommand(command="next", description="Начать разговор"),
    BotCommand(command="end", description="Закончить разговор"),
    BotCommand(command="add", description="Загрузить пачку слов"),
    BotCommand(command="pack", description="Показать текущую пачку"),
    BotCommand(command="memory", description="Что Ник обо мне помнит"),
    BotCommand(command="settings", description="Расписание: когда и как часто"),
    BotCommand(command="pause", description="Пауза"),
    BotCommand(command="resume", description="Продолжить"),
    BotCommand(command="start", description="Начало"),
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
    *,
    allowed: frozenset[int] = frozenset(),
) -> Dispatcher:
    """Собрать диспетчер. В процессе вызывается один раз.

    Роутеры хендлеров — объекты уровня модуля (обычный приём aiogram), а один
    роутер нельзя подключить к двум родителям. Боту это не мешает: диспетчер
    у процесса один. Тестам приходится делить общий — см. фикстуру
    `dispatcher` в `tests/conftest.py`.

    Клиент LLM живёт весь процесс, поэтому едет в workflow-данных, а не в
    мидлвари: хендлеру достаточно объявить параметр `llm`. Сессия базы — иначе,
    она своя на каждый апдейт.

    `allowed` пустой по умолчанию — бот отвечает всем. Это удобно локально и в
    тестах, а прод про такое узнаёт предупреждением в журнале (см. `runner`).
    """
    dp = Dispatcher(llm=llm)

    # Первой и на весь апдейт: незнакомец не должен доходить ни до сессии
    # базы, ни до хендлеров — см. `AllowlistMiddleware`.
    dp.update.outer_middleware(AllowlistMiddleware(allowed))

    db_session = DbSessionMiddleware(session_factory)
    dp.message.middleware(db_session)
    dp.callback_query.middleware(db_session)

    dp.include_router(build_router())
    return dp


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(BOT_COMMANDS)

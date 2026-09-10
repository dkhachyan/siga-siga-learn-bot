"""Кого бот пускает: `ALLOWED_TG_USER_IDS`.

Имя бота публично, спрятать его нельзя — значит запрет живёт только в коде, и
проверять его надо не «отвечает ли он мне», а «доходит ли посторонний до
хендлера». Поэтому мидлварь здесь зовётся напрямую с подставным хендлером:
диспетчер в прогоне один и общий (см. фикстуру `dispatcher`), второй с другим
списком в том же процессе не собрать.

Отдельно проверяется подключение: мидлварь должна стоять на `update` и раньше
сессии базы, иначе незнакомец успевает завестись в `users`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, Update, User

from siga.bot.middlewares import AllowlistMiddleware, DbSessionMiddleware
from tests.fake_telegram import CHAT_ID, FAKE_TOKEN, TG_USER_ID, RecordingSession

STRANGER_ID = 909


class Doorman:
    """Мидлварь с подставным хендлером: пустил или нет и что ответил."""

    def __init__(self, *allowed: int) -> None:
        self.session = RecordingSession()
        self.bot = Bot(
            token=FAKE_TOKEN,
            session=self.session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.guard = AllowlistMiddleware(frozenset(allowed))
        self.passed = False

    async def _handler(self, event: TelegramObject, data: dict[str, Any]) -> str:
        self.passed = True
        return "хендлер"

    async def feed(self, update: Update) -> None:
        """Прогнать апдейт так, как это делает aiogram: с контекстом события.

        `event_context` кладёт `UserContextMiddleware` — она у диспетчера
        зарегистрирована раньше нашей, так что в бою список видит уже
        разобранного пользователя. Здесь повторяется тот же порядок.
        """
        data: dict[str, Any] = {"bot": self.bot}
        await UserContextMiddleware()(
            lambda event, data: self.guard(self._handler, event, data), update, data
        )

    @property
    def replies(self) -> list[str]:
        return [
            call.text
            for call in self.session.calls
            if type(call).__name__ in {"SendMessage", "AnswerCallbackQuery"}
            and isinstance(call.text, str)
        ]


def message_from(user_id: int) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Кто-то"),
            text="/start",
        ),
    )


def click_from(user_id: int) -> Update:
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="cb1",
            from_user=User(id=user_id, is_bot=False, first_name="Кто-то"),
            chat_instance="test",
            data="cfg:menu:",
        ),
    )


@pytest.fixture
async def doorman() -> Doorman:
    return Doorman(TG_USER_ID)


async def test_a_stranger_never_reaches_a_handler(doorman: Doorman) -> None:
    """Главное обещание: чужое сообщение до кода бота не доходит."""
    await doorman.feed(message_from(STRANGER_ID))

    assert not doorman.passed


async def test_a_stranger_is_told_the_bot_is_closed(doorman: Doorman) -> None:
    """Молчание человек читает как поломку, а не как запрет."""
    await doorman.feed(message_from(STRANGER_ID))

    assert doorman.replies, "незнакомцу не ответили вовсе"
    assert "закрыт" in doorman.replies[-1]


async def test_a_stranger_pressing_a_button_gets_an_answer(doorman: Doorman) -> None:
    """Без ответа на callback кнопка в чужом клиенте крутится до таймаута."""
    await doorman.feed(click_from(STRANGER_ID))

    assert not doorman.passed
    assert doorman.replies, "кнопка осталась без ответа"


async def test_the_owner_gets_through(doorman: Doorman) -> None:
    await doorman.feed(message_from(TG_USER_ID))

    assert doorman.passed
    assert not doorman.replies, "своему не за что отказывать"


async def test_an_empty_list_lets_everyone_in() -> None:
    """Пустой список — «открыто»: локальный запуск не требует настройки."""
    open_door = Doorman()

    await open_door.feed(message_from(STRANGER_ID))

    assert open_door.passed


# --- подключение --------------------------------------------------------------


def test_the_guard_stands_before_the_database(dispatcher: Dispatcher) -> None:
    """Список — внешняя мидлварь на `update`, сессия базы — глубже.

    Проверка структурой, а не сценарием: в прогоне один общий диспетчер, и
    список у него пустой. Но подключение важнее самого решения — забыть
    `dp.update.outer_middleware` значит оставить дверь открытой при
    заполненном `.env`, и никакой сценарный тест этого не покажет.
    """
    assert any(isinstance(item, AllowlistMiddleware) for item in dispatcher.update.outer_middleware)

    for observer in (dispatcher.message, dispatcher.callback_query):
        assert any(isinstance(item, DbSessionMiddleware) for item in observer.middleware)
        assert not any(isinstance(item, AllowlistMiddleware) for item in observer.middleware)

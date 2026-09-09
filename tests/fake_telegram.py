"""Поддельный Telegram: гоняем настоящие апдейты через настоящий Dispatcher.

Проверять хендлеры вызовом функции напрямую почти бессмысленно — половина
логики живёт в фильтрах, состояниях FSM и мидлвари с сессией. Поэтому здесь
подменяется только транспорт: `Bot` с фиктивным токеном и сессией, которая
никуда не ходит, а складывает вызовы в список.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
    User,
)

#: Формат правильный, бот несуществующий — сеть всё равно не задействована.
FAKE_TOKEN = "42:AAHfake-token-for-tests-only-000000000000"

CHAT_ID = 500
TG_USER_ID = 501


class RecordingSession(BaseSession):
    """Вместо запроса к api.telegram.org — запись в список."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._next_message_id = 1000

    async def close(self) -> None:
        return None

    # Сигнатуры обоих методов заданы BaseSession — `timeout` тут не наш выбор.
    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> TelegramType:
        self.calls.append(method)
        self._next_message_id += 1
        # Хендлеры используют только результат отправки сообщения; остальным
        # методам достаточно `True`, как и настоящему Bot API.
        if type(method).__name__ in {"SendMessage", "EditMessageText"}:
            return sent_message(self._next_message_id, method)  # type: ignore[return-value]
        return True  # type: ignore[return-value]

    async def stream_content(  # type: ignore[override]
        self,
        url: str,
        headers: Any = None,
        timeout: int = 30,  # noqa: ASYNC109
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> Any:
        raise NotImplementedError


def sent_message(message_id: int, method: Any) -> Message:
    return Message(
        message_id=message_id,
        date=dt.datetime.now(dt.UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=1, is_bot=True, first_name="bot"),
        text=getattr(method, "text", None),
        reply_markup=getattr(method, "reply_markup", None),
    )


class FakeTelegram:
    """Тонкая обёртка: отправить апдейт, посмотреть, что бот ответил."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self.session = RecordingSession()
        self.bot = Bot(
            token=FAKE_TOKEN,
            session=self.session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dispatcher = dispatcher
        self._update_id = 0
        self._message_id = 0

    # --- отправка ------------------------------------------------------------

    async def send(self, text: str) -> list[str]:
        """Сообщение от человека. Возвращает тексты ответов бота."""
        self._message_id += 1
        message = Message(
            message_id=self._message_id,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=TG_USER_ID, is_bot=False, first_name="Человек"),
            text=text,
        )
        return await self._feed(Update(update_id=self._next_update(), message=message))

    async def send_photo(self) -> list[str]:
        """Фотография от человека — без файла, важен только тип апдейта."""
        self._message_id += 1
        message = Message(
            message_id=self._message_id,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=TG_USER_ID, is_bot=False, first_name="Человек"),
            photo=[PhotoSize(file_id="f", file_unique_id="u", width=800, height=600)],
        )
        return await self._feed(Update(update_id=self._next_update(), message=message))

    async def click(self, data: str) -> list[str]:
        """Нажатие инлайн-кнопки с заданным callback_data."""
        self._message_id += 1
        origin = Message(
            message_id=self._message_id,
            date=dt.datetime.now(dt.UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=1, is_bot=True, first_name="bot"),
            text="экран подтверждения",
        )
        callback = CallbackQuery(
            id=f"cb{self._message_id}",
            from_user=User(id=TG_USER_ID, is_bot=False, first_name="Человек"),
            chat_instance="test",
            message=origin,
            data=data,
        )
        return await self._feed(Update(update_id=self._next_update(), callback_query=callback))

    async def press(self, text_prefix: str) -> list[str]:
        """Нажать кнопку последнего экрана, найдя её по началу подписи."""
        button = self.find_button(text_prefix)
        if button is None:
            raise AssertionError(f"кнопки «{text_prefix}» нет на последнем экране")
        return await self.click(button)

    def forget_state(self) -> None:
        """Забыть FSM, как после рестарта процесса. База при этом остаётся."""
        storage = self.dispatcher.fsm.storage
        self.dispatcher.fsm.storage = type(storage)()

    # --- чтение --------------------------------------------------------------

    def find_button(self, text_prefix: str) -> str | None:
        """callback_data кнопки по началу подписи, начиная с последнего экрана."""
        for markup in reversed(self.keyboards):
            for row in markup.inline_keyboard:
                for button in row:
                    if button.text.startswith(text_prefix) and button.callback_data:
                        return button.callback_data
        return None

    @property
    def edits(self) -> list[str]:
        """Тексты правок сообщений.

        Карточки листаются `edit_text` — новым сообщением на каждое нажатие
        переписка превратилась бы в свалку. В ответах `send`/`click` таких
        вызовов не видно, поэтому для них отдельный список.
        """
        return [
            call.text
            for call in self.session.calls
            if type(call).__name__ == "EditMessageText" and isinstance(call.text, str)
        ]

    @property
    def keyboards(self) -> list[InlineKeyboardMarkup]:
        return [
            call.reply_markup
            for call in self.session.calls
            if isinstance(getattr(call, "reply_markup", None), InlineKeyboardMarkup)
        ]

    @property
    def last_keyboard(self) -> InlineKeyboardMarkup | None:
        return self.keyboards[-1] if self.keyboards else None

    # --- внутреннее ----------------------------------------------------------

    def _next_update(self) -> int:
        self._update_id += 1
        return self._update_id

    async def _feed(self, update: Update) -> list[str]:
        before = len(self.session.calls)
        await self.dispatcher.feed_update(self.bot, update)
        return [
            call.text
            for call in self.session.calls[before:]
            if type(call).__name__ == "SendMessage"
        ]

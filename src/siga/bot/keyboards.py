"""Инлайн-клавиатуры и данные их кнопок.

`CallbackData` вместо строк руками: aiogram сам собирает и разбирает payload,
и опечатка в имени поля становится ошибкой типов, а не молчащей кнопкой.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

#: Сколько кнопок-номеров в ряду на экранах правки и удаления.
NUMBERS_PER_ROW = 5


class PackAction(CallbackData, prefix="pack"):
    """Кнопки экрана подтверждения."""

    action: Literal["confirm", "edit", "delete", "add", "back"]


class WordAction(CallbackData, prefix="word"):
    """Выбор конкретного слова номером."""

    action: Literal["edit", "delete"]
    word_id: int


class CardAction(CallbackData, prefix="card"):
    """Листание карточек. `index` — номер слова с нуля в порядке пачки.

    Номер, а не `word_id`: кнопка «дальше» должна знать, куда идти, а не что
    показать, и при удалении слова из пачки список просто сдвигается — вместо
    кнопки, ведущей в никуда.
    """

    action: Literal["open", "show", "list"]
    """`open` — первая карточка новым сообщением, `show` — листание на месте.

    Разделены нарочно: список пачки может не влезть в одно сообщение, и тогда
    «вернуться к списку» — это несколько новых сообщений, а не правка одного.
    Листать же удобно на месте, не заваливая переписку.
    """

    index: int = 0


def active_pack_screen() -> InlineKeyboardMarkup:
    """Кнопки под списком активной пачки. Правки тут нет: пачка уже в работе."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🃏 Карточки", callback_data=CardAction(action="open", index=0))
    return builder.as_markup()


def card_nav(index: int, total: int) -> InlineKeyboardMarkup:
    """Стрелки листания и возврат к списку.

    Листание кольцевое: с последней карточки «дальше» ведёт на первую. Так
    не нужна отдельная неактивная кнопка на краях — а тупиковая стрелка,
    которая просто ничего не делает, читается как поломка.
    """
    builder = InlineKeyboardBuilder()
    if total > 1:
        builder.button(text="←", callback_data=CardAction(action="show", index=(index - 1) % total))
        builder.button(text="→", callback_data=CardAction(action="show", index=(index + 1) % total))
        builder.adjust(2)
    builder.row(
        InlineKeyboardButton(text="☰ Списком", callback_data=CardAction(action="list").pack())
    )
    return builder.as_markup()


def pack_screen() -> InlineKeyboardMarkup:
    """Обязательный набор кнопок экрана подтверждения, FR-IMP-8."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Всё верно", callback_data=PackAction(action="confirm"))
    builder.button(text="✏️ Править", callback_data=PackAction(action="edit"))
    builder.button(text="🗑 Удалить", callback_data=PackAction(action="delete"))
    builder.button(text="➕ Добавить", callback_data=PackAction(action="add"))
    builder.adjust(1, 3)
    return builder.as_markup()


def word_numbers(
    word_ids: Sequence[int], action: Literal["edit", "delete"]
) -> InlineKeyboardMarkup:
    """Кнопки-номера по числу слов в пачке плюс возврат.

    Номер, а не само слово: греческое слово на кнопке не помещается, а номер
    совпадает с тем, что человек видит в списке.
    """
    builder = InlineKeyboardBuilder()
    for index, word_id in enumerate(word_ids, start=1):
        builder.button(text=str(index), callback_data=WordAction(action=action, word_id=word_id))
    builder.adjust(*([NUMBERS_PER_ROW] * (len(word_ids) // NUMBERS_PER_ROW + 1)))
    builder.row(
        InlineKeyboardButton(text="← Назад", callback_data=PackAction(action="back").pack())
    )
    return builder.as_markup()

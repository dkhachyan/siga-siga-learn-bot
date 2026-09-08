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

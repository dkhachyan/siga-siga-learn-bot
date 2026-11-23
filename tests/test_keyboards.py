"""Кнопки-номера слов: страницы для больших пачек и меню уроков."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from siga.bot import keyboards
from siga.bot.keyboards import NUMBERS_PER_PAGE

#: Сколько кнопок Telegram принимает в одной клавиатуре.
TELEGRAM_MAX_BUTTONS = 100


def _texts(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_small_pack_has_no_page_buttons() -> None:
    markup = keyboards.word_numbers(list(range(100, 112)), "delete")
    texts = _texts(markup)
    assert texts[:12] == [str(n) for n in range(1, 13)]
    assert texts[12:] == ["← Назад"]


def test_big_pack_is_split_into_pages_under_the_telegram_limit() -> None:
    ids = list(range(1000, 1300))  # пачка из уроков на триста слов

    first = keyboards.word_numbers(ids, "drop", back_to_list=True)
    texts = _texts(first)
    assert len(texts) <= TELEGRAM_MAX_BUTTONS
    assert texts[0] == "1"
    assert str(NUMBERS_PER_PAGE) in texts
    assert str(NUMBERS_PER_PAGE + 1) not in texts
    assert f"{NUMBERS_PER_PAGE + 1}–{2 * NUMBERS_PER_PAGE} →" in texts
    assert not any(text.startswith("← 1") for text in texts), "назад с первой некуда"


def test_middle_page_numbers_continue_and_point_both_ways() -> None:
    ids = list(range(1000, 1120))
    texts = _texts(keyboards.word_numbers(ids, "edit", page=1))

    assert texts[0] == str(NUMBERS_PER_PAGE + 1)
    assert f"← 1–{NUMBERS_PER_PAGE}" in texts
    assert f"{2 * NUMBERS_PER_PAGE + 1}–120 →" in texts


def test_number_buttons_carry_the_word_ids_of_their_page() -> None:
    ids = list(range(1000, 1120))
    markup = keyboards.word_numbers(ids, "delete", page=2)
    data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert f"word:delete:{ids[2 * NUMBERS_PER_PAGE]}" in data
    assert f"word:delete:{ids[-1]}" in data
    assert f"word:delete:{ids[0]}" not in data


def test_page_out_of_range_is_clamped() -> None:
    """Кнопка из старой переписки: пачка с тех пор похудела."""
    ids = list(range(1000, 1010))
    texts = _texts(keyboards.word_numbers(ids, "drop", back_to_list=True, page=7))
    assert texts[0] == "1"


def test_numbers_page_of() -> None:
    assert keyboards.numbers_page_of(0) == 0
    assert keyboards.numbers_page_of(NUMBERS_PER_PAGE - 1) == 0
    assert keyboards.numbers_page_of(NUMBERS_PER_PAGE) == 1


def test_lesson_menu_has_a_button_per_lesson() -> None:
    markup = keyboards.lesson_menu(range(1, 23))
    data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert len(data) == 22
    assert data[0] == "lsn:1:1"
    assert data[-1] == "lsn:22:22"

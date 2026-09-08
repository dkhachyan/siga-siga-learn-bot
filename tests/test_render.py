"""Вёрстка экрана подтверждения."""

from __future__ import annotations

import datetime as dt

import pytest

from siga.bot.render import (
    MESSAGE_LIMIT,
    WordLine,
    pack_screen,
    pack_title,
    plural_ru,
    word_line,
    words_form,
)

TELEGRAM_LIMIT = 4096


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "слово"),
        (2, "слова"),
        (4, "слова"),
        (5, "слов"),
        (0, "слов"),
        # Подвох русского счёта: 11–14 всегда «слов», хотя кончаются на 1..4
        (11, "слов"),
        (12, "слов"),
        (14, "слов"),
        (21, "слово"),
        (22, "слова"),
        (25, "слов"),
        (101, "слово"),
        (111, "слов"),
        (112, "слов"),
    ],
)
def test_words_form(count: int, expected: str) -> None:
    assert words_form(count) == expected


def test_plural_ru_is_not_hardcoded_to_words() -> None:
    assert plural_ru(1, "день", "дня", "дней") == "день"
    assert plural_ru(3, "день", "дня", "дней") == "дня"
    assert plural_ru(13, "день", "дня", "дней") == "дней"


def test_pack_title_uses_genitive_month() -> None:
    assert pack_title(dt.date(2026, 9, 8)) == "Пачка от 8 сентября"
    assert pack_title(dt.date(2026, 3, 1)) == "Пачка от 1 марта"
    assert pack_title(dt.date(2026, 12, 31)) == "Пачка от 31 декабря"


def test_word_line_marks_a_missing_translation() -> None:
    assert word_line(1, WordLine("το νερό", "вода")) == "1. <b>το νερό</b> — вода"
    assert word_line(7, WordLine("το νερό", None)) == "7. <b>το νερό</b> — <i>переведу сам</i>"


def test_word_line_marks_a_word_from_a_previous_pack() -> None:
    assert word_line(2, WordLine("ο καφές", "кофе", known=True)).endswith("— кофе ↩︎")


def test_pack_screen_numbers_words_from_one() -> None:
    words = [WordLine("το νερό", "вода"), WordLine("ο καφές", "кофе")]
    screen = pack_screen("Пачка от 8 сентября", words)

    assert len(screen) == 1
    assert screen[0].startswith("<b>Пачка от 8 сентября</b> — 2 слова")
    assert "1. <b>το νερό</b> — вода" in screen[0]
    assert "2. <b>ο καφές</b> — кофе" in screen[0]
    assert "↩︎" not in screen[0], "без повторов подсказка не нужна"


def test_pack_screen_explains_the_repeat_mark() -> None:
    words = [WordLine("το νερό", "вода", known=True), WordLine("ο καφές", "кофе")]
    screen = pack_screen("Пачка", words)
    assert "↩︎ — 1 слово из прошлых пачек" in screen[-1]


def test_pack_screen_splits_a_long_list_across_messages() -> None:
    words = [WordLine(f"η λέξη νούμερο {i}", f"слово номер {i}") for i in range(1, 101)]
    screen = pack_screen("Большая пачка", words)

    assert len(screen) > 1, "сотня слов в одно сообщение Telegram не влезает"
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in screen)
    assert all(len(chunk) <= MESSAGE_LIMIT for chunk in screen)

    # Ни одно слово не потерялось и не задвоилось на стыке сообщений
    joined = "\n".join(screen)
    for number in range(1, 101):
        assert joined.count(f"{number}. <b>η λέξη νούμερο {number}</b>") == 1


def test_pack_screen_survives_an_empty_pack() -> None:
    screen = pack_screen("Пустая", [])
    assert screen == ["<b>Пустая</b> — 0 слов\n"]

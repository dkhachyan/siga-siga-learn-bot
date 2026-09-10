"""Вёрстка экрана подтверждения, списка активной пачки и карточек."""

from __future__ import annotations

import datetime as dt

import pytest

from siga.bot.render import (
    MESSAGE_LIMIT,
    Card,
    WordLine,
    active_pack_screen,
    gap_form,
    pack_screen,
    pack_title,
    plural_ru,
    word_card,
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


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (15, "15 минут"),
        (45, "45 минут"),
        (60, "1 час"),
        (90, "1 час 30 минут"),
        (120, "2 часа"),
        (121, "2 часа 1 минута"),
        (480, "8 часов"),
    ],
)
def test_gap_form_reads_like_speech_not_like_a_timer(minutes: int, expected: str) -> None:
    assert gap_form(minutes) == expected


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


# --- список активной пачки ----------------------------------------------------


def test_active_pack_screen_shows_how_long_is_left() -> None:
    screen = active_pack_screen("Пачка", [WordLine("το νερό", "вода")], days_left=9)
    assert "1. <b>το νερό</b> — вода" in screen[-1]
    assert "9 дней" in screen[-1]


def test_active_pack_screen_says_the_period_is_over() -> None:
    screen = active_pack_screen("Пачка", [WordLine("το νερό", "вода")], days_left=0)
    assert "закончился" in screen[-1].lower()


def test_active_pack_screen_omits_the_deadline_when_there_is_none() -> None:
    screen = active_pack_screen("Пачка", [WordLine("το νερό", "вода")], days_left=None)
    assert "период" not in screen[-1].lower()


# --- карточка слова -----------------------------------------------------------


NOUN = Card(
    lemma="το νερό",
    translation="вода",
    pos="noun",
    article="το",
    gender="n",
    examples=({"el": "Θέλω νερό.", "ru": "Хочу воды."},),
    enriched=True,
)


def test_word_card_shows_the_word_grammar_and_example() -> None:
    card = word_card(NOUN, position=1, total=12, title="Пачка от 8 сентября")

    assert "<b>το νερό</b>" in card
    assert "вода" in card
    assert "существительное, средний род" in card
    assert "Θέλω νερό." in card
    assert "Хочу воды." in card


def test_word_card_says_where_in_the_pack_it_is() -> None:
    """Счётчик в тексте, а не мёртвой кнопкой: листать иначе некуда."""
    assert "3 из 12" in word_card(NOUN, position=3, total=12, title="Пачка")


def test_word_card_does_not_double_the_article() -> None:
    """Артикль обычно уже в лемме — «το το νερό» быть не должно."""
    assert word_card(NOUN, position=1, total=1, title="Пачка").count("το νερό") >= 1
    assert "το το νερό" not in word_card(NOUN, position=1, total=1, title="Пачка")


def test_word_card_adds_a_missing_article() -> None:
    card = Card(lemma="νερό", translation="вода", pos="noun", article="το", enriched=True)
    assert "<b>το νερό</b>" in word_card(card, position=1, total=1, title="Пачка")


def test_word_card_shows_a_verb_form_only_when_it_differs() -> None:
    same = Card(lemma="πίνω", translation="пить", pos="verb", verb_form="πίνω", enriched=True)
    other = Card(lemma="μιλώ", translation="говорить", pos="verb", verb_form="μιλάω", enriched=True)

    assert "1 л. ед. ч." not in word_card(same, position=1, total=1, title="Пачка")
    assert "1 л. ед. ч. — μιλάω" in word_card(other, position=1, total=1, title="Пачка")


def test_word_card_shows_the_dictionary_translation_beside_a_human_one() -> None:
    """FR-IMP-4: свой перевод остаётся, словарный показывается рядом."""
    card = Card(lemma="το νερό", translation="вода", translation_model="водичка", enriched=True)
    text = word_card(card, position=1, total=1, title="Пачка")

    assert "вода" in text
    assert "водичка" in text


def test_word_card_hides_a_dictionary_translation_that_is_the_same() -> None:
    card = Card(lemma="το νερό", translation="вода", translation_model="вода", enriched=True)
    assert word_card(card, position=1, total=1, title="Пачка").count("вода") == 1


def test_word_card_admits_a_word_without_grammar() -> None:
    """Обогащение могло не дойти — карточка всё равно показывает слово."""
    card = Card(lemma="ευχαριστώ", translation="спасибо")
    text = word_card(card, position=1, total=1, title="Пачка")

    assert "ευχαριστώ" in text
    assert "спасибо" in text
    assert "ещё не собрал" in text


def test_word_card_stays_silent_about_grammar_it_has_no_words_for() -> None:
    """`other` — предлоги и частицы: писать «часть речи: other» незачем."""
    card = Card(lemma="και", translation="и", pos="other", enriched=True)
    text = word_card(card, position=1, total=1, title="Пачка")

    assert "other" not in text
    assert "ещё не собрал" not in text

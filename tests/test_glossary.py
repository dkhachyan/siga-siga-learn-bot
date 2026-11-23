"""Встроенный словарь уроков (FR-IMP-12): чтение, выбор по урокам, диапазон."""

from __future__ import annotations

import pytest

from siga import glossary
from siga.core.wordlist import MAX_LEMMA_LEN, MAX_LESSON_PACK_WORDS, MAX_TRANSLATION_LEN

CSV = """урок,категория,греческий,русский
1,Существительные,ο άντρας,"мужчина, муж"
1,Существительные,κύριε (κλητική),господин (обращение)
1,Существительные,(το διαδίκτυο),интернет
2,Существительные,ο Άγγλος - η Αγγλίδα:,англичанин - англичанка
2,Существительные,ο άντρας,муж (повтор)
2,Глаголы,просто по-русски,без греческого
3,Глаголы,είμαι,(я) есть
"""


def test_bundled_glossary_has_all_lessons() -> None:
    book = glossary.load()
    assert book.lessons == tuple(range(1, 23))


def test_every_lesson_fits_a_pack_on_its_own() -> None:
    """Иначе кнопка урока в меню вела бы в отказ «слишком много»."""
    book = glossary.load()
    for lesson in book.lessons:
        assert 0 < len(book.words(lesson, lesson)) <= MAX_LESSON_PACK_WORDS


def test_bundled_words_fit_the_database_columns() -> None:
    book = glossary.load()
    for word in book.words(1, 22):
        assert len(word.lemma) <= MAX_LEMMA_LEN
        assert word.translation_ru is None or len(word.translation_ru) <= MAX_TRANSLATION_LEN


def test_words_keep_book_order_and_drop_repeats_across_lessons() -> None:
    book = glossary.parse(CSV)
    words = book.words(1, 3)

    lemmas = [word.lemma for word in words]
    assert lemmas[0] == "ο άντρας"
    assert lemmas.count("ο άντρας") == 1, "повтор из второго урока выброшен"
    assert words[0].translation_ru == "мужчина, муж", "перевод — из первого появления"
    assert lemmas[-1] == "είμαι"


def test_words_of_one_lesson_only() -> None:
    book = glossary.parse(CSV)
    assert [word.lemma for word in book.words(3, 3)] == ["είμαι"]


def test_rows_without_greek_are_skipped() -> None:
    book = glossary.parse(CSV)
    assert all("по-русски" not in word.lemma for word in book.words(1, 3))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ο Άγγλος - η Αγγλίδα:", "ο Άγγλος - η Αγγλίδα"),
        ("(το διαδίκτυο)", "το διαδίκτυο"),
        ("κύριε (κλητική)", "κύριε (κλητική)"),
        ("  ο   άντρας ", "ο άντρας"),
    ],
)
def test_clean_greek_removes_layout_leftovers_only(raw: str, expected: str) -> None:
    assert glossary.clean_greek(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3", (3, 3)),
        ("1-4", (1, 4)),
        ("1–4", (1, 4)),
        ("1 — 4", (1, 4)),
        (" 2 - 5 ", (2, 5)),
        ("4-1", (1, 4)),
        ("22", (22, 22)),
    ],
)
def test_parse_range_accepts_a_lesson_or_a_range(text: str, expected: tuple[int, int]) -> None:
    assert glossary.parse_range(text, range(1, 23)) == expected


@pytest.mark.parametrize("text", ["", "abc", "0", "23", "1-23", "1,3", "1-", "-3", "3 5"])
def test_parse_range_rejects_what_is_not_a_lesson(text: str) -> None:
    assert glossary.parse_range(text, range(1, 23)) is None

"""Сборка текста сообщений из данных.

Чистые функции: на вход строки и числа, на выходе строки. Ни бота, ни базы —
чтобы вёрстку экрана подтверждения можно было проверить тестом, а не глазами
в Telegram.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Telegram режет сообщение на 4096 символах. Оставляем запас на заголовок и
#: на то, что HTML-теги считаются вместе с текстом.
MESSAGE_LIMIT = 3800

#: Родительный падеж: «от 8 сентября». Стандартный `%B` даёт именительный.
MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def pack_title(when: dt.date) -> str:
    """Название пачки по дате загрузки: «Пачка от 8 сентября»."""
    return f"Пачка от {when.day} {MONTHS_RU[when.month - 1]}"


@dataclass(frozen=True, slots=True)
class WordLine:
    """То, что нужно строке списка, — без модели SQLAlchemy."""

    lemma: str
    translation: str | None
    known: bool = False
    """Слово уже встречалось в прошлых пачках (FR-IMP-5)."""


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Русская форма числительного: 1 слово, 2 слова, 5 слов."""
    tail_100 = count % 100
    if 11 <= tail_100 <= 14:
        return many
    match count % 10:
        case 1:
            return one
        case 2 | 3 | 4:
            return few
        case _:
            return many


def words_form(count: int) -> str:
    return plural_ru(count, "слово", "слова", "слов")


def word_line(index: int, word: WordLine) -> str:
    """Одна строка нумерованного списка."""
    translation = word.translation or "<i>переведу сам</i>"
    mark = " ↩︎" if word.known else ""
    return f"{index}. <b>{word.lemma}</b> — {translation}{mark}"


def _chunk(lines: Iterable[str], *, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Склеить строки в сообщения, не переступая лимит Telegram."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + 1
        if current and length + addition > limit:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


def pack_screen(title: str, words: Sequence[WordLine]) -> list[str]:
    """Экран подтверждения (FR-IMP-8): заголовок и нумерованный список.

    Возвращает список сообщений: сотня слов в одно сообщение Telegram не влезает.
    Кнопки вешаются на последнее.
    """
    header = f"<b>{title}</b> — {len(words)} {words_form(len(words))}"
    lines = [word_line(i, word) for i, word in enumerate(words, start=1)]

    known_count = sum(1 for word in words if word.known)
    footer = []
    if known_count:
        footer = [
            "",
            f"↩︎ — {known_count} {words_form(known_count)} из прошлых пачек. "
            "Оставишь — будем повторять, не нужны — удали.",
        ]

    chunks = _chunk([header, "", *lines, *footer])
    return chunks

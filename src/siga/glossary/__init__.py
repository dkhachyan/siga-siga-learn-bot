"""Встроенный словарь учебника: слова по урокам (FR-IMP-12).

Источник — словари в конце уроков «Ελληνικά για σας Α1» (уроки 1–22),
выгруженные в `greek_a1.csv`: урок, категория, греческое слово, перевод.
Здесь только чтение файла и выбор слов по номерам уроков; пачку из них
заводит `bot.handlers.lessons` тем же путём, что и импорт текстом.

Читается один раз за процесс: файл в пакете и на ходу не меняется.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources

from siga.core.greek import has_greek
from siga.core.wordlist import MAX_LEMMA_LEN, MAX_TRANSLATION_LEN, ParsedWord

GLOSSARY_FILE = "greek_a1.csv"

#: Название учебника — для заголовков и справки.
BOOK_TITLE = "Ελληνικά για σας Α1"

#: «3», «1-4», «1–4», «1 — 4». Номера уроков — не больше трёх цифр.
_RANGE_RE = re.compile(r"^\s*(\d{1,3})\s*(?:[-–—]\s*(\d{1,3}))?\s*$")


@dataclass(frozen=True, slots=True)
class Entry:
    lesson: int
    category: str
    greek: str
    russian: str


@dataclass(frozen=True, slots=True)
class Glossary:
    entries: tuple[Entry, ...]

    @property
    def lessons(self) -> tuple[int, ...]:
        """Номера уроков, у которых есть слова, по возрастанию."""
        return tuple(sorted({entry.lesson for entry in self.entries}))

    def words(self, first: int, last: int) -> list[ParsedWord]:
        """Слова уроков `first..last` включительно, без повторов, в порядке книги.

        Повтор — по ключу леммы (`ParsedWord.key`), как и при импорте текстом:
        одно и то же слово из двух уроков в пачку дважды не попадает.
        """
        words: list[ParsedWord] = []
        seen: set[str] = set()
        for line_no, entry in enumerate(self.entries, start=1):
            if not first <= entry.lesson <= last:
                continue
            word = ParsedWord(
                line_no=line_no,
                raw=f"{entry.greek} — {entry.russian}",
                lemma=entry.greek,
                translation_ru=entry.russian[:MAX_TRANSLATION_LEN] or None,
            )
            if word.key in seen:
                continue
            seen.add(word.key)
            words.append(word)
        return words


def clean_greek(text: str) -> str:
    """Убрать следы вёрстки учебника, не трогая само слово.

    Двоеточие в конце («ο Άγγλος - η Αγγλίδα:») — это подпись к таблице, а
    слово целиком в скобках («(το διαδίκτυο)») — сноска. Скобки внутри
    («κύριε (κλητική)») — пояснение, их оставляем.
    """
    text = " ".join(text.split()).rstrip(":").strip()
    if text.startswith("(") and text.endswith(")") and text.count("(") == 1:
        text = text[1:-1].strip()
    return text


def parse(text: str) -> Glossary:
    """Разобрать CSV словаря. Строки без греческого или слишком длинные пропускаются."""
    entries: list[Entry] = []
    for row in csv.DictReader(io.StringIO(text)):
        greek = clean_greek(row["греческий"])
        russian = " ".join(row["русский"].split())
        if not has_greek(greek) or len(greek) > MAX_LEMMA_LEN:
            continue
        entries.append(
            Entry(
                lesson=int(row["урок"]),
                category=row["категория"].strip(),
                greek=greek,
                russian=russian,
            )
        )
    return Glossary(tuple(entries))


@cache
def load() -> Glossary:
    """Словарь из пакета."""
    text = resources.files(__name__).joinpath(GLOSSARY_FILE).read_text(encoding="utf-8")
    return parse(text)


def parse_range(text: str, lessons: Sequence[int]) -> tuple[int, int] | None:
    """«3» → (3, 3), «1-4» → (1, 4). None — не номер или урока такого нет.

    Обратный порядок («4-1») переворачиваем: что человек имел в виду, понятно.
    """
    match = _RANGE_RE.match(text)
    if match is None or not lessons:
        return None
    first = int(match.group(1))
    last = int(match.group(2) or first)
    first, last = min(first, last), max(first, last)
    if first < min(lessons) or last > max(lessons):
        return None
    return first, last


__all__ = [
    "BOOK_TITLE",
    "Entry",
    "Glossary",
    "clean_greek",
    "load",
    "parse",
    "parse_range",
]

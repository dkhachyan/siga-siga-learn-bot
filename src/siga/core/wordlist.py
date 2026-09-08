"""Разбор списка слов, введённого текстом.

Люди пишут списки как попало: через тире, двоеточие, табуляцию, с номерами,
с артиклями и без, иногда переводом вперёд. Задача разбора — вытащить из
этого пары «греческое слово — перевод», не потеряв ничего молча: всё, что
не разобралось, возвращается отдельным списком, чтобы показать человеку.

Здесь нет ни базы, ни сети: на вход строка, на выходе структура. Всё
остальное — работа вызывающего кода.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from siga.core.greek import has_greek, normalize_lemma

#: Лимиты пачки, FR-IMP-6.
MIN_PACK_WORDS = 10
MAX_PACK_WORDS = 100

#: Максимальная длина леммы. Греческое слово длиннее — почти наверняка
#: склеившаяся строка или мусор распознавания.
MAX_LEMMA_LEN = 48
MAX_TRANSLATION_LEN = 120

#: Разделители «слово / перевод» в порядке убывания однозначности.
_SEPARATORS = ("\t", " — ", " – ", " -- ", " - ", "—", "–", "−", ":", "=", "  ", "-")

#: Маркер списка в начале строки: «1.», «12)», «-», «•», «*».
_LIST_MARKER_RE = re.compile(r"^\s*(?:\d{1,3}\s*[.)]\s*|[-•*·]\s+)")

#: Кавычки и знаки препинания по краям куска.
_TRIM_CHARS = " \t\"'«»„“”‚‘’()[]…,;.!?"


class RejectReason(StrEnum):
    NO_GREEK = "no_greek"
    """В строке нет ни одной греческой буквы."""

    TOO_LONG = "too_long"
    """Слишком длинно для слова — скорее всего слиплись строки."""


@dataclass(frozen=True, slots=True)
class ParsedWord:
    line_no: int
    raw: str
    lemma: str
    """Греческая часть в том виде, как её написал человек, — с артиклем и ударением."""
    translation_ru: str | None
    """None, если перевода в источнике не было: сгенерируем при обогащении (FR-IMP-3)."""

    @property
    def key(self) -> str:
        """Ключ сравнения: без артикля, без ударений, в нижнем регистре."""
        return normalize_lemma(self.lemma)


@dataclass(frozen=True, slots=True)
class RejectedLine:
    line_no: int
    raw: str
    reason: RejectReason


@dataclass(frozen=True, slots=True)
class ParseResult:
    words: tuple[ParsedWord, ...]
    """Уникальные слова в порядке появления."""
    duplicates: tuple[ParsedWord, ...]
    """Повторы внутри самой пачки — выброшены, но показываем их человеку."""
    rejected: tuple[RejectedLine, ...]
    """Строки, из которых слово не вышло."""


def _trim(part: str) -> str:
    return " ".join(part.strip(_TRIM_CHARS).split())


def _split_pair(line: str) -> tuple[str, str | None]:
    """Разрезать строку по самому раннему разделителю."""
    best_at: int | None = None
    best_sep = ""
    for sep in _SEPARATORS:
        at = line.find(sep)
        if at <= 0:  # -1 — нет; 0 — строка начинается с разделителя, это не пара
            continue
        if best_at is None or at < best_at or (at == best_at and len(sep) > len(best_sep)):
            best_at, best_sep = at, sep

    if best_at is None:
        return line, None
    return line[:best_at], line[best_at + len(best_sep) :]


def parse_wordlist(text: str) -> ParseResult:
    """Разобрать текст, введённый человеком, в список слов."""
    words: list[ParsedWord] = []
    duplicates: list[ParsedWord] = []
    rejected: list[RejectedLine] = []
    seen: set[str] = set()

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        raw = raw_line.strip()
        if not raw:
            continue

        line = _LIST_MARKER_RE.sub("", raw)
        left, right = _split_pair(line)
        left, right = _trim(left), _trim(right) if right is not None else None

        # Перевод могли написать первым — ориентируемся на письменность, не на порядок
        if has_greek(left):
            lemma, translation = left, right
        elif right and has_greek(right):
            lemma, translation = right, left
        else:
            rejected.append(RejectedLine(line_no, raw, RejectReason.NO_GREEK))
            continue

        if not lemma or len(lemma) > MAX_LEMMA_LEN:
            rejected.append(RejectedLine(line_no, raw, RejectReason.TOO_LONG))
            continue

        word = ParsedWord(
            line_no=line_no,
            raw=raw,
            lemma=lemma,
            translation_ru=translation[:MAX_TRANSLATION_LEN] if translation else None,
        )

        if word.key in seen:
            duplicates.append(word)
            continue

        seen.add(word.key)
        words.append(word)

    return ParseResult(tuple(words), tuple(duplicates), tuple(rejected))

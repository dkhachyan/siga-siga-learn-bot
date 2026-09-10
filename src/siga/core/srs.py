"""Лестница интервалов: когда слово показывать снова и что делать с боксом.

Чистая логика §5.7 — ни базы, ни бота. Здесь считается вся арифметика
повторений, а `db.progress` только раскладывает результат по строкам.
Разделение не ради красоты: правила SRS придётся крутить по живым данным,
и крутить их проще там, где тест — это три числа на входе и три на выходе.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from siga.core.enums import Verdict

#: Верхний бокс лестницы, §5.7. Дальше слово считается выученным.
MAX_BOX = 5

#: Сколько ждать до следующего показа на каждом боксе.
#: Нулевой бокс — новое слово: интервала нет, оно и так в пуле.
BOX_INTERVALS: dict[int, dt.timedelta] = {
    1: dt.timedelta(minutes=20),
    2: dt.timedelta(hours=2),
    3: dt.timedelta(days=1),
    4: dt.timedelta(days=3),
    5: dt.timedelta(days=7),
}

#: Сколько верных употреблений подряд нужно на верхнем боксе (FR-SRS-3).
STREAK_TO_LEARN = 2


@dataclass(frozen=True, slots=True)
class Progress:
    """Состояние слова: то же, что в строке `word_progress`, но без базы."""

    box: int = 0
    correct_streak: int = 0
    uses: int = 0
    correct_uses: int = 0
    next_due_at: dt.datetime | None = None
    learned: bool = False


@dataclass(frozen=True, slots=True)
class Outcome:
    """Итог слова за эпизод — агрегат всех его оценок (FR-SRS-1).

    Именно агрегат, а не оценка последнего хода: слово, употреблённое в
    эпизоде трижды, должно продвинуть бокс один раз.
    """

    used: bool
    verdict: Verdict | None = None
    """None — слово так и не прозвучало (FR-EP-7). Это не ошибка."""


def aggregate(verdicts: Iterable[Verdict]) -> Outcome:
    """Свести оценки слова за эпизод к одной.

    Лучшая из оценок, а не худшая и не последняя. Рекаст для того и нужен:
    человек ошибся, Ник переспросил правильной формой, человек повторил верно —
    это удача эпизода, и наказывать за первую попытку значит обессмыслить приём.
    """
    seen = list(verdicts)
    if not seen:
        return Outcome(used=False)
    for best in (Verdict.CORRECT, Verdict.ALMOST):
        if best in seen:
            return Outcome(used=True, verdict=best)
    return Outcome(used=True, verdict=Verdict.INCORRECT)


def next_box(box: int, verdict: Verdict) -> int:
    """Куда переехать по лестнице, FR-SRS-2."""
    match verdict:
        case Verdict.CORRECT:
            return min(box + 1, MAX_BOX)
        case Verdict.ALMOST:
            return box
        case Verdict.INCORRECT:
            # До первого бокса, а не до нуля: нулевой означает «новое слово»,
            # а слово, которое человек уже пробовал и ошибся, — не новое.
            return 1


def due_at(box: int, now: dt.datetime) -> dt.datetime | None:
    """Когда слово созреет. None на нулевом боксе — оно уже в пуле."""
    interval = BOX_INTERVALS.get(box)
    return None if interval is None else now + interval


def review(current: Progress, outcome: Outcome, now: dt.datetime) -> Progress:
    """Новое состояние слова после закрытия эпизода.

    Не употреблённое слово (FR-EP-7, FR-SRS-5) бокс не трогает, но получает
    `next_due_at` в прошлом: по FR-SRS-4 просроченные идут первыми, так что
    «уклонился» само собой означает «спрошу в первую очередь». Отдельного
    флага приоритета для этого не нужно.
    """
    if not outcome.used or outcome.verdict is None:
        return replace(current, next_due_at=now)

    box = next_box(current.box, outcome.verdict)
    correct = outcome.verdict is Verdict.CORRECT
    streak = current.correct_streak + 1 if correct else 0

    return Progress(
        box=box,
        correct_streak=streak,
        uses=current.uses + 1,
        correct_uses=current.correct_uses + (1 if correct else 0),
        next_due_at=due_at(box, now),
        learned=box >= MAX_BOX and streak >= STREAK_TO_LEARN,
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    """Слово-кандидат в эпизод: только то, чем определяется очередь."""

    word_id: int
    box: int = 0
    next_due_at: dt.datetime | None = None


def _priority(candidate: Candidate, now: dt.datetime) -> tuple[int, float, int]:
    """Ключ сортировки по FR-SRS-4: просроченные → новые → ближайшие по времени.

    Внутри просроченных — сначала те, что ждут дольше: иначе слово, которое
    человек однажды пропустил, может так и не всплыть никогда.
    """
    if candidate.next_due_at is None:
        return (1, 0.0, candidate.word_id)
    delay = (candidate.next_due_at - now).total_seconds()
    group = 0 if delay <= 0 else 2
    return (group, delay, candidate.word_id)


def select(candidates: Sequence[Candidate], *, now: dt.datetime, limit: int) -> list[Candidate]:
    """Отобрать слова для эпизода в порядке приоритета."""
    return sorted(candidates, key=lambda item: _priority(item, now))[:limit]

"""Лестница интервалов: что происходит со словом после эпизода.

Тесты на чистые функции — три числа на входе, три на выходе. База и модель
сюда не заглядывают, поэтому правила §5.7 видно в одном месте, и менять их
можно, не поднимая Postgres.
"""

from __future__ import annotations

import datetime as dt

import pytest

from siga.core import srs
from siga.core.enums import Verdict

NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)


def test_correct_answer_moves_one_box_up() -> None:
    after = srs.review(srs.Progress(box=2), srs.Outcome(used=True, verdict=Verdict.CORRECT), NOW)

    assert after.box == 3
    assert after.next_due_at == NOW + dt.timedelta(days=1)
    assert after.uses == 1
    assert after.correct_uses == 1


def test_almost_holds_the_box() -> None:
    """Рекаст не наказывает и не награждает: слово остаётся на своей ступени."""
    after = srs.review(srs.Progress(box=3), srs.Outcome(used=True, verdict=Verdict.ALMOST), NOW)

    assert after.box == 3
    assert after.correct_streak == 0
    assert after.correct_uses == 0


def test_mistake_drops_to_the_first_box_not_to_zero() -> None:
    """Нулевой бокс означает «новое слово», а ошибившийся уже не новичок."""
    after = srs.review(srs.Progress(box=4), srs.Outcome(used=True, verdict=Verdict.INCORRECT), NOW)

    assert after.box == 1
    assert after.next_due_at == NOW + dt.timedelta(minutes=20)


def test_the_ladder_stops_at_the_top() -> None:
    after = srs.review(
        srs.Progress(box=srs.MAX_BOX, correct_streak=0),
        srs.Outcome(used=True, verdict=Verdict.CORRECT),
        NOW,
    )

    assert after.box == srs.MAX_BOX
    assert not after.learned, "одного верного ответа на верхнем боксе мало (FR-SRS-3)"


def test_two_correct_answers_on_the_top_box_mean_learned() -> None:
    after = srs.review(
        srs.Progress(box=srs.MAX_BOX, correct_streak=1),
        srs.Outcome(used=True, verdict=Verdict.CORRECT),
        NOW,
    )

    assert after.learned


def test_a_mistake_resets_the_streak() -> None:
    after = srs.review(
        srs.Progress(box=srs.MAX_BOX, correct_streak=1),
        srs.Outcome(used=True, verdict=Verdict.ALMOST),
        NOW,
    )

    assert after.correct_streak == 0
    assert not after.learned


def test_an_unused_word_keeps_its_box_but_comes_back_first() -> None:
    """FR-EP-7: уклонился — не ошибся. Штрафа нет, но слово идёт в начало очереди."""
    before = srs.Progress(box=3, correct_streak=1, uses=5, correct_uses=4)
    after = srs.review(before, srs.Outcome(used=False), NOW)

    assert after.box == 3
    assert after.correct_streak == 1
    assert after.uses == 5
    assert after.next_due_at == NOW, "просроченные идут первыми — отдельный флаг не нужен"


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([], None),
        ([Verdict.INCORRECT], Verdict.INCORRECT),
        ([Verdict.INCORRECT, Verdict.CORRECT], Verdict.CORRECT),
        ([Verdict.CORRECT, Verdict.INCORRECT], Verdict.CORRECT),
        ([Verdict.INCORRECT, Verdict.ALMOST], Verdict.ALMOST),
    ],
)
def test_the_best_attempt_wins(verdicts: list[Verdict], expected: Verdict | None) -> None:
    """Рекаст ради того и делается: исправился — значит справился."""
    outcome = srs.aggregate(verdicts)

    assert outcome.verdict == expected
    assert outcome.used is bool(verdicts)


def test_one_word_used_three_times_moves_one_box() -> None:
    """FR-SRS-1: продвигает эпизод целиком, а не каждое употребление в нём."""
    outcome = srs.aggregate([Verdict.CORRECT, Verdict.CORRECT, Verdict.CORRECT])
    after = srs.review(srs.Progress(box=1), outcome, NOW)

    assert after.box == 2
    assert after.uses == 1


class TestSelection:
    """Очередь слов по FR-SRS-4: просроченные → новые → ближайшие по времени."""

    def test_overdue_words_come_before_new_ones(self) -> None:
        new = srs.Candidate(word_id=1)
        overdue = srs.Candidate(word_id=2, box=2, next_due_at=NOW - dt.timedelta(hours=1))

        chosen = srs.select([new, overdue], now=NOW, limit=2)

        assert [item.word_id for item in chosen] == [2, 1]

    def test_new_words_come_before_words_that_are_not_due_yet(self) -> None:
        new = srs.Candidate(word_id=1)
        later = srs.Candidate(word_id=2, box=3, next_due_at=NOW + dt.timedelta(days=1))

        chosen = srs.select([later, new], now=NOW, limit=2)

        assert [item.word_id for item in chosen] == [1, 2]

    def test_the_longest_overdue_goes_first(self) -> None:
        """Иначе однажды пропущенное слово может не всплыть уже никогда."""
        recent = srs.Candidate(word_id=1, next_due_at=NOW - dt.timedelta(minutes=10))
        ancient = srs.Candidate(word_id=2, next_due_at=NOW - dt.timedelta(days=3))

        chosen = srs.select([recent, ancient], now=NOW, limit=2)

        assert [item.word_id for item in chosen] == [2, 1]

    def test_the_nearest_of_the_future_words_goes_first(self) -> None:
        soon = srs.Candidate(word_id=1, next_due_at=NOW + dt.timedelta(minutes=30))
        far = srs.Candidate(word_id=2, next_due_at=NOW + dt.timedelta(days=7))

        chosen = srs.select([far, soon], now=NOW, limit=2)

        assert [item.word_id for item in chosen] == [1, 2]

    def test_limit_cuts_the_tail(self) -> None:
        candidates = [srs.Candidate(word_id=index) for index in range(1, 6)]

        assert len(srs.select(candidates, now=NOW, limit=3)) == 3

    def test_an_empty_pool_is_not_an_error(self) -> None:
        assert srs.select([], now=NOW, limit=3) == []

"""Отбор слов в эпизод и запись результатов повторения.

Вся арифметика — в `core.srs`; здесь только чтение кандидатов и раскладывание
готового `Progress` по колонкам. Граница нужна затем, что правила SRS будут
меняться по живым данным, и менять их проще там, где нет базы.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from siga.core import srs
from siga.db.models import Word, WordProgress


async def pool(session: AsyncSession, *, pack_id: int) -> list[tuple[Word, WordProgress]]:
    """Слова пачки, которые ещё имеет смысл спрашивать.

    Выученные (FR-SRS-3) отсеиваются: они возвращаются в разговор эпизодами D5
    из архива, а не занимают место в текущей пачке.
    """
    rows = await session.execute(
        select(Word, WordProgress)
        .join(WordProgress, WordProgress.word_id == Word.id)
        .where(Word.pack_id == pack_id, WordProgress.learned_at.is_(None))
        .order_by(Word.id)
    )
    return [(word, progress) for word, progress in rows]


async def pick(
    session: AsyncSession,
    *,
    pack_id: int,
    now: dt.datetime,
    limit: int,
    rng: random.Random | None = None,
) -> list[tuple[Word, WordProgress]]:
    """Слова для одного эпизода в порядке приоритета FR-SRS-4.

    Вместе с прогрессом, а не одни слова: бокс нужен и для выбора типа
    эпизода (§5.3), и в промпте — модель по-разному заводит разговор про
    новое слово и про то, которое человек уже видел.
    """
    available = await pool(session, pack_id=pack_id)
    by_id = {word.id: (word, progress) for word, progress in available}

    chosen = srs.select(
        [
            srs.Candidate(word_id=word.id, box=progress.box, next_due_at=progress.next_due_at)
            for word, progress in available
        ],
        now=now,
        limit=limit,
        rng=rng,
    )
    return [by_id[candidate.word_id] for candidate in chosen]


async def pick_groups(
    session: AsyncSession,
    *,
    pack_id: int,
    now: dt.datetime,
    until: dt.datetime,
    groups: int,
    size: int,
    rng: random.Random | None = None,
) -> list[list[tuple[Word, WordProgress]]]:
    """Слова сразу на несколько эпизодов — для плана дня (FR-SCH-2).

    Отбирать по одному эпизоду за раз нельзя: приоритет у слов один и тот же,
    и все эпизоды дня получили бы одинаковый набор. Поэтому берём длинный
    список по тому же порядку FR-SRS-4 и режем его на группы подряд — рядом
    оказываются слова похожей зрелости, а значит и тип эпизода для группы
    выходит осмысленным.

    `until` — конец окна дня: слово, которое созреет только к ночи, сегодня не
    берём. Иначе повторение случится раньше, чем истёк его интервал, и вся
    лестница §5.7 превратится в украшение.

    Групп может выйти меньше, чем просили: слов в пачке столько, сколько есть,
    и лучше три разговора вместо четырёх, чем четвёртый ни о чём.
    """
    if groups <= 0 or size <= 0:
        return []

    available = [
        (word, progress)
        for word, progress in await pool(session, pack_id=pack_id)
        if progress.next_due_at is None or progress.next_due_at <= until
    ]
    by_id = {word.id: (word, progress) for word, progress in available}

    chosen = srs.select(
        [
            srs.Candidate(word_id=word.id, box=progress.box, next_due_at=progress.next_due_at)
            for word, progress in available
        ],
        now=now,
        limit=groups * size,
        rng=rng,
    )
    return [
        [by_id[candidate.word_id] for candidate in chosen[start : start + size]]
        for start in range(0, len(chosen), size)
    ]


async def get(session: AsyncSession, *, word_id: int) -> WordProgress | None:
    return await session.get(WordProgress, word_id)


async def apply(
    session: AsyncSession,
    *,
    outcomes: Mapping[int, srs.Outcome],
    now: dt.datetime,
) -> None:
    """Записать итоги эпизода по каждому целевому слову.

    Коммита здесь нет: итоги — часть закрытия эпизода, и разъезжаться с ним
    они не должны. Закрытие само решает, когда фиксировать.
    """
    if not outcomes:
        return

    rows = await session.scalars(
        select(WordProgress).where(WordProgress.word_id.in_(set(outcomes)))
    )
    for row in rows:
        outcome = outcomes[row.word_id]
        updated = srs.review(_snapshot(row), outcome, now)

        row.box = updated.box
        row.correct_streak = updated.correct_streak
        row.uses = updated.uses
        row.correct_uses = updated.correct_uses
        row.next_due_at = updated.next_due_at
        row.last_shown_at = now
        if updated.learned and row.learned_at is None:
            row.learned_at = now


async def release(session: AsyncSession, *, word_ids: Sequence[int], now: dt.datetime) -> None:
    """Вернуть слова в пул без штрафа — брошенный или пропущенный эпизод.

    По FR-SCH-4 и FR-SCH-6 человек тут ни при чём: до него реплика либо не
    дошла, либо дошла не вовремя. Бокс не трогаем, только освобождаем слова,
    чтобы они попали в следующий отбор.
    """
    if not word_ids:
        return

    rows = await session.scalars(
        select(WordProgress).where(WordProgress.word_id.in_(set(word_ids)))
    )
    for row in rows:
        row.next_due_at = now


def _snapshot(row: WordProgress) -> srs.Progress:
    """Строка базы как чистое состояние для `core.srs`."""
    return srs.Progress(
        box=row.box,
        correct_streak=row.correct_streak,
        uses=row.uses,
        correct_uses=row.correct_uses,
        next_due_at=row.next_due_at,
        learned=row.learned_at is not None,
    )

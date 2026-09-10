"""Очередь расписания: поставить события на день и разобрать созревшие.

Очередь в таблице, а не в памяти процесса, — решение §9: расписание переживает
деплой и расходится на несколько отправщиков. Взаимное исключение делает сама
база: `SELECT … FOR UPDATE SKIP LOCKED` отдаёт каждое событие ровно одному
воркеру, а второй молча берёт следующее вместо ожидания на блокировке.

Поэтому `claim` нарочно не коммитит: блокировка держится транзакцией, и снять
её раньше отправки значило бы разрешить двойную отправку. Коммитит вызывающий
— в тот же момент, когда меняет статус (NFR-1).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from siga.core.enums import EventStatus, EventType
from siga.db.models import ScheduledEvent


@dataclass(frozen=True, slots=True)
class Planned:
    """Событие, которое надо поставить в очередь."""

    fire_at: dt.datetime
    type: EventType
    payload: dict[str, object] = field(default_factory=dict)


async def plan(
    session: AsyncSession,
    *,
    user_id: int,
    events: Sequence[Planned],
    commit: bool = True,
) -> list[ScheduledEvent]:
    """Поставить события в очередь одной транзакцией.

    Всё разом: план дня — это набор, и наполовину расписанный день хуже
    неразписанного, потому что второй попытки в этот день уже не будет.

    `commit=False` — для отправщика: пинок ставится в очередь тем же коммитом,
    что и отметка об отправке эпизода.
    """
    if not events:
        return []

    rows = [
        ScheduledEvent(
            user_id=user_id,
            fire_at=event.fire_at,
            type=event.type,
            payload=dict(event.payload),
        )
        for event in events
    ]
    session.add_all(rows)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return rows


async def has_plan_for(
    session: AsyncSession, *, user_id: int, since: dt.datetime, until: dt.datetime
) -> bool:
    """Расписан ли уже этот отрезок — любыми событиями, в любом статусе.

    Так ночной пересчёт становится идемпотентным: воркер может проснуться в
    свой час дважды, упасть между делом и подняться заново — второго плана на
    тот же день не появится. Считаем и отправленные, и пропущенные: «день уже
    прожит» — это тоже ответ.
    """
    found = await session.scalar(
        select(ScheduledEvent.id)
        .where(
            ScheduledEvent.user_id == user_id,
            ScheduledEvent.fire_at >= since,
            ScheduledEvent.fire_at < until,
        )
        .limit(1)
    )
    return found is not None


async def claim(session: AsyncSession, *, now: dt.datetime, limit: int) -> list[ScheduledEvent]:
    """Забрать созревшие события себе до конца транзакции (FR-SCH-3).

    Порядок — по времени: если отправщик отстал, разбирать надо с самого
    раннего, иначе просрочка растёт у одних и тех же событий.
    """
    rows = await session.scalars(
        select(ScheduledEvent)
        .where(
            ScheduledEvent.status == EventStatus.PENDING,
            ScheduledEvent.fire_at <= now,
        )
        .order_by(ScheduledEvent.fire_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows)


def mark(event: ScheduledEvent, *, status: EventStatus, now: dt.datetime) -> None:
    """Отметить судьбу события. Без коммита — он на вызывающем (NFR-1).

    `attempts_count` растёт при любом исходе: событие, на котором отправщик
    крутится вхолостую, видно только по этому счётчику.
    """
    event.status = status
    event.attempts_count += 1
    event.locked_at = now


async def pending_at(
    session: AsyncSession, *, user_id: int, since: dt.datetime
) -> list[ScheduledEvent]:
    """Ещё не отправленные события человека начиная с этого момента."""
    rows = await session.scalars(
        select(ScheduledEvent)
        .where(
            ScheduledEvent.user_id == user_id,
            ScheduledEvent.status == EventStatus.PENDING,
            ScheduledEvent.fire_at >= since,
        )
        .order_by(ScheduledEvent.fire_at)
    )
    return list(rows)


def episode_id_of(event: ScheduledEvent) -> int | None:
    """Эпизод, к которому событие относится, если он у события есть."""
    value = event.payload.get("episode_id")
    return value if isinstance(value, int) else None


__all__ = [
    "Planned",
    "claim",
    "episode_id_of",
    "has_plan_for",
    "mark",
    "pending_at",
    "plan",
]

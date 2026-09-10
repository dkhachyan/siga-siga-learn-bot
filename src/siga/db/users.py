"""Запросы к таблице `users`."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from siga.core import schedule as clock
from siga.core.enums import PackStatus, UserState
from siga.db.models import Pack, User


async def get_by_tg_id(session: AsyncSession, tg_user_id: int) -> User | None:
    result: User | None = await session.scalar(select(User).where(User.tg_user_id == tg_user_id))
    return result


async def get_or_create(
    session: AsyncSession,
    *,
    tg_user_id: int,
    tg_username: str | None = None,
) -> tuple[User, bool]:
    """Вернуть пользователя, заведя запись при первом обращении.

    Через `ON CONFLICT DO NOTHING`, а не «проверил-и-вставил»: два апдейта от
    одного человека могут прийти одновременно, и гонка на уникальном индексе
    иначе уронит один из них.

    Возвращает пару (пользователь, создан_ли_сейчас).
    """
    stmt = (
        insert(User)
        .values(tg_user_id=tg_user_id, tg_username=tg_username)
        .on_conflict_do_nothing(index_elements=[User.tg_user_id])
        .returning(User)
    )
    user = await session.scalar(stmt)
    if user is not None:
        await session.commit()
        return user, True

    existing = await get_by_tg_id(session, tg_user_id)
    if existing is None:  # pragma: no cover — возможно только при удалении в этот же миг
        raise RuntimeError(f"Пользователь tg_user_id={tg_user_id} исчез между вставкой и чтением")

    if tg_username != existing.tg_username:
        existing.tg_username = tg_username
        await session.commit()

    return existing, False


async def scheduled(session: AsyncSession) -> list[User]:
    """Кому бот сейчас должен писать: есть активная пачка и нет паузы.

    Планировщик перебирает этот список целиком и уже в Python смотрит, у кого
    настал его час. Считать час средствами SQL можно, но людей у нас счётные
    единицы (§2, бот по приглашениям), а читаемость правила важнее запроса,
    который придётся расшифровывать при каждой правке окна.
    """
    rows = await session.scalars(
        select(User)
        .join(Pack, Pack.user_id == User.id)
        .where(Pack.status == PackStatus.ACTIVE, User.paused_at.is_(None))
        .order_by(User.id)
    )
    return list(rows)


async def pause(session: AsyncSession, *, user: User, now: dt.datetime) -> None:
    """Поставить расписание на паузу (FR-SCH-7). Период при этом стоит."""
    if user.paused_at is not None:
        return
    user.paused_at = now
    user.state = UserState.PAUSED
    await session.commit()


async def resume(session: AsyncSession, *, user: User, now: dt.datetime) -> int:
    """Снять паузу и вернуть периоду простоявшие дни (FR-SCH-7).

    Дни возвращаются активной пачке: пауза — это про человека, а продление —
    про то, что он не успел пройти. Возвращаем, на сколько дней сдвинулись,
    чтобы было что сказать в ответ.
    """
    if user.paused_at is None:
        return 0

    days = clock.paused_days(user.paused_at, now)
    user.paused_at = None
    user.state = UserState.ACTIVE

    pack = await session.scalar(
        select(Pack).where(Pack.user_id == user.id, Pack.status == PackStatus.ACTIVE)
    )
    if pack is not None and days:
        pack.paused_days += days
        if pack.ends_at is not None:
            pack.ends_at += dt.timedelta(days=days)

    await session.commit()
    return days

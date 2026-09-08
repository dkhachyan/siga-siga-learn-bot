"""Запросы к таблице `users`."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from siga.db.models import User


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

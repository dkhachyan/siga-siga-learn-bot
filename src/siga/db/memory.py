"""Профиль диалога в базе: прочитать, сохранить с потолком, стереть.

Форма профиля и вытеснение живут в `core.memory`; здесь одна строка на
человека и три операции над ней.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from siga.core.memory import Profile, estimate_tokens, trim
from siga.db.models import DialogMemory


async def load(session: AsyncSession, *, user_id: int) -> Profile:
    """Профиль человека. Пустой, если Ник о нём пока ничего не знает."""
    row = await session.get(DialogMemory, user_id)
    return Profile.from_dict(row.profile if row is not None else None)


async def save(
    session: AsyncSession, *, user_id: int, profile: Profile, now: dt.datetime
) -> Profile:
    """Записать профиль, ужав до потолка (FR-MEM-3).

    Ужимаем здесь, а не в промпте: иначе профиль в базе тихо рос бы дальше, и
    потолок ловил бы его заново при каждом вызове — а вместе с ним и то, что
    из него выпало, продолжало бы занимать место.
    """
    trimmed = trim(profile)
    trimmed.last_seen_at = now

    row = await session.get(DialogMemory, user_id)
    if row is None:
        row = DialogMemory(user_id=user_id)
        session.add(row)

    row.profile = trimmed.to_dict()
    row.tokens_estimate = estimate_tokens(trimmed)
    row.updated_at = now

    await session.commit()
    return trimmed


async def touch(session: AsyncSession, *, user_id: int, now: dt.datetime) -> None:
    """Отметить, что человек только что говорил, ничего не добавляя.

    Нужно ради `last_seen_at`: по нему считается пауза между репликами
    (FR-MEM-5), и она должна быть верной даже после хода, о котором нечего
    было запомнить.
    """
    profile = await load(session, user_id=user_id)
    await save(session, user_id=user_id, profile=profile, now=now)


async def clear(session: AsyncSession, *, user_id: int, now: dt.datetime) -> None:
    """Стереть всё, что Ник помнит (FR-MEM-4).

    Строка остаётся пустой, а не удаляется: `updated_at` — свидетельство того,
    что человек воспользовался правом на забвение, и терять его не стоит.
    """
    row = await session.get(DialogMemory, user_id)
    if row is None:
        return
    row.profile = {}
    row.tokens_estimate = 0
    row.updated_at = now
    await session.commit()

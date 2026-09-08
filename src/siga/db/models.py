"""Модели SQLAlchemy.

Этап 0 заводит только `users`. Остальные таблицы из §7 спецификации
(`packs`, `words`, `episodes`, `turns`, `scheduled_events`, …) приезжают
на своих этапах, каждая — отдельной миграцией.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Time,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from siga.core.enums import Level, UserState
from siga.db.base import Base

DEFAULT_TZ = "Asia/Nicosia"
DEFAULT_WINDOW_START = dt.time(9, 0)
DEFAULT_WINDOW_END = dt.time(21, 0)
DEFAULT_EPISODES_PER_DAY = 4


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "episodes_per_day BETWEEN 1 AND 12",
            name="episodes_per_day_range",
        ),
        CheckConstraint(
            "state IN ('new', 'onboarding', 'active', 'paused', 'finished')",
            name="state_known",
        ),
        CheckConstraint("level IN ('A1', 'A2', 'B1', 'B2')", name="level_known"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(32))

    tz: Mapped[str] = mapped_column(String(64), default=DEFAULT_TZ, server_default=DEFAULT_TZ)
    level: Mapped[str] = mapped_column(String(2), default=Level.A1, server_default=Level.A1)

    window_start: Mapped[dt.time] = mapped_column(
        Time, default=DEFAULT_WINDOW_START, server_default="09:00"
    )
    window_end: Mapped[dt.time] = mapped_column(
        Time, default=DEFAULT_WINDOW_END, server_default="21:00"
    )
    episodes_per_day: Mapped[int] = mapped_column(
        SmallInteger, default=DEFAULT_EPISODES_PER_DAY, server_default="4"
    )

    state: Mapped[str] = mapped_column(
        String(16), default=UserState.NEW, server_default=UserState.NEW
    )

    invited_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<User id={self.id} tg={self.tg_user_id} state={self.state}>"

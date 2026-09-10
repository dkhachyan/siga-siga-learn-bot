"""Промежуток между разговорами: users.min_gap_minutes.

Было константой в ядре (`DEFAULT_MIN_GAP`), стало настройкой человека
(FR-SCH-8). Сорок пять минут остаются значением по умолчанию, так что для тех,
кто ничего не трогал, расписание не меняется.

`server_default` обязателен: колонка `NOT NULL`, а строки в `users` уже есть —
без значения по умолчанию Postgres откажется её добавлять.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-10 11:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("min_gap_minutes", sa.SmallInteger(), nullable=False, server_default="45"),
    )
    op.create_check_constraint(
        "min_gap_minutes_range", "users", "min_gap_minutes BETWEEN 15 AND 480"
    )


def downgrade() -> None:
    op.drop_constraint("min_gap_minutes_range", "users", type_="check")
    op.drop_column("users", "min_gap_minutes")

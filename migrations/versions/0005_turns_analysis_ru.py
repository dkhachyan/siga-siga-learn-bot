"""Разбор хода: turns.analysis_ru.

Объяснение по-русски приезжает вместе с репликой (§8.3), но в чат не уходит,
пока человек не нажал `🔍 Разбор`. Значит, его надо где-то держать — иначе
кнопка после закрытия эпизода покажет пустоту, а FR-CHK-2 требует разбора по
всему эпизоду целиком.

Отдельной колонкой, а не полем в `corrections`: это связный текст, а не запись
о правке, и искать его придётся по ходам, а не внутри JSON.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-09 22:33:33.749036
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("analysis_ru", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("turns", "analysis_ru")

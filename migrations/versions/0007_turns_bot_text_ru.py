"""Перевод реплики: turns.bot_text_ru.

Кэш маршрута `R6` (FR-CHK-7). Перевод спрашивается у модели по нажатию кнопки
и складывается сюда: второе нажатие той же кнопки должно быть бесплатным и
мгновенным, а кнопка живёт в переписке вечно.

Nullable без значения по умолчанию нарочно: `NULL` здесь значит «ещё не
переводили», и это ровно то условие, по которому обработчик решает, идти ли к
модели. Пустая строка такое различие стёрла бы.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-10 09:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("bot_text_ru", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("turns", "bot_text_ru")

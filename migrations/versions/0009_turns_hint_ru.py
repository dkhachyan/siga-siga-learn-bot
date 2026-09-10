"""Подсказка «что ответить»: turns.hint_ru.

Кэш маршрута `R7` (FR-CHK-8). Варианты ответа спрашиваются у модели по нажатию
кнопки `💡 Что ответить` и складываются сюда готовым текстом: второе нажатие
той же кнопки должно быть бесплатным, а кнопка живёт в переписке вечно.

Nullable без значения по умолчанию, ровно как у соседнего `bot_text_ru` (0007):
`NULL` значит «не спрашивали», и это то самое условие, по которому обработчик
решает, идти ли к модели. Пустая строка различие стёрла бы, и кнопка навсегда
показывала бы пустоту.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-10 16:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("hint_ru", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("turns", "hint_ru")

"""Разговор по теме: эпизоды без целевых слов.

FR-EP-9 добавляет тип эпизода `T` (TOPIC) и разрешает пустой
`target_word_ids`: человек может попросить разговор «просто на тему», не
держа пачки. Меняются два CHECK-ограничения `episodes`; их SQL живёт в
`siga.db.models` (`INTENT_KNOWN_SQL`, `TARGET_WORD_IDS_SIZE_SQL`) — та же
строка используется и в ORM, чтобы `alembic check` сверял базу и код по
одному источнику.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-12 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from siga.db.models import INTENT_KNOWN_SQL, TARGET_WORD_IDS_SIZE_SQL

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("intent_known", "episodes", type_="check")
    op.create_check_constraint("intent_known", "episodes", INTENT_KNOWN_SQL)
    op.drop_constraint("target_word_ids_size", "episodes", type_="check")
    op.create_check_constraint("target_word_ids_size", "episodes", TARGET_WORD_IDS_SIZE_SQL)


def downgrade() -> None:
    # Возврат запрещает и тип T, и пустой список слов; если в базе уже есть
    # тематические эпизоды, откат честно упрётся в ограничение — и это правильно.
    op.drop_constraint("target_word_ids_size", "episodes", type_="check")
    op.create_check_constraint(
        "target_word_ids_size", "episodes", "array_length(target_word_ids, 1) BETWEEN 1 AND 3"
    )
    op.drop_constraint("intent_known", "episodes", type_="check")
    op.create_check_constraint(
        "intent_known",
        "episodes",
        "intent IN ('D1', 'D2', 'D3', 'D4', 'D5')",
    )

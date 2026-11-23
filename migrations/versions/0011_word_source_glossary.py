"""Источник слова «glossary»: пачки из уроков учебника.

FR-IMP-12 добавляет к источникам слова встроенный словарь уроков. Меняются
два CHECK-ограничения — `words.source_known` и `imports.source_type_known`;
их SQL живёт в `siga.db.models` (`WORD_SOURCE_KNOWN_SQL`,
`IMPORT_SOURCE_KNOWN_SQL`), чтобы `alembic check` сверял базу и код по
одному источнику.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-23 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from siga.db.models import IMPORT_SOURCE_KNOWN_SQL, WORD_SOURCE_KNOWN_SQL

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = "'text', 'photo', 'file', 'manual'"


def upgrade() -> None:
    op.drop_constraint("source_known", "words", type_="check")
    op.create_check_constraint("source_known", "words", WORD_SOURCE_KNOWN_SQL)
    op.drop_constraint("source_type_known", "imports", type_="check")
    op.create_check_constraint("source_type_known", "imports", IMPORT_SOURCE_KNOWN_SQL)


def downgrade() -> None:
    # Если пачки из уроков уже заведены, откат честно упрётся в ограничение.
    op.drop_constraint("source_type_known", "imports", type_="check")
    op.create_check_constraint("source_type_known", "imports", f"source_type IN ({_OLD_VALUES})")
    op.drop_constraint("source_known", "words", type_="check")
    op.create_check_constraint("source_known", "words", f"source IN ({_OLD_VALUES})")

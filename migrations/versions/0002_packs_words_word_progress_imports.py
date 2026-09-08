"""Импорт слов: imports, packs, words, word_progress.

Таблицы этапа 1 — всё, что нужно, чтобы принять список слов, показать экран
подтверждения и завести пачку. `episodes`, `turns` и расписание приедут позже.

Заодно правится дефолт окна в `users`: этап 0 поставил 09:00–21:00, а §5.1
спецификации говорит 10:00–20:00. Меняется только `server_default`, то есть
поведение для новых пользователей; существующие строки не трогаем — человек
мог настроить окно сам, и переписывать его выбор нельзя.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-08 16:16:22.824427
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "imports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("word_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_type IN ('text', 'photo', 'file', 'manual')",
            name=op.f("ck_imports_source_type_known"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_imports_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_imports")),
    )
    op.create_index(op.f("ix_imports_user_id"), "imports", ["user_id"], unique=False)
    op.create_table(
        "packs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("import_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("period_days", sa.SmallInteger(), server_default="14", nullable=False),
        sa.Column("paused_days", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')", name=op.f("ck_packs_status_known")
        ),
        sa.CheckConstraint("paused_days >= 0", name=op.f("ck_packs_paused_days_non_negative")),
        sa.CheckConstraint("period_days BETWEEN 3 AND 60", name=op.f("ck_packs_period_days_range")),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["imports.id"],
            name=op.f("fk_packs_import_id_imports"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_packs_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_packs")),
    )
    op.create_index("ix_packs_user_id_status", "packs", ["user_id", "status"], unique=False)
    op.create_table(
        "words",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("pack_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("lemma", sa.String(length=64), nullable=False),
        sa.Column("lemma_accented", sa.String(length=64), nullable=False),
        sa.Column("translation_ru", sa.String(length=160), nullable=True),
        sa.Column("translation_model", sa.String(length=160), nullable=True),
        sa.Column("pos", sa.String(length=16), nullable=True),
        sa.Column("article", sa.String(length=8), nullable=True),
        sa.Column("gender", sa.String(length=1), nullable=True),
        sa.Column("verb_form", sa.String(length=64), nullable=True),
        sa.Column(
            "examples", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "gender IS NULL OR gender IN ('m', 'f', 'n')", name=op.f("ck_words_gender_known")
        ),
        sa.CheckConstraint(
            "pos IS NULL OR pos IN ('noun', 'verb', 'adjective', 'adverb', 'phrase', 'other')",
            name=op.f("ck_words_pos_known"),
        ),
        sa.CheckConstraint(
            "source IN ('text', 'photo', 'file', 'manual')", name=op.f("ck_words_source_known")
        ),
        sa.ForeignKeyConstraint(
            ["pack_id"], ["packs.id"], name=op.f("fk_words_pack_id_packs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_words_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_words")),
    )
    op.create_index(op.f("ix_words_pack_id"), "words", ["pack_id"], unique=False)
    op.create_index("ix_words_user_id_lemma", "words", ["user_id", "lemma"], unique=False)
    op.create_table(
        "word_progress",
        sa.Column("word_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("box", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("correct_streak", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("uses", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("correct_uses", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("last_shown_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("learned_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("box BETWEEN 0 AND 5", name=op.f("ck_word_progress_box_range")),
        sa.CheckConstraint(
            "correct_streak >= 0", name=op.f("ck_word_progress_correct_streak_non_negative")
        ),
        sa.CheckConstraint(
            "correct_uses <= uses", name=op.f("ck_word_progress_correct_uses_within_uses")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_word_progress_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["word_id"],
            ["words.id"],
            name=op.f("fk_word_progress_word_id_words"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("word_id", name=op.f("pk_word_progress")),
    )
    op.create_index(
        "ix_word_progress_user_id_next_due_at",
        "word_progress",
        ["user_id", "next_due_at"],
        unique=False,
    )
    op.alter_column(
        "users",
        "window_start",
        existing_type=postgresql.TIME(),
        server_default="10:00",
        existing_nullable=False,
    )
    op.alter_column(
        "users",
        "window_end",
        existing_type=postgresql.TIME(),
        server_default="20:00",
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "window_end",
        existing_type=postgresql.TIME(),
        server_default=sa.text("'21:00:00'::time without time zone"),
        existing_nullable=False,
    )
    op.alter_column(
        "users",
        "window_start",
        existing_type=postgresql.TIME(),
        server_default=sa.text("'09:00:00'::time without time zone"),
        existing_nullable=False,
    )
    op.drop_index("ix_word_progress_user_id_next_due_at", table_name="word_progress")
    op.drop_table("word_progress")
    op.drop_index("ix_words_user_id_lemma", table_name="words")
    op.drop_index(op.f("ix_words_pack_id"), table_name="words")
    op.drop_table("words")
    op.drop_index("ix_packs_user_id_status", table_name="packs")
    op.drop_table("packs")
    op.drop_index(op.f("ix_imports_user_id"), table_name="imports")
    op.drop_table("imports")

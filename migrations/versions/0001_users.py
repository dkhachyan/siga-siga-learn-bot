"""users

Revision ID: 0001
Revises:
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_username", sa.String(length=32), nullable=True),
        sa.Column("tz", sa.String(length=64), server_default="Asia/Nicosia", nullable=False),
        sa.Column("level", sa.String(length=2), server_default="A1", nullable=False),
        sa.Column("window_start", sa.Time(), server_default="09:00", nullable=False),
        sa.Column("window_end", sa.Time(), server_default="21:00", nullable=False),
        sa.Column("episodes_per_day", sa.SmallInteger(), server_default="4", nullable=False),
        sa.Column("state", sa.String(length=16), server_default="new", nullable=False),
        sa.Column("invited_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "episodes_per_day BETWEEN 1 AND 12",
            name=op.f("ck_users_episodes_per_day_range"),
        ),
        sa.CheckConstraint("level IN ('A1', 'A2', 'B1', 'B2')", name=op.f("ck_users_level_known")),
        sa.CheckConstraint(
            "state IN ('new', 'onboarding', 'active', 'paused', 'finished')",
            name=op.f("ck_users_state_known"),
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name=op.f("fk_users_invited_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    # уникальность tg_user_id держит индекс, а не отдельный constraint —
    # на него опирается ON CONFLICT в siga.db.users.get_or_create
    op.create_index(op.f("ix_users_tg_user_id"), "users", ["tg_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_tg_user_id"), table_name="users")
    op.drop_table("users")

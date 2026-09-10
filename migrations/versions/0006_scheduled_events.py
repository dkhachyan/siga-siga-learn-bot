"""Расписание: scheduled_events и users.paused_at.

Очередь намерений «написать человеку в такой-то момент» (§5.6). Держим её в
базе, а не в памяти процесса: расписание должно переживать деплой и
расходиться на несколько отправщиков — `FOR UPDATE SKIP LOCKED` по индексу
`(status, fire_at)` даёт это без брокера (§9).

`users.paused_at` — момент, с которого расписание стоит (FR-SCH-7). У человека,
а не у пачки: на паузу ставят себя. По нему при снятии паузы считается, на
сколько дней продлить период.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-09 23:11:34.886500
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'skipped', 'expired')",
            name=op.f("ck_scheduled_events_event_status_known"),
        ),
        sa.CheckConstraint(
            "type IN ('episode', 'nudge')",
            name=op.f("ck_scheduled_events_event_type_known"),
        ),
        sa.CheckConstraint(
            "attempts_count >= 0",
            name=op.f("ck_scheduled_events_attempts_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_scheduled_events_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_events")),
    )
    op.create_index(
        "ix_scheduled_events_status_fire_at",
        "scheduled_events",
        ["status", "fire_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scheduled_events_user_id"), "scheduled_events", ["user_id"], unique=False
    )
    op.add_column("users", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "paused_at")
    op.drop_index(op.f("ix_scheduled_events_user_id"), table_name="scheduled_events")
    op.drop_index("ix_scheduled_events_status_fire_at", table_name="scheduled_events")
    op.drop_table("scheduled_events")

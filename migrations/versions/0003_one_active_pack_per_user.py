"""Одна активная пачка на человека.

Спецификация везде говорит про активную пачку в единственном числе (§5.8,
§5.9), но в базе запрета не было — и `/add` при активной пачке заводил вторую.
Частичный уникальный индекс делает это невозможным на всех путях, а не только
в том хендлере, где проверку не забыли поставить.

Перед индексом приходится прибрать данные: у кого активных пачек несколько,
все, кроме самой свежей, уходят в архив. Иначе `CREATE UNIQUE INDEX` упадёт.
Свежая — по `started_at`, а при равенстве по `id`: это ровно тот порядок,
которым `packs.get_active` уже выбирает пачку для показа, так что человек
увидит в `/pack` то же, что и до миграции.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09 22:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE packs SET status = 'archived'
        WHERE status = 'active'
          AND id NOT IN (
              SELECT DISTINCT ON (user_id) id FROM packs
              WHERE status = 'active'
              ORDER BY user_id, started_at DESC NULLS LAST, id DESC
          )
        """
    )
    op.create_index(
        "uq_packs_active_per_user",
        "packs",
        ["user_id"],
        unique=True,
        postgresql_where="status = 'active'",
    )


def downgrade() -> None:
    # Пачки, убранные в архив при накатывании, остаются в архиве: какая из них
    # была активной до миграции, в базе больше не записано, а угадывать —
    # значит вернуть человеку в работу пачку, которую он уже не видел.
    op.drop_index("uq_packs_active_per_user", table_name="packs")

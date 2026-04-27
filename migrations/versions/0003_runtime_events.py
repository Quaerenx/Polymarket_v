from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_runtime_events"
down_revision = "0002_runtime_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("event_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_runtime_events_ts", "runtime_events", ["ts"], unique=False)
    op.create_index(
        "ix_runtime_events_category_ts",
        "runtime_events",
        ["category", "ts"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_events_event_type_ts",
        "runtime_events",
        ["event_type", "ts"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_events_event_type_ts", table_name="runtime_events")
    op.drop_index("ix_runtime_events_category_ts", table_name="runtime_events")
    op.drop_index("ix_runtime_events_ts", table_name="runtime_events")
    op.drop_table("runtime_events")

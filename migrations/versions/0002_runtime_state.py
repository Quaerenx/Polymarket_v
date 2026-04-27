from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_runtime_state"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_runtime_state_key", "runtime_state", ["key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runtime_state_key", table_name="runtime_state")
    op.drop_table("runtime_state")

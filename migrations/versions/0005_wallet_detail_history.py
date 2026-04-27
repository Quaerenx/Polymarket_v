from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_wallet_detail_history"
down_revision = "0004_wallet_lb_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_detail_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proxy_wallet", sa.String(length=255), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_positions_json", sa.JSON(), nullable=True),
        sa.Column("trades_json", sa.JSON(), nullable=True),
        sa.Column("total_value_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("proxy_wallet", "observed_at"),
    )
    op.create_index(
        "ix_wallet_detail_snapshots_proxy_wallet_observed_at",
        "wallet_detail_snapshots",
        ["proxy_wallet", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wallet_detail_snapshots_proxy_wallet_observed_at",
        table_name="wallet_detail_snapshots",
    )
    op.drop_table("wallet_detail_snapshots")

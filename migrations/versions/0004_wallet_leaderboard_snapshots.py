from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_wallet_lb_snapshots"
down_revision = "0003_runtime_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_leaderboard_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proxy_wallet", sa.String(length=255), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", sa.String(length=255), nullable=False),
        sa.Column("time_period", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("rank", sa.String(length=64), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("proxy_wallet", "category", "time_period", "observed_at"),
    )
    op.create_index(
        "ix_wallet_leaderboard_snapshots_proxy_wallet",
        "wallet_leaderboard_snapshots",
        ["proxy_wallet"],
        unique=False,
    )
    op.create_index(
        "ix_wallet_leaderboard_snapshots_category_observed_at",
        "wallet_leaderboard_snapshots",
        ["category", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wallet_leaderboard_snapshots_category_observed_at",
        table_name="wallet_leaderboard_snapshots",
    )
    op.drop_index(
        "ix_wallet_leaderboard_snapshots_proxy_wallet",
        table_name="wallet_leaderboard_snapshots",
    )
    op.drop_table("wallet_leaderboard_snapshots")

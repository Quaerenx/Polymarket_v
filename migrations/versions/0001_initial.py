from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "markets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("condition_id", sa.String(length=255), nullable=False),
        sa.Column("market_id", sa.String(length=255), nullable=True),
        sa.Column("event_id", sa.String(length=255), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("slug", sa.String(length=255), nullable=True),
        sa.Column("end_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("closed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("neg_risk", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("min_tick_size", sa.Float(), nullable=True),
        sa.Column("min_order_size", sa.Float(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("condition_id"),
    )
    op.create_index("ix_markets_condition_id", "markets", ["condition_id"], unique=False)
    op.create_index("ix_markets_active", "markets", ["active"], unique=False)

    op.create_table(
        "market_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("condition_id", sa.String(length=255), nullable=False),
        sa.Column("token_id", sa.String(length=255), nullable=False),
        sa.Column("outcome", sa.String(length=255), nullable=True),
        sa.Column("side_label", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_id"),
    )
    op.create_index(
        "ix_market_tokens_condition_id", "market_tokens", ["condition_id"], unique=False
    )

    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("condition_id", sa.String(length=255), nullable=False),
        sa.Column("token_id", sa.String(length=255), nullable=False),
        sa.Column("best_bid", sa.Float(), nullable=True),
        sa.Column("best_ask", sa.Float(), nullable=True),
        sa.Column("midpoint", sa.Float(), nullable=True),
        sa.Column("spread", sa.Float(), nullable=True),
        sa.Column("last_trade_price", sa.Float(), nullable=True),
        sa.Column("liquidity_score", sa.Float(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("token_id", "ts"),
    )
    op.create_index(
        "ix_market_snapshots_token_id_ts", "market_snapshots", ["token_id", "ts"], unique=False
    )
    op.create_index(
        "ix_market_snapshots_condition_id_ts",
        "market_snapshots",
        ["condition_id", "ts"],
        unique=False,
    )

    op.create_table(
        "wallets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proxy_wallet", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("proxy_wallet"),
    )
    op.create_index("ix_wallets_proxy_wallet", "wallets", ["proxy_wallet"], unique=False)

    op.create_table(
        "wallet_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proxy_wallet", sa.String(length=255), nullable=False),
        sa.Column("condition_id", sa.String(length=255), nullable=False),
        sa.Column("token_id", sa.String(length=255), nullable=True),
        sa.Column("outcome", sa.String(length=255), nullable=True),
        sa.Column("size", sa.Float(), nullable=True),
        sa.Column("avg_price", sa.Float(), nullable=True),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("current_value", sa.Float(), nullable=True),
        sa.Column("cash_pnl", sa.Float(), nullable=True),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.Column("total_pnl", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_wallet_positions_proxy_wallet", "wallet_positions", ["proxy_wallet"], unique=False
    )
    op.create_index(
        "ix_wallet_positions_condition_id", "wallet_positions", ["condition_id"], unique=False
    )

    op.create_table(
        "wallet_activity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proxy_wallet", sa.String(length=255), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("condition_id", sa.String(length=255), nullable=False),
        sa.Column("token_id", sa.String(length=255), nullable=True),
        sa.Column("side", sa.String(length=64), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("size", sa.Float(), nullable=True),
        sa.Column("tx_hash", sa.String(length=255), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_wallet_activity_proxy_wallet_ts",
        "wallet_activity",
        ["proxy_wallet", "ts"],
        unique=False,
    )
    op.create_index(
        "ix_wallet_activity_condition_id_ts",
        "wallet_activity",
        ["condition_id", "ts"],
        unique=False,
    )
    op.create_index(
        "ix_wallet_activity_token_id_ts", "wallet_activity", ["token_id", "ts"], unique=False
    )

    op.create_table(
        "wallet_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("proxy_wallet", sa.String(length=255), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", sa.String(length=255), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("roi", sa.Float(), nullable=True),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("closed_market_count", sa.Integer(), nullable=True),
        sa.Column("trade_count", sa.Integer(), nullable=True),
        sa.Column("clv_1h", sa.Float(), nullable=True),
        sa.Column("clv_6h", sa.Float(), nullable=True),
        sa.Column("clv_24h", sa.Float(), nullable=True),
        sa.Column("max_drawdown", sa.Float(), nullable=True),
        sa.Column("profit_concentration", sa.Float(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("raw_metrics_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint("proxy_wallet", "as_of", "category"),
    )
    op.create_index(
        "ix_wallet_scores_proxy_wallet", "wallet_scores", ["proxy_wallet"], unique=False
    )
    op.create_index("ix_wallet_scores_category", "wallet_scores", ["category"], unique=False)

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("condition_id", sa.String(length=255), nullable=False),
        sa.Column("token_id", sa.String(length=255), nullable=False),
        sa.Column("direction", sa.String(length=64), nullable=False),
        sa.Column("market_midpoint", sa.Float(), nullable=True),
        sa.Column("effective_entry_price", sa.Float(), nullable=True),
        sa.Column("fair_prob", sa.Float(), nullable=True),
        sa.Column("edge_bps", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_wallet_count", sa.Integer(), nullable=False),
        sa.Column("source_wallets_json", sa.JSON(), nullable=True),
        sa.Column("reason_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_signals_token_id_ts", "signals", ["token_id", "ts"], unique=False)

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("condition_id", sa.String(length=255), nullable=False),
        sa.Column("token_id", sa.String(length=255), nullable=False),
        sa.Column("side", sa.String(length=64), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("size", sa.Float(), nullable=True),
        sa.Column("order_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.Integer(), nullable=True),
        sa.Column("external_order_id", sa.String(length=255), nullable=True),
        sa.Column("reason_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_orders_mode_status", "orders", ["mode", "status"], unique=False)

    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("size", sa.Float(), nullable=True),
        sa.Column("fee", sa.Float(), nullable=True),
        sa.Column("tx_hash", sa.String(length=255), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_fills_order_id", "fills", ["order_id"], unique=False)

    op.create_table(
        "paper_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("condition_id", sa.String(length=255), nullable=False),
        sa.Column("token_id", sa.String(length=255), nullable=False),
        sa.Column("side", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Float(), nullable=False),
        sa.Column("avg_price", sa.Float(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_paper_positions_token_id", "paper_positions", ["token_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_paper_positions_token_id", table_name="paper_positions")
    op.drop_table("paper_positions")
    op.drop_index("ix_fills_order_id", table_name="fills")
    op.drop_table("fills")
    op.drop_index("ix_orders_mode_status", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_signals_token_id_ts", table_name="signals")
    op.drop_table("signals")
    op.drop_index("ix_wallet_scores_category", table_name="wallet_scores")
    op.drop_index("ix_wallet_scores_proxy_wallet", table_name="wallet_scores")
    op.drop_table("wallet_scores")
    op.drop_index("ix_wallet_activity_token_id_ts", table_name="wallet_activity")
    op.drop_index("ix_wallet_activity_condition_id_ts", table_name="wallet_activity")
    op.drop_index("ix_wallet_activity_proxy_wallet_ts", table_name="wallet_activity")
    op.drop_table("wallet_activity")
    op.drop_index("ix_wallet_positions_condition_id", table_name="wallet_positions")
    op.drop_index("ix_wallet_positions_proxy_wallet", table_name="wallet_positions")
    op.drop_table("wallet_positions")
    op.drop_index("ix_wallets_proxy_wallet", table_name="wallets")
    op.drop_table("wallets")
    op.drop_index("ix_market_snapshots_condition_id_ts", table_name="market_snapshots")
    op.drop_index("ix_market_snapshots_token_id_ts", table_name="market_snapshots")
    op.drop_table("market_snapshots")
    op.drop_index("ix_market_tokens_condition_id", table_name="market_tokens")
    op.drop_table("market_tokens")
    op.drop_index("ix_markets_active", table_name="markets")
    op.drop_index("ix_markets_condition_id", table_name="markets")
    op.drop_table("markets")

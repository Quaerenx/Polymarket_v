from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base declarative model."""


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    market_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    neg_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    min_tick_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_order_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class MarketToken(Base):
    __tablename__ = "market_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(255), index=True)
    token_id: Mapped[str] = mapped_column(String(255), unique=True)
    outcome: Mapped[str | None] = mapped_column(String(255), nullable=True)
    side_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint("token_id", "ts"),
        Index("ix_market_snapshots_token_id_ts", "token_id", "ts"),
        Index("ix_market_snapshots_condition_id_ts", "condition_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    condition_id: Mapped[str] = mapped_column(String(255))
    token_id: Mapped[str] = mapped_column(String(255))
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    midpoint: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_trade_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proxy_wallet: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class WalletLeaderboardSnapshot(Base):
    __tablename__ = "wallet_leaderboard_snapshots"
    __table_args__ = (
        UniqueConstraint("proxy_wallet", "category", "time_period", "observed_at"),
        Index("ix_wallet_leaderboard_snapshots_proxy_wallet", "proxy_wallet"),
        Index(
            "ix_wallet_leaderboard_snapshots_category_observed_at",
            "category",
            "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proxy_wallet: Mapped[str] = mapped_column(String(255))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    category: Mapped[str] = mapped_column(String(255))
    time_period: Mapped[str] = mapped_column(String(64))
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class WalletDetailSnapshot(Base):
    __tablename__ = "wallet_detail_snapshots"
    __table_args__ = (
        UniqueConstraint("proxy_wallet", "observed_at"),
        Index("ix_wallet_detail_snapshots_proxy_wallet_observed_at", "proxy_wallet", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proxy_wallet: Mapped[str] = mapped_column(String(255))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_positions_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    trades_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    total_value_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class WalletPosition(Base):
    __tablename__ = "wallet_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proxy_wallet: Mapped[str] = mapped_column(String(255), index=True)
    condition_id: Mapped[str] = mapped_column(String(255), index=True)
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class WalletActivity(Base):
    __tablename__ = "wallet_activity"
    __table_args__ = (
        Index("ix_wallet_activity_proxy_wallet_ts", "proxy_wallet", "ts"),
        Index("ix_wallet_activity_condition_id_ts", "condition_id", "ts"),
        Index("ix_wallet_activity_token_id_ts", "token_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proxy_wallet: Mapped[str] = mapped_column(String(255))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    condition_id: Mapped[str] = mapped_column(String(255))
    token_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    side: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class WalletScore(Base):
    __tablename__ = "wallet_scores"
    __table_args__ = (
        UniqueConstraint("proxy_wallet", "as_of", "category"),
        Index("ix_wallet_scores_proxy_wallet", "proxy_wallet"),
        Index("ix_wallet_scores_category", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proxy_wallet: Mapped[str] = mapped_column(String(255))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    category: Mapped[str] = mapped_column(String(255))
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi: Mapped[float | None] = mapped_column(Float, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_market_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trade_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clv_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    clv_6h: Mapped[float | None] = mapped_column(Float, nullable=True)
    clv_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_concentration: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    condition_id: Mapped[str] = mapped_column(String(255))
    token_id: Mapped[str] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(64))
    market_midpoint: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fair_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_wallet_count: Mapped[int] = mapped_column(Integer, default=0)
    source_wallets_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    reason_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="new")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_mode_status", "mode", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    mode: Mapped[str] = mapped_column(String(32))
    condition_id: Mapped[str] = mapped_column(String(255))
    token_id: Mapped[str] = mapped_column(String(255))
    side: Mapped[str] = mapped_column(String(64))
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64))
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class RuntimeEvent(Base):
    __tablename__ = "runtime_events"
    __table_args__ = (
        Index("ix_runtime_events_ts", "ts"),
        Index("ix_runtime_events_category_ts", "category", "ts"),
        Index("ix_runtime_events_event_type_ts", "event_type", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    category: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(32), default="info")
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class RuntimeState(Base):
    __tablename__ = "runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    state_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (Index("ix_paper_positions_token_id", "token_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    condition_id: Mapped[str] = mapped_column(String(255))
    token_id: Mapped[str] = mapped_column(String(255))
    side: Mapped[str] = mapped_column(String(64))
    size: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

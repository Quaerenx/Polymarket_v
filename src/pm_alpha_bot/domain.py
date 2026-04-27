from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    """Shared base model for normalized domain records."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class NormalizedMarketToken(DomainModel):
    condition_id: str
    token_id: str
    outcome: str | None = None
    side_label: str | None = None


class NormalizedMarket(DomainModel):
    condition_id: str
    question: str
    market_id: str | None = None
    event_id: str | None = None
    category: str | None = None
    slug: str | None = None
    end_date: datetime | None = None
    active: bool = True
    closed: bool = False
    neg_risk: bool = False
    min_tick_size: float | None = None
    min_order_size: float | None = None
    tokens: list[NormalizedMarketToken] = Field(default_factory=list)
    raw_json: dict[str, Any] | None = None


class LeaderboardEntry(DomainModel):
    proxy_wallet: str = Field(alias="proxyWallet")
    username: str | None = Field(default=None, alias="userName")
    pnl: float | None = None
    volume: float | None = Field(default=None, alias="vol")
    rank: str | None = None
    source: str = "leaderboard"
    category: str | None = None
    raw_json: dict[str, Any] | None = None


class WalletPositionRecord(DomainModel):
    proxy_wallet: str
    condition_id: str
    token_id: str | None = None
    outcome: str | None = None
    size: float | None = None
    avg_price: float | None = None
    current_price: float | None = None
    current_value: float | None = None
    cash_pnl: float | None = None
    realized_pnl: float | None = None
    total_pnl: float | None = None
    updated_at: datetime
    raw_json: dict[str, Any] | None = None


class WalletActivityRecord(DomainModel):
    proxy_wallet: str
    ts: datetime
    condition_id: str
    token_id: str | None = None
    side: str | None = None
    price: float | None = None
    size: float | None = None
    tx_hash: str | None = None
    raw_json: dict[str, Any] | None = None


class OrderBookSnapshot(DomainModel):
    ts: datetime
    condition_id: str
    token_id: str
    best_bid: float | None = None
    best_ask: float | None = None
    midpoint: float | None = None
    spread: float | None = None
    last_trade_price: float | None = None
    liquidity_score: float | None = None
    raw_json: dict[str, Any] | None = None


class WalletScoreRecord(DomainModel):
    proxy_wallet: str
    as_of: datetime
    category: str
    pnl: float | None = None
    roi: float | None = None
    win_rate: float | None = None
    closed_market_count: int | None = None
    trade_count: int | None = None
    clv_1h: float | None = None
    clv_6h: float | None = None
    clv_24h: float | None = None
    max_drawdown: float | None = None
    profit_concentration: float | None = None
    score: float | None = None
    raw_metrics_json: dict[str, Any] | None = None


class SignalRecord(DomainModel):
    ts: datetime
    condition_id: str
    token_id: str
    direction: str
    market_midpoint: float | None = None
    effective_entry_price: float | None = None
    fair_prob: float | None = None
    edge_bps: float | None = None
    confidence: float | None = None
    source_wallet_count: int = 0
    source_wallets_json: list[str] = Field(default_factory=list)
    reason_json: dict[str, Any] = Field(default_factory=dict)
    status: str = "new"


class PaperOrderRequest(DomainModel):
    condition_id: str
    token_id: str
    side: str
    price: float
    size: float
    order_type: str = "limit"
    signal_id: int | None = None
    reason_json: dict[str, Any] = Field(default_factory=dict)


class GeoblockStatus(DomainModel):
    blocked: bool
    raw_json: dict[str, Any] | None = None


class RiskState(DomainModel):
    capital: float
    cash: float
    equity: float
    daily_loss_pct: float
    total_drawdown_pct: float
    market_exposure_pct: dict[str, float]
    category_exposure_pct: dict[str, float]
    condition_exposure_pct: dict[str, float] = Field(default_factory=dict)
    event_exposure_pct: dict[str, float] = Field(default_factory=dict)
    kill_switch_reason: str | None = None


class PreparedLiveOrder(DomainModel):
    signal_id: int
    condition_id: str
    token_id: str
    side: str
    price: float
    size: float
    tick_size: str | None = None
    neg_risk: bool | None = None
    order_type: str = "GTC"
    post_only: bool = True
    expiration: int = 0
    reason_json: dict[str, Any] = Field(default_factory=dict)


class LiveOrderSubmission(DomainModel):
    success: bool
    external_order_id: str | None = None
    status: str | None = None
    order_type: str = "GTC"
    post_only: bool = True
    taking_amount: str | None = None
    making_amount: str | None = None
    trade_ids: list[str] = Field(default_factory=list)
    transaction_hashes: list[str] = Field(default_factory=list)
    error_msg: str | None = None
    raw_json: dict[str, Any] | None = None


class LiveOpenOrder(DomainModel):
    external_order_id: str
    status: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    side: str | None = None
    original_size: float | None = None
    size_matched: float | None = None
    price: float | None = None
    outcome: str | None = None
    order_type: str | None = None
    expiration: int | None = None
    created_at: datetime | None = None
    raw_json: dict[str, Any] | None = None


class LiveTradeFill(DomainModel):
    ts: datetime
    trade_id: str | None = None
    taker_order_id: str | None = None
    maker_order_ids: list[str] = Field(default_factory=list)
    condition_id: str | None = None
    token_id: str | None = None
    side: str | None = None
    price: float | None = None
    size: float | None = None
    fee: float | None = None
    status: str | None = None
    tx_hash: str | None = None
    raw_json: dict[str, Any] | None = None


class LiveExecutionResult(DomainModel):
    prepared: PreparedLiveOrder
    local_order_id: int | None = None
    external_order_id: str | None = None
    submitted_status: str | None = None
    synced_status: str | None = None
    fills_synced: int = 0
    raw_response: dict[str, Any] | None = None


class BalanceAllowanceSnapshot(DomainModel):
    asset_type: str
    token_id: str | None = None
    balance: float | None = None
    allowance: float | None = None
    available: float | None = None
    raw_json: dict[str, Any] | None = None


class LiveAccountHealth(DomainModel):
    ok: bool
    reason: str
    collateral: BalanceAllowanceSnapshot | None = None
    conditional: list[BalanceAllowanceSnapshot] = Field(default_factory=list)
    outstanding_buy_notional: float = 0.0
    outstanding_sell_size_by_token: dict[str, float] = Field(default_factory=dict)
    required_available: float = 0.0
    required_token_id: str | None = None


class BacktestReport(DomainModel):
    fill_model: str = "optimistic"
    total_trades: int
    win_rate: float
    average_edge_bps: float
    realized_pnl: float
    fees_total: float = 0.0
    unrealized_pnl: float
    max_drawdown: float
    exposure_by_category: dict[str, float]
    top_winning_markets: list[tuple[str, float]]
    top_losing_markets: list[tuple[str, float]]
    source_wallet_attribution: dict[str, int]

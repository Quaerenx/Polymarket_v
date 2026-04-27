from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pm_alpha_bot.config import Settings
from pm_alpha_bot.db.models import Market, MarketSnapshot
from pm_alpha_bot.domain import RiskState, SignalRecord
from pm_alpha_bot.risk.sizing import order_size_units, position_size_pct
from pm_alpha_bot.strategy.filters import snapshot_is_fresh


@dataclass(slots=True)
class RiskDecision:
    """Decision from the risk engine."""

    passed: bool
    reason: str
    size_units: float = 0.0
    size_pct: float = 0.0


def evaluate_signal_risk(
    *,
    signal: SignalRecord,
    market: Market,
    snapshot: MarketSnapshot,
    risk_state: RiskState,
    settings: Settings,
    as_of: datetime,
) -> RiskDecision:
    """Evaluate risk limits and produce an order size."""
    if risk_state.daily_loss_pct > settings.max_daily_loss_pct:
        return RiskDecision(False, "daily_loss_limit_exceeded")
    if risk_state.total_drawdown_pct > settings.max_total_drawdown_pct:
        return RiskDecision(False, "total_drawdown_limit_exceeded")
    if not snapshot_is_fresh(snapshot, settings, as_of):
        return RiskDecision(False, "stale_orderbook")
    if signal.effective_entry_price is None or signal.fair_prob is None:
        return RiskDecision(False, "missing_pricing")
    market_exposure = risk_state.market_exposure_pct.get(signal.token_id, 0.0)
    condition_exposure = risk_state.condition_exposure_pct.get(market.condition_id, 0.0)
    event_exposure = (
        risk_state.event_exposure_pct.get(market.event_id, 0.0)
        if market.event_id
        else 0.0
    )
    category_exposure = risk_state.category_exposure_pct.get(market.category or "", 0.0)
    size_pct = position_size_pct(
        fair_prob=signal.fair_prob,
        entry_price=signal.effective_entry_price,
        kelly_fraction_scale=settings.kelly_fraction,
        max_market_exposure_pct=settings.max_market_exposure_pct,
        confidence=signal.confidence,
    )
    if size_pct <= 0:
        return RiskDecision(False, "non_positive_size")
    if market_exposure + size_pct > settings.max_market_exposure_pct:
        return RiskDecision(False, "market_exposure_limit")
    if condition_exposure + size_pct > settings.max_condition_exposure_pct:
        return RiskDecision(False, "condition_exposure_limit")
    if market.event_id and event_exposure + size_pct > settings.max_event_exposure_pct:
        return RiskDecision(False, "event_exposure_limit")
    if category_exposure + size_pct > settings.max_category_exposure_pct:
        return RiskDecision(False, "category_exposure_limit")
    size_units = order_size_units(risk_state.capital, size_pct, signal.effective_entry_price)
    if market.min_order_size is not None and size_units < market.min_order_size:
        return RiskDecision(False, "below_min_order_size")
    return RiskDecision(True, "ok", size_units=size_units, size_pct=size_pct)

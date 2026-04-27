from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pm_alpha_bot.config import Settings
from pm_alpha_bot.db.models import Market, MarketSnapshot
from pm_alpha_bot.time import ensure_utc


def snapshot_is_fresh(snapshot: MarketSnapshot, settings: Settings, as_of: datetime) -> bool:
    """Return true when the orderbook snapshot is fresh enough."""
    return (as_of - ensure_utc(snapshot.ts)).total_seconds() <= settings.snapshot_stale_after_sec


def spread_bps(snapshot: MarketSnapshot) -> float | None:
    """Return spread in basis points for Polymarket prices in `[0, 1]`."""
    if snapshot.best_ask is None or snapshot.best_bid is None:
        return None
    return (snapshot.best_ask - snapshot.best_bid) * 10000


def snapshot_has_tradeable_orderbook(
    snapshot: MarketSnapshot | None,
    *,
    settings: Settings,
    as_of: datetime,
    max_age: timedelta | None = None,
) -> bool:
    """Return true when the latest snapshot is recent and has a usable orderbook."""
    if snapshot is None:
        return False
    if snapshot.best_bid is None or snapshot.best_ask is None:
        return False
    if snapshot.best_bid <= 0 or snapshot.best_ask >= 1 or snapshot.best_bid >= snapshot.best_ask:
        return False
    age_limit = max_age or timedelta(
        minutes=max(settings.tradeable_orderbook_focus_window_minutes, 1)
    )
    if (as_of - ensure_utc(snapshot.ts)) > age_limit:
        return False
    spread = spread_bps(snapshot)
    return spread is not None and spread <= settings.max_spread_bps


def _raw_bool(raw: dict[str, Any], key: str) -> bool | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return bool(value)


def market_is_tradeable(market: Market, as_of: datetime) -> bool:
    """Return true when CLOB metadata says a market can currently accept orders."""
    raw = market.raw_json if isinstance(market.raw_json, dict) else {}
    raw_active = _raw_bool(raw, "active")
    raw_closed = _raw_bool(raw, "closed")
    raw_archived = _raw_bool(raw, "archived")
    accepting_orders = _raw_bool(raw, "accepting_orders")
    enable_order_book = _raw_bool(raw, "enable_order_book")

    if market.closed or raw_closed is True or raw_archived is True:
        return False
    if raw_active is False or (raw_active is None and not market.active):
        return False
    if accepting_orders is False or enable_order_book is False:
        return False
    if accepting_orders is True and enable_order_book is not False:
        return True
    return not (market.end_date is not None and ensure_utc(market.end_date) <= as_of)

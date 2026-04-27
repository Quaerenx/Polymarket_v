from __future__ import annotations

from datetime import datetime, timedelta
from typing import TypedDict

from sqlalchemy import func, select

from pm_alpha_bot.config import Settings
from pm_alpha_bot.db.models import Market, MarketSnapshot
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.market_categories import normalize_market_category
from pm_alpha_bot.strategy.filters import snapshot_has_tradeable_orderbook

MARKET_CATEGORY_NO_TRADEABLE_ORDERBOOK = "market_category_no_tradeable_orderbook"


class MarketCategoryLiquiditySummary(TypedDict):
    enabled: bool
    eligible: bool
    category: str | None
    total_snapshots: int
    tradeable_snapshots: int
    min_category_samples: int


def market_category_liquidity_summary(
    repo: Repository,
    *,
    market_category: str | None,
    settings: Settings,
    as_of: datetime,
) -> MarketCategoryLiquiditySummary:
    """Summarize whether a category has recent usable orderbook evidence."""
    normalized_category = normalize_market_category(market_category)
    min_samples = max(int(settings.signal_liquidity_gate_min_category_samples), 0)
    if not settings.signal_liquidity_gate_enabled or normalized_category is None:
        return {
            "enabled": bool(settings.signal_liquidity_gate_enabled),
            "eligible": True,
            "category": normalized_category,
            "total_snapshots": 0,
            "tradeable_snapshots": 0,
            "min_category_samples": min_samples,
        }

    window_start = as_of - timedelta(
        minutes=max(int(settings.signal_liquidity_gate_window_minutes), 1)
    )
    snapshots = repo.session.scalars(
        select(MarketSnapshot)
        .join(Market, Market.condition_id == MarketSnapshot.condition_id)
        .where(
            MarketSnapshot.ts >= window_start,
            MarketSnapshot.ts <= as_of,
            func.lower(func.coalesce(Market.category, "")) == normalized_category,
        )
    )
    total = 0
    tradeable = 0
    for snapshot in snapshots:
        total += 1
        if snapshot_has_tradeable_orderbook(snapshot, settings=settings, as_of=as_of):
            tradeable += 1
    return {
        "enabled": True,
        "eligible": total < min_samples or tradeable > 0,
        "category": normalized_category,
        "total_snapshots": total,
        "tradeable_snapshots": tradeable,
        "min_category_samples": min_samples,
    }


def market_category_has_recent_tradeable_orderbook(
    repo: Repository,
    *,
    market_category: str | None,
    settings: Settings,
    as_of: datetime,
    cache: dict[str, MarketCategoryLiquiditySummary] | None = None,
) -> bool:
    """Return true unless recent data proves the whole category has no usable orderbooks."""
    normalized_category = normalize_market_category(market_category) or ""
    if cache is not None and normalized_category in cache:
        return cache[normalized_category]["eligible"]
    summary = market_category_liquidity_summary(
        repo,
        market_category=market_category,
        settings=settings,
        as_of=as_of,
    )
    if cache is not None:
        cache[normalized_category] = summary
    return summary["eligible"]

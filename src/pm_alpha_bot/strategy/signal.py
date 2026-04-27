from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.domain import SignalRecord
from pm_alpha_bot.execution.orders import maker_limit_price
from pm_alpha_bot.market_categories import market_category_matches, normalize_market_category
from pm_alpha_bot.strategy.fair_value import compute_fair_probability
from pm_alpha_bot.strategy.filters import market_is_tradeable, snapshot_is_fresh, spread_bps
from pm_alpha_bot.strategy.liquidity import (
    MarketCategoryLiquiditySummary,
    market_category_has_recent_tradeable_orderbook,
)
from pm_alpha_bot.time import ensure_utc


def _recency_weight(age_hours: float) -> float:
    if age_hours <= 0.25:
        return 1.0
    if age_hours <= 1:
        return 0.8
    if age_hours <= 3:
        return 0.5
    if age_hours <= 8:
        return 0.2
    return 0.0


def _direction_sign(side: str | None) -> float:
    value = (side or "").lower()
    if value.startswith("sell"):
        return -1.0
    return 1.0


def _activity_side_is_trade(side: str | None) -> bool:
    return (side or "").strip().upper() in {"BUY", "SELL"}


def _wallet_is_signal_eligible(row: Any, *, min_score: float) -> bool:
    raw_metrics = row.raw_metrics_json or {}
    recent_30d_pnl = raw_metrics.get("recent_30d_pnl") or 0.0
    concentration = row.profit_concentration if row.profit_concentration is not None else 0.0
    return bool(
        (row.score or 0.0) >= min_score
        and (row.closed_market_count or 0) >= 30
        and (row.trade_count or 0) >= 50
        and recent_30d_pnl >= 0
        and (row.roi or 0.0) >= 0.03
        and concentration <= 0.40
    )


def generate_signals(
    repo: Repository,
    *,
    category: str,
    settings: Settings | None = None,
    as_of: datetime | None = None,
    market_category: str | None = None,
) -> list[SignalRecord]:
    """Generate tradeable signals from wallet consensus and market data."""
    resolved_settings = settings or get_settings()
    resolved_as_of = as_of or datetime.now(UTC)
    resolved_market_category = normalize_market_category(
        market_category or resolved_settings.market_category_filter
    )
    scores = repo.latest_wallet_scores(
        category=category,
        min_score=resolved_settings.signal_min_wallet_score,
        as_of=resolved_as_of,
    )
    eligible = {
        row.proxy_wallet: row
        for row in scores
        if _wallet_is_signal_eligible(row, min_score=resolved_settings.signal_min_wallet_score)
    }
    if not eligible:
        return []
    liquidity_cache: dict[str, MarketCategoryLiquiditySummary] = {}
    activity = repo.recent_wallet_activity(
        proxy_wallets=list(eligible),
        since=resolved_as_of - timedelta(hours=resolved_settings.signal_activity_lookback_hours),
        until=resolved_as_of,
        market_category=resolved_market_category,
    )
    per_token_wallet: dict[str, dict[str, Any]] = defaultdict(dict)
    for item in activity:
        if item.token_id is None:
            continue
        if not _activity_side_is_trade(item.side):
            continue
        current = per_token_wallet[item.token_id].get(item.proxy_wallet)
        if current is None or item.ts > current["ts"]:
            per_token_wallet[item.token_id][item.proxy_wallet] = {
                "ts": item.ts,
                "side": item.side,
                "size": item.size or 0.0,
            }
    signals: list[SignalRecord] = []
    for token_id, wallet_map in per_token_wallet.items():
        snapshot = repo.latest_snapshot_for_token(token_id, as_of=resolved_as_of)
        market = repo.get_market_for_token(token_id)
        if snapshot is None or market is None:
            continue
        if not market_category_matches(market.category, resolved_market_category):
            continue
        if not market_is_tradeable(market, resolved_as_of):
            continue
        if not market_category_has_recent_tradeable_orderbook(
            repo,
            market_category=market.category,
            settings=resolved_settings,
            as_of=resolved_as_of,
            cache=liquidity_cache,
        ):
            continue
        snapshot_fresh = snapshot_is_fresh(snapshot, resolved_settings, resolved_as_of)
        if not snapshot_fresh:
            continue
        signed_wallets: list[tuple[str, float]] = []
        wallet_direction_score = 0.0
        contributing_age_hours: list[float] = []
        for wallet_address, metadata in wallet_map.items():
            wallet_score = eligible[wallet_address].score or 0.0
            age_hours = max(
                0.0, (resolved_as_of - ensure_utc(metadata["ts"])).total_seconds() / 3600
            )
            weight = _recency_weight(age_hours)
            size_weight = min(max(metadata["size"], 1.0), 10.0) / 10
            contribution = wallet_score * _direction_sign(metadata["side"]) * weight * size_weight
            wallet_direction_score += contribution
            if contribution > 0:
                signed_wallets.append((wallet_address, contribution))
                contributing_age_hours.append(age_hours)
        source_wallets = [wallet for wallet, contribution in signed_wallets if contribution > 0]
        if len(source_wallets) < resolved_settings.min_wallet_consensus:
            continue
        if wallet_direction_score <= 0:
            continue
        fair_prob, components = compute_fair_probability(
            snapshot=snapshot,
            wallet_direction_score=wallet_direction_score,
            repo=repo,
            settings=resolved_settings,
            as_of=resolved_as_of,
        )
        tick_size = market.min_tick_size or 0.01
        entry_price = maker_limit_price(snapshot, tick_size)
        edge_bps = (fair_prob - entry_price) * 10000
        spread = spread_bps(snapshot)
        if spread is None or spread > resolved_settings.max_spread_bps:
            continue
        if edge_bps < resolved_settings.min_edge_bps:
            continue
        reason_json = {
            "wallet_direction_score": wallet_direction_score,
            "components": components,
            "spread_bps": spread,
            "snapshot_fresh": snapshot_fresh,
            "source_wallets": source_wallets,
            "source_wallet_activity_age_hours": _age_summary(
                contributing_age_hours,
                cutoff_hours=resolved_settings.signal_activity_lookback_hours,
            ),
            "market_category": market.category,
            "market_category_filter": resolved_market_category,
        }
        confidence = min(1.0, wallet_direction_score / max(len(source_wallets), 1))
        signals.append(
            SignalRecord(
                ts=resolved_as_of,
                condition_id=market.condition_id,
                token_id=token_id,
                direction="BUY",
                market_midpoint=snapshot.midpoint,
                effective_entry_price=entry_price,
                fair_prob=fair_prob,
                edge_bps=edge_bps,
                confidence=confidence,
                source_wallet_count=len(source_wallets),
                source_wallets_json=source_wallets,
                reason_json=reason_json,
                status="new",
            )
        )
    return signals


def _age_summary(age_hours: list[float], *, cutoff_hours: int) -> dict[str, float | int | None]:
    if not age_hours:
        return {
            "min": None,
            "max": None,
            "avg": None,
            "cutoff": cutoff_hours,
        }
    return {
        "min": min(age_hours),
        "max": max(age_hours),
        "avg": sum(age_hours) / len(age_hours),
        "cutoff": cutoff_hours,
    }

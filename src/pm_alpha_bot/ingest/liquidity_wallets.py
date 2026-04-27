from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from pm_alpha_bot.clients.base import ApiError
from pm_alpha_bot.clients.clob_public import ClobPublicClient
from pm_alpha_bot.clients.data_api import DataApiClient
from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db import models as m
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.domain import WalletActivityRecord
from pm_alpha_bot.ingest.orderbook_fetch import fetch_latest_orderbook_snapshots
from pm_alpha_bot.market_categories import market_category_matches
from pm_alpha_bot.strategy.filters import market_is_tradeable, snapshot_has_tradeable_orderbook
from pm_alpha_bot.strategy.liquidity import (
    MarketCategoryLiquiditySummary,
    market_category_has_recent_tradeable_orderbook,
)
from pm_alpha_bot.strategy.signal import _activity_side_is_trade
from pm_alpha_bot.time import ensure_utc


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _activity_key(activity: WalletActivityRecord) -> tuple[str, ...]:
    tx_hash = activity.tx_hash or ""
    if tx_hash:
        return ("tx", tx_hash, str(activity.token_id or ""))
    return (
        "row",
        activity.proxy_wallet,
        activity.condition_id,
        str(activity.token_id or ""),
        ensure_utc(activity.ts).isoformat(),
        str(activity.side or ""),
        str(activity.price or ""),
        str(activity.size or ""),
    )


def _dedupe_activity(values: list[WalletActivityRecord]) -> list[WalletActivityRecord]:
    seen: set[tuple[str, ...]] = set()
    deduped: list[WalletActivityRecord] = []
    for value in values:
        key = _activity_key(value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _is_recent_trade(
    activity: WalletActivityRecord,
    *,
    settings: Settings,
    as_of: datetime,
) -> bool:
    ts = ensure_utc(activity.ts)
    if ts > as_of + timedelta(minutes=5):
        return False
    lookback = timedelta(hours=max(int(settings.signal_activity_lookback_hours), 1))
    return ts >= as_of - lookback


async def discover_liquid_trade_wallets(
    repo: Repository,
    *,
    settings: Settings | None = None,
    trade_limit: int | None = None,
    wallet_limit: int | None = None,
    market_category: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """Discover additional wallets from recent trades in currently liquid markets."""
    resolved_settings = settings or get_settings()
    resolved_as_of = as_of or datetime.now(UTC)
    if not resolved_settings.liquidity_wallet_discovery_enabled:
        return _empty_summary(disabled=True)

    resolved_trade_limit = max(
        int(trade_limit or resolved_settings.liquidity_wallet_discovery_trade_limit),
        0,
    )
    resolved_wallet_limit = max(
        int(wallet_limit or resolved_settings.liquidity_wallet_discovery_wallet_limit),
        0,
    )
    if resolved_trade_limit <= 0 or resolved_wallet_limit <= 0:
        return _empty_summary(disabled=False)

    async with DataApiClient(settings=resolved_settings) as client:
        global_trades = await client.get_recent_trades(limit=resolved_trade_limit)
    global_trades = [
        trade
        for trade in global_trades
        if trade.proxy_wallet and trade.token_id and _activity_side_is_trade(trade.side)
    ]

    condition_ids = _dedupe_preserve_order([trade.condition_id for trade in global_trades])
    markets_enriched = await _enrich_missing_markets(
        repo,
        condition_ids=condition_ids,
        settings=resolved_settings,
    )
    token_ids = _dedupe_preserve_order(
        [str(trade.token_id) for trade in global_trades if trade.token_id]
    )
    orderbooks_enriched = 0
    if token_ids:
        snapshots, _ = await fetch_latest_orderbook_snapshots(
            repo,
            token_ids=token_ids,
            settings=resolved_settings,
            as_of=resolved_as_of,
        )
        orderbooks_enriched += repo.save_snapshots(snapshots) if snapshots else 0

    liquid_condition_ids = _recent_liquid_condition_ids(
        repo,
        settings=resolved_settings,
        market_category=market_category,
        as_of=resolved_as_of,
        limit=resolved_settings.liquidity_wallet_discovery_market_limit,
    )
    market_trades = await _fetch_market_trades(
        condition_ids=liquid_condition_ids,
        settings=resolved_settings,
        limit=resolved_settings.liquidity_wallet_discovery_market_trade_limit,
    )

    recent_trades = _dedupe_activity(
        [
            trade
            for trade in [*global_trades, *market_trades]
            if trade.proxy_wallet
            and trade.token_id
            and _activity_side_is_trade(trade.side)
            and _is_recent_trade(trade, settings=resolved_settings, as_of=resolved_as_of)
        ]
    )
    if not recent_trades:
        return {
            **_empty_summary(disabled=False),
            "liquid_markets": len(liquid_condition_ids),
            "market_trades": len(market_trades),
        }

    condition_ids = _dedupe_preserve_order([trade.condition_id for trade in recent_trades])
    markets_enriched += await _enrich_missing_markets(
        repo,
        condition_ids=condition_ids,
        settings=resolved_settings,
    )
    token_ids = _dedupe_preserve_order(
        [str(trade.token_id) for trade in recent_trades if trade.token_id]
    )
    token_ids = _tokens_without_tradeable_snapshot(
        repo,
        token_ids=token_ids,
        settings=resolved_settings,
        as_of=resolved_as_of,
    )
    snapshots, _ = await fetch_latest_orderbook_snapshots(
        repo,
        token_ids=token_ids,
        settings=resolved_settings,
        as_of=resolved_as_of,
    )
    orderbooks_enriched += repo.save_snapshots(snapshots) if snapshots else 0

    liquidity_cache: dict[str, MarketCategoryLiquiditySummary] = {}
    selected_activity: list[WalletActivityRecord] = []
    wallet_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "proxy_wallet": "",
            "username": None,
            "trade_count": 0,
            "notional": 0.0,
            "categories": set(),
            "condition_ids": set(),
            "token_ids": set(),
        }
    )
    for trade in recent_trades:
        if trade.token_id is None:
            continue
        market = repo.get_market_for_token(str(trade.token_id))
        if market is None:
            continue
        if not market_category_matches(market.category, market_category):
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
        snapshot = repo.latest_snapshot_for_token(str(trade.token_id), as_of=resolved_as_of)
        if not snapshot_has_tradeable_orderbook(
            snapshot,
            settings=resolved_settings,
            as_of=resolved_as_of,
        ):
            continue

        selected_activity.append(trade)
        stats = wallet_stats[trade.proxy_wallet]
        stats["proxy_wallet"] = trade.proxy_wallet
        raw = trade.raw_json or {}
        stats["username"] = stats["username"] or raw.get("pseudonym") or raw.get("name")
        stats["trade_count"] = int(stats["trade_count"]) + 1
        stats["notional"] = float(stats["notional"]) + (trade.price or 0.0) * (trade.size or 0.0)
        stats["categories"].add(market.category or "")
        stats["condition_ids"].add(trade.condition_id)
        stats["token_ids"].add(str(trade.token_id))

    stored_activity = repo.add_wallet_activity(selected_activity) if selected_activity else 0
    discovered_wallets = [
        {
            **stats,
            "categories": sorted(value for value in stats["categories"] if value),
            "condition_ids": sorted(stats["condition_ids"]),
            "token_ids": sorted(stats["token_ids"]),
            "source": "liquidity_discovery",
        }
        for stats in wallet_stats.values()
    ]
    discovered_wallets.sort(
        key=lambda item: (float(item.get("notional") or 0.0), int(item.get("trade_count") or 0)),
        reverse=True,
    )
    discovered_wallets = discovered_wallets[:resolved_wallet_limit]
    wallets_upserted = repo.upsert_discovered_wallets(
        discovered_wallets,
        observed_at=resolved_as_of,
    )
    return {
        "disabled": 0,
        "recent_trades": len(recent_trades),
        "liquid_markets": len(liquid_condition_ids),
        "market_trades": len(market_trades),
        "markets_enriched": markets_enriched,
        "orderbooks_enriched": orderbooks_enriched,
        "liquid_trade_rows": len(selected_activity),
        "wallets_discovered": len(discovered_wallets),
        "wallets_upserted": wallets_upserted,
        "activity_stored": stored_activity,
    }


def _recent_liquid_condition_ids(
    repo: Repository,
    *,
    settings: Settings,
    market_category: str | None,
    as_of: datetime,
    limit: int,
) -> list[str]:
    resolved_limit = max(int(limit), 0)
    if resolved_limit <= 0:
        return []
    window_start = as_of - timedelta(
        minutes=max(int(settings.tradeable_orderbook_focus_window_minutes), 1)
    )
    rows = repo.session.execute(
        select(m.MarketSnapshot, m.Market)
        .join(m.Market, m.Market.condition_id == m.MarketSnapshot.condition_id)
        .where(m.MarketSnapshot.ts >= window_start, m.MarketSnapshot.ts <= as_of)
        .order_by(m.MarketSnapshot.ts.desc())
        .limit(resolved_limit * 20)
    ).all()
    liquidity_cache: dict[str, MarketCategoryLiquiditySummary] = {}
    condition_ids: list[str] = []
    seen: set[str] = set()
    for snapshot, market in rows:
        if market.condition_id in seen:
            continue
        if not market_category_matches(market.category, market_category):
            continue
        if not market_is_tradeable(market, as_of):
            continue
        if not market_category_has_recent_tradeable_orderbook(
            repo,
            market_category=market.category,
            settings=settings,
            as_of=as_of,
            cache=liquidity_cache,
        ):
            continue
        if not snapshot_has_tradeable_orderbook(snapshot, settings=settings, as_of=as_of):
            continue
        seen.add(market.condition_id)
        condition_ids.append(market.condition_id)
        if len(condition_ids) >= resolved_limit:
            break
    return condition_ids


def _tokens_without_tradeable_snapshot(
    repo: Repository,
    *,
    token_ids: list[str],
    settings: Settings,
    as_of: datetime,
) -> list[str]:
    return [
        token_id
        for token_id in token_ids
        if not snapshot_has_tradeable_orderbook(
            repo.latest_snapshot_for_token(token_id, as_of=as_of),
            settings=settings,
            as_of=as_of,
        )
    ]


async def _fetch_market_trades(
    *,
    condition_ids: list[str],
    settings: Settings,
    limit: int,
) -> list[WalletActivityRecord]:
    resolved_limit = max(int(limit), 0)
    if resolved_limit <= 0 or not condition_ids:
        return []
    trades: list[WalletActivityRecord] = []
    async with DataApiClient(settings=settings) as client:
        for condition_id in condition_ids:
            try:
                trades.extend(await client.get_market_trades(condition_id, limit=resolved_limit))
            except (httpx.HTTPError, ApiError):
                continue
    return trades


async def _enrich_missing_markets(
    repo: Repository,
    *,
    condition_ids: list[str],
    settings: Settings,
) -> int:
    missing = [
        condition_id
        for condition_id in condition_ids
        if repo.get_market_by_condition_id(condition_id) is None
    ]
    if not missing:
        return 0
    markets = []
    async with ClobPublicClient(settings=settings) as client:
        for condition_id in missing:
            try:
                markets.append(await client.get_normalized_market_info(condition_id))
            except (httpx.HTTPError, ApiError):
                continue
    if not markets:
        return 0
    count = repo.upsert_markets(markets)
    repo.session.expire_all()
    return count


def _empty_summary(*, disabled: bool) -> dict[str, int]:
    return {
        "disabled": int(disabled),
        "recent_trades": 0,
        "liquid_markets": 0,
        "market_trades": 0,
        "markets_enriched": 0,
        "orderbooks_enriched": 0,
        "liquid_trade_rows": 0,
        "wallets_discovered": 0,
        "wallets_upserted": 0,
        "activity_stored": 0,
    }

from __future__ import annotations

from datetime import datetime, timedelta

from pm_alpha_bot.clients.clob_public import ClobPublicClient
from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.models import Market, MarketSnapshot
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.ingest.orderbook_fetch import fetch_latest_orderbook_snapshots
from pm_alpha_bot.strategy.filters import (
    market_is_tradeable,
    snapshot_has_tradeable_orderbook,
    snapshot_is_fresh,
)
from pm_alpha_bot.strategy.liquidity import (
    MarketCategoryLiquiditySummary,
    market_category_has_recent_tradeable_orderbook,
)
from pm_alpha_bot.strategy.signal import _activity_side_is_trade, _wallet_is_signal_eligible


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _prioritize_tradeable_focus_token_ids(
    repo: Repository,
    *,
    token_ids: list[str],
    settings: Settings,
    as_of: datetime,
) -> list[str]:
    if not settings.tradeable_orderbook_focus:
        return token_ids
    known_tradeable: list[str] = []
    discovery: list[str] = []
    for token_id in token_ids:
        if snapshot_has_tradeable_orderbook(
            repo.latest_snapshot_for_token(token_id, as_of=as_of),
            settings=settings,
            as_of=as_of,
        ):
            known_tradeable.append(token_id)
        else:
            discovery.append(token_id)
    return [*known_tradeable, *discovery]


def _snapshot_needs_signal_refresh(
    snapshot: MarketSnapshot | None,
    *,
    settings: Settings,
    as_of: datetime,
) -> bool:
    if snapshot is None:
        return True
    if not snapshot_is_fresh(snapshot, settings, as_of):
        return True
    return not snapshot_has_tradeable_orderbook(snapshot, settings=settings, as_of=as_of)


def _market_needs_signal_refresh(
    market: Market | None,
    *,
    as_of: datetime,
) -> bool:
    if market is None:
        return True
    return not market_is_tradeable(market, as_of)


async def enrich_markets_from_recent_wallet_activity(
    repo: Repository,
    *,
    proxy_wallets: list[str],
    since: datetime,
    until: datetime,
    limit: int,
    market_category: str | None = None,
) -> int:
    """Fetch missing market metadata for recent tracked-wallet activity."""
    if limit <= 0 or not proxy_wallets:
        return 0
    condition_ids = repo.recent_wallet_activity_condition_ids(
        proxy_wallets=proxy_wallets,
        since=since,
        until=until,
        limit=limit,
        market_category=market_category,
    )
    missing = [
        condition_id
        for condition_id in condition_ids
        if condition_id
        if repo.get_market_by_condition_id(condition_id) is None
    ]
    if not missing:
        return 0
    async with ClobPublicClient() as client:
        markets = await client.get_normalized_markets(missing)
    return repo.upsert_markets(markets)


async def ingest_orderbooks_for_recent_wallet_tokens(
    repo: Repository,
    *,
    proxy_wallets: list[str],
    since: datetime,
    until: datetime,
    limit: int,
    settings: Settings | None = None,
    market_category: str | None = None,
) -> int:
    """Fetch latest orderbooks for recent tracked-wallet token activity."""
    if limit <= 0 or not proxy_wallets:
        return 0
    token_ids = repo.recent_wallet_activity_token_ids(
        proxy_wallets=proxy_wallets,
        since=since,
        until=until,
        limit=limit,
        market_category=market_category,
    )
    if not token_ids:
        return 0
    resolved_settings = settings or get_settings()
    token_ids = _prioritize_tradeable_focus_token_ids(
        repo,
        token_ids=token_ids,
        settings=resolved_settings,
        as_of=until,
    )
    if not token_ids:
        return 0
    snapshots, _ = await fetch_latest_orderbook_snapshots(
        repo,
        token_ids=token_ids,
        settings=resolved_settings,
    )
    if not snapshots:
        return 0
    return repo.save_snapshots(snapshots)


async def enrich_signal_inputs_for_category(
    repo: Repository,
    *,
    category: str,
    settings: Settings,
    as_of: datetime,
    limit: int,
    market_category: str | None = None,
) -> dict[str, int]:
    """Backfill missing market metadata and snapshots for eligible signal inputs."""
    if limit <= 0:
        return {
            "eligible_wallets": 0,
            "candidate_markets": 0,
            "candidate_tokens": 0,
            "markets_enriched": 0,
            "orderbooks_enriched": 0,
        }

    latest_scores = repo.latest_wallet_scores(category=category, as_of=as_of)
    eligible_wallets = [
        row.proxy_wallet
        for row in latest_scores
        if _wallet_is_signal_eligible(row, min_score=settings.signal_min_wallet_score)
    ]
    if not eligible_wallets:
        return {
            "eligible_wallets": 0,
            "candidate_markets": 0,
            "candidate_tokens": 0,
            "markets_enriched": 0,
            "orderbooks_enriched": 0,
        }

    activity = repo.recent_wallet_activity(
        proxy_wallets=eligible_wallets,
        since=as_of - timedelta(hours=settings.signal_activity_lookback_hours),
        until=as_of,
        market_category=market_category,
    )
    latest_activity_by_token: dict[str, tuple[str | None, datetime]] = {}
    for item in activity:
        if item.token_id is None:
            continue
        if not _activity_side_is_trade(item.side):
            continue
        current = latest_activity_by_token.get(item.token_id)
        if current is None or item.ts > current[1]:
            latest_activity_by_token[item.token_id] = (item.condition_id, item.ts)

    condition_ids_to_fetch: list[str] = []
    for token_id, (condition_id, _) in latest_activity_by_token.items():
        market = repo.get_market_for_token(token_id)
        if _market_needs_signal_refresh(market, as_of=as_of) and condition_id:
            condition_ids_to_fetch.append(str(condition_id))

    condition_ids_to_fetch = _dedupe_preserve_order(condition_ids_to_fetch)[:limit]
    markets_enriched = 0
    orderbooks_enriched = 0
    async with ClobPublicClient(settings=settings) as client:
        if condition_ids_to_fetch:
            markets = await client.get_normalized_markets(condition_ids_to_fetch)
            markets_enriched = repo.upsert_markets(markets)
            repo.session.expire_all()

    token_ids_to_fetch: list[str] = []
    liquidity_cache: dict[str, MarketCategoryLiquiditySummary] = {}
    for token_id in latest_activity_by_token:
        market = repo.get_market_for_token(token_id)
        if market is None or not market_is_tradeable(market, as_of):
            continue
        if not market_category_has_recent_tradeable_orderbook(
            repo,
            market_category=market.category,
            settings=settings,
            as_of=as_of,
            cache=liquidity_cache,
        ):
            continue
        if _snapshot_needs_signal_refresh(
            repo.latest_snapshot_for_token(token_id, as_of=as_of),
            settings=settings,
            as_of=as_of,
        ):
            token_ids_to_fetch.append(token_id)

    token_ids_to_fetch = _dedupe_preserve_order(token_ids_to_fetch)[:limit]
    token_ids_to_fetch = _prioritize_tradeable_focus_token_ids(
        repo,
        token_ids=token_ids_to_fetch,
        settings=settings,
        as_of=as_of,
    )
    if token_ids_to_fetch:
        snapshots, _ = await fetch_latest_orderbook_snapshots(
            repo,
            token_ids=token_ids_to_fetch,
            settings=settings,
            as_of=as_of,
        )
        if snapshots:
            orderbooks_enriched = repo.save_snapshots(snapshots)
    return {
        "eligible_wallets": len(eligible_wallets),
        "candidate_markets": len(condition_ids_to_fetch),
        "candidate_tokens": len(token_ids_to_fetch),
        "markets_enriched": markets_enriched,
        "orderbooks_enriched": orderbooks_enriched,
    }

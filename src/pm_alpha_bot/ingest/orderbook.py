from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.models import MarketToken
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.ingest.orderbook_fetch import (
    current_missing_orderbook_cache,
    fetch_latest_orderbook_snapshots,
)
from pm_alpha_bot.market_categories import normalize_market_category
from pm_alpha_bot.strategy.filters import snapshot_has_tradeable_orderbook


def _category_key(repo: Repository, token: MarketToken) -> str:
    market = repo.get_market_by_condition_id(token.condition_id)
    return normalize_market_category(getattr(market, "category", None)) or "(empty)"


def _round_robin_by_category(repo: Repository, tokens: list[MarketToken]) -> list[MarketToken]:
    grouped: OrderedDict[str, list[MarketToken]] = OrderedDict()
    for token in tokens:
        grouped.setdefault(_category_key(repo, token), []).append(token)

    selected: list[MarketToken] = []
    while grouped:
        for category in list(grouped):
            selected.append(grouped[category].pop(0))
            if not grouped[category]:
                del grouped[category]
    return selected


def _prioritize_known_tradeable(
    repo: Repository,
    *,
    tokens: list[MarketToken],
    settings: Settings,
    as_of: datetime,
    market_category: str | None,
) -> list[MarketToken]:
    known_tradeable: list[MarketToken] = []
    discovery: list[MarketToken] = []
    for token in tokens:
        snapshot = repo.latest_snapshot_for_token(token.token_id, as_of=as_of)
        if snapshot_has_tradeable_orderbook(snapshot, settings=settings, as_of=as_of):
            known_tradeable.append(token)
        else:
            discovery.append(token)

    if market_category is None:
        known_tradeable = _round_robin_by_category(repo, known_tradeable)
        discovery = _round_robin_by_category(repo, discovery)
    return [*known_tradeable, *discovery]


def _select_tokens_for_orderbook_refresh(
    repo: Repository,
    *,
    limit: int,
    settings: Settings,
    market_category: str | None = None,
) -> list[str]:
    discover_across_categories = market_category is None
    fetch_limit = (
        max(limit * 5, limit)
        if settings.tradeable_orderbook_focus or discover_across_categories
        else limit
    )
    tokens = repo.list_active_tokens(limit=fetch_limit, market_category=market_category)
    as_of = datetime.now(UTC)
    missing_cache = current_missing_orderbook_cache(repo, settings=settings, as_of=as_of)
    tokens = [token for token in tokens if token.token_id not in missing_cache]
    if discover_across_categories:
        tokens = _round_robin_by_category(repo, tokens)
    if settings.tradeable_orderbook_focus:
        tokens = _prioritize_known_tradeable(
            repo,
            tokens=tokens,
            settings=settings,
            as_of=as_of,
            market_category=market_category,
        )
    return [token.token_id for token in tokens[:limit]]


def select_tokens_for_orderbook_refresh(
    repo: Repository,
    *,
    limit: int,
    settings: Settings | None = None,
    market_category: str | None = None,
) -> list[str]:
    """Return active token IDs prioritized for orderbook discovery and refresh."""
    return _select_tokens_for_orderbook_refresh(
        repo,
        limit=limit,
        settings=settings or get_settings(),
        market_category=market_category,
    )


async def ingest_orderbook(
    repo: Repository,
    *,
    limit: int,
    settings: Settings | None = None,
    market_category: str | None = None,
) -> int:
    """Fetch and persist latest orderbook snapshots for active tokens."""
    resolved_settings = settings or get_settings()
    token_ids = _select_tokens_for_orderbook_refresh(
        repo,
        limit=limit,
        settings=resolved_settings,
        market_category=market_category,
    )
    if not token_ids:
        return 0
    snapshots, _ = await fetch_latest_orderbook_snapshots(
        repo,
        token_ids=token_ids,
        settings=resolved_settings,
    )
    return repo.save_snapshots(snapshots)

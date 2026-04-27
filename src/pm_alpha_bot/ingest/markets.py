from __future__ import annotations

from pm_alpha_bot.clients.gamma import GammaClient
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.market_categories import market_category_matches


async def ingest_markets(
    repo: Repository,
    limit: int,
    active_only: bool,
    market_category: str | None = None,
) -> int:
    """Fetch and persist market metadata."""
    fetch_limit = limit if not market_category else max(limit * 5, limit)
    async with GammaClient() as client:
        markets = await client.list_markets(limit=fetch_limit, active_only=active_only)
    if market_category:
        markets = [
            market
            for market in markets
            if market_category_matches(market.category, market_category)
        ][:limit]
    return repo.upsert_markets(markets)

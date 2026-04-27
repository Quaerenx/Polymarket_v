from __future__ import annotations

from datetime import UTC, datetime

from pm_alpha_bot.clients.data_api import DataApiClient
from pm_alpha_bot.db.repository import Repository


async def ingest_leaderboard(
    repo: Repository,
    *,
    category: str,
    time_period: str,
    limit: int,
) -> int:
    """Fetch leaderboard data and upsert tracked wallets."""
    async with DataApiClient() as client:
        entries = await client.get_leaderboard(
            category=category, time_period=time_period, limit=limit
        )
    observed_at = datetime.now(UTC)
    return repo.upsert_wallets_from_leaderboard(
        entries,
        time_period=time_period,
        observed_at=observed_at,
    )

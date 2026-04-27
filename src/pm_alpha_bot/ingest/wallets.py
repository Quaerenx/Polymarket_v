from __future__ import annotations

from datetime import UTC, datetime

from pm_alpha_bot.clients.data_api import DataApiClient
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.logging import get_logger

logger = get_logger(__name__)


async def ingest_wallets(repo: Repository, *, limit: int) -> dict[str, int]:
    """Refresh tracked wallet positions, activity, and enrichment data."""
    wallets = repo.list_wallets(limit=limit)
    summary = {"wallets": 0, "positions": 0, "activity": 0, "failures": 0}
    async with DataApiClient() as client:
        for wallet in wallets:
            observed_at = datetime.now(UTC)
            try:
                positions = await client.get_current_positions(wallet.proxy_wallet, limit=limit)
                closed_positions = await client.get_closed_positions(
                    wallet.proxy_wallet, limit=limit
                )
                activity = await client.get_activity(wallet.proxy_wallet, limit=limit)
                trades = await client.get_trades(wallet.proxy_wallet, limit=limit)
                total_value = await client.get_total_value(wallet.proxy_wallet)
            except Exception:
                logger.exception("wallet_ingest_failed", extra={"wallet": wallet.proxy_wallet})
                summary["failures"] += 1
                continue
            repo.replace_wallet_positions(wallet.proxy_wallet, positions)
            summary["positions"] += len(positions)
            summary["activity"] += repo.add_wallet_activity(activity)
            repo.update_wallet_details(
                wallet.proxy_wallet,
                closed_positions=closed_positions,
                trades=trades,
                total_value=total_value,
                observed_at=observed_at,
            )
            summary["wallets"] += 1
    return summary

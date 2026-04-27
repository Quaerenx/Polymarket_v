from __future__ import annotations

from datetime import datetime, timedelta

from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.scoring.metrics import mean


def approximate_wallet_clv(
    repo: Repository, proxy_wallet: str, as_of: datetime
) -> dict[str, float | None]:
    """Approximate wallet CLV using future midpoint snapshots."""
    horizons = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24)}
    activities = repo.recent_wallet_activity(
        proxy_wallets=[proxy_wallet],
        since=as_of - timedelta(days=30),
        until=as_of,
    )
    results: dict[str, float | None] = {}
    for label, delta in horizons.items():
        samples: list[float] = []
        for activity in activities:
            if activity.price is None or activity.token_id is None:
                continue
            future = repo.future_snapshot(activity.token_id, activity.ts + delta)
            if future is None or future.midpoint is None:
                continue
            direction = -1.0 if (activity.side or "").lower().startswith("sell") else 1.0
            samples.append(direction * (future.midpoint - activity.price))
        results[label] = mean(samples)
    return results

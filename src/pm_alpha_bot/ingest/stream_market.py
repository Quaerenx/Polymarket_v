from __future__ import annotations

from collections.abc import Sequence

from pm_alpha_bot.clients.websocket_market import (
    MarketWebSocketClient,
    normalize_market_message,
)
from pm_alpha_bot.db.repository import Repository


async def stream_market(
    repo: Repository,
    *,
    token_ids: Sequence[str],
    max_messages: int | None = None,
    timeout_sec: float | None = None,
) -> dict[str, int]:
    """Stream market-channel events and persist normalized snapshots."""
    client = MarketWebSocketClient()
    summary = {"messages": 0, "snapshots": 0}
    async for payload in client.stream_messages(
        token_ids,
        max_messages=max_messages,
        timeout_sec=timeout_sec,
    ):
        summary["messages"] += 1
        events = payload if isinstance(payload, list) else [payload]
        snapshots = []
        for event in events:
            snapshots.extend(
                normalize_market_message(
                    event,
                    get_existing=repo.latest_snapshot_for_token,
                )
            )
        if snapshots:
            summary["snapshots"] += repo.save_snapshots(snapshots)
            repo.session.commit()
    return summary

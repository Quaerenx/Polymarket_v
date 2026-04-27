from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pm_alpha_bot.clients.base import BaseApiClient
from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.domain import LeaderboardEntry, WalletActivityRecord, WalletPositionRecord


def _parse_datetime(value: str | int | float | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class DataApiClient(BaseApiClient):
    """Public Data API client."""

    def __init__(self, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        super().__init__(resolved.poly_data_host, settings=resolved)

    async def get_leaderboard(
        self,
        *,
        category: str,
        time_period: str,
        limit: int,
    ) -> list[LeaderboardEntry]:
        """Fetch normalized leaderboard entries."""
        payload = await self.get_json(
            "/v1/leaderboard",
            params={
                "category": category,
                "timePeriod": time_period,
                "limit": limit,
                "orderBy": "PNL",
            },
        )
        entries: list[LeaderboardEntry] = []
        for item in payload:
            normalized = LeaderboardEntry.model_validate(item)
            normalized.category = category
            normalized.raw_json = item
            entries.append(normalized)
        return entries

    async def get_current_positions(
        self, wallet: str, limit: int = 100
    ) -> list[WalletPositionRecord]:
        """Fetch current positions for a wallet."""
        payload = await self.get_json("/positions", params={"user": wallet, "limit": limit})
        now = datetime.now(UTC)
        return [
            WalletPositionRecord(
                proxy_wallet=wallet,
                condition_id=str(item["conditionId"]),
                token_id=item.get("asset"),
                outcome=item.get("outcome"),
                size=_to_float(item.get("size")),
                avg_price=_to_float(item.get("avgPrice")),
                current_price=_to_float(item.get("curPrice")),
                current_value=_to_float(item.get("currentValue")),
                cash_pnl=_to_float(item.get("cashPnl")),
                realized_pnl=_to_float(item.get("realizedPnl")),
                total_pnl=_to_float(item.get("cashPnl")),
                updated_at=now,
                raw_json=item,
            )
            for item in payload
        ]

    async def get_closed_positions(self, wallet: str, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch closed positions for a wallet."""
        payload = await self.get_json("/closed-positions", params={"user": wallet, "limit": limit})
        return [dict(item) for item in payload]

    async def get_activity(self, wallet: str, limit: int = 100) -> list[WalletActivityRecord]:
        """Fetch wallet activity and normalize key fields."""
        payload = await self.get_json("/activity", params={"user": wallet, "limit": limit})
        return [_normalize_activity_item(item, proxy_wallet=wallet) for item in payload]

    async def get_recent_trades(self, limit: int = 100) -> list[WalletActivityRecord]:
        """Fetch recent public trades and normalize them as wallet activity rows."""
        payload = await self.get_json("/trades", params={"limit": limit})
        activities: list[WalletActivityRecord] = []
        for item in payload:
            proxy_wallet = str(item.get("proxyWallet") or item.get("user") or "")
            if not proxy_wallet:
                continue
            activities.append(_normalize_activity_item(item, proxy_wallet=proxy_wallet))
        return activities

    async def get_market_trades(
        self,
        condition_id: str,
        limit: int = 100,
    ) -> list[WalletActivityRecord]:
        """Fetch recent public trades for one market condition."""
        payload = await self.get_json(
            "/trades",
            params={"market": condition_id, "limit": limit},
        )
        activities: list[WalletActivityRecord] = []
        for item in payload:
            proxy_wallet = str(item.get("proxyWallet") or item.get("user") or "")
            if not proxy_wallet:
                continue
            activity = _normalize_activity_item(item, proxy_wallet=proxy_wallet)
            if activity.condition_id == condition_id:
                activities.append(activity)
        return activities

    async def get_trades(self, wallet: str, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch trades for a wallet."""
        payload = await self.get_json("/trades", params={"user": wallet, "limit": limit})
        return [dict(item) for item in payload]

    async def get_total_value(self, wallet: str) -> dict[str, Any]:
        """Fetch total wallet value data."""
        payload = await self.get_json("/value", params={"user": wallet})
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                return first
        return {"value": payload}


def _normalize_activity_item(item: dict[str, Any], *, proxy_wallet: str) -> WalletActivityRecord:
    activity_type = str(item.get("side") or item.get("type") or item.get("action") or "")
    condition_id = str(item.get("conditionId") or item.get("market") or "")
    return WalletActivityRecord(
        proxy_wallet=proxy_wallet,
        ts=_parse_datetime(item.get("timestamp") or item.get("createdAt")),
        condition_id=condition_id,
        token_id=item.get("asset") or item.get("tokenId"),
        side=activity_type,
        price=_to_float(item.get("price")),
        size=_to_float(item.get("size") or item.get("amount")),
        tx_hash=item.get("txHash") or item.get("transactionHash"),
        raw_json=item,
    )

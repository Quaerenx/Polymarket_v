from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from pm_alpha_bot.clients.base import ApiError, BaseApiClient
from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.domain import NormalizedMarket, NormalizedMarketToken, OrderBookSnapshot


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime:
    raw = _to_float(value)
    if raw is None:
        return datetime.now(UTC)
    if raw > 10_000_000_000:
        raw /= 1000
    return datetime.fromtimestamp(raw, tz=UTC)


class ClobPublicClient(BaseApiClient):
    """Public CLOB data client."""

    def __init__(self, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        super().__init__(resolved.poly_clob_host, settings=resolved)

    async def get_orderbook(self, token_id: str) -> OrderBookSnapshot:
        """Fetch and normalize an orderbook snapshot for a token."""
        payload = await self.get_json("/book", params={"token_id": token_id})
        bids = payload.get("bids", [])
        asks = payload.get("asks", [])
        best_bid = _to_float(bids[0]["price"]) if bids else None
        best_ask = _to_float(asks[0]["price"]) if asks else None
        midpoint = None
        if best_bid is not None and best_ask is not None:
            midpoint = (best_bid + best_ask) / 2
        spread = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid
        liquidity_score = _estimate_liquidity_score(bids, asks)
        ts = _parse_timestamp(payload.get("timestamp"))
        return OrderBookSnapshot(
            ts=ts,
            condition_id=str(payload.get("market")),
            token_id=str(payload.get("asset_id") or token_id),
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            last_trade_price=_to_float(payload.get("last_trade_price")),
            liquidity_score=liquidity_score,
            raw_json=payload,
        )

    async def get_orderbooks_batch(self, token_ids: list[str]) -> list[OrderBookSnapshot]:
        """Fetch orderbook snapshots sequentially as a conservative fallback."""
        snapshots: list[OrderBookSnapshot] = []
        for token_id in token_ids:
            try:
                snapshots.append(await self.get_orderbook(token_id))
            except (httpx.HTTPError, ApiError):
                continue
        return snapshots

    async def get_midpoint(self, token_id: str) -> float | None:
        """Fetch midpoint price for a token."""
        payload = await self.get_json("/midpoint", params={"token_id": token_id})
        if isinstance(payload, dict):
            return _to_float(payload.get("mid"))
        return _to_float(payload)

    async def get_spread(self, token_id: str) -> float | None:
        """Fetch spread for a token."""
        payload = await self.get_json("/spread", params={"token_id": token_id})
        if isinstance(payload, dict):
            return _to_float(payload.get("spread"))
        return _to_float(payload)

    async def get_last_trade_price(self, token_id: str) -> float | None:
        """Fetch last trade price for a token."""
        payload = await self.get_json("/last-trade-price", params={"token_id": token_id})
        if isinstance(payload, dict):
            return _to_float(payload.get("price"))
        return _to_float(payload)

    async def get_prices_history(self, token_id: str, interval: str = "1h") -> list[dict[str, Any]]:
        """Fetch historical pricing data for a token."""
        payload = await self.get_json(
            "/prices-history", params={"token_id": token_id, "interval": interval}
        )
        return [dict(item) for item in payload]

    async def get_market_info(self, condition_id: str) -> dict[str, Any]:
        """Fetch CLOB market info."""
        return await self.get_json("/markets/" + condition_id)

    async def get_normalized_market_info(self, condition_id: str) -> NormalizedMarket:
        """Fetch and normalize CLOB market info into the shared market model."""
        payload = await self.get_market_info(condition_id)
        return self._normalize_market(payload)

    async def get_normalized_markets(self, condition_ids: list[str]) -> list[NormalizedMarket]:
        """Fetch and normalize multiple CLOB market records sequentially."""
        markets: list[NormalizedMarket] = []
        for condition_id in condition_ids:
            markets.append(await self.get_normalized_market_info(condition_id))
        return markets

    async def get_server_time(self) -> dict[str, Any]:
        """Fetch CLOB server time."""
        return await self.get_json("/time")

    def _normalize_market(self, payload: dict[str, Any]) -> NormalizedMarket:
        tokens = [
            NormalizedMarketToken(
                condition_id=str(payload.get("condition_id") or payload.get("market") or ""),
                token_id=str(token.get("token_id") or token.get("asset_id") or ""),
                outcome=str(token.get("outcome")) if token.get("outcome") is not None else None,
                side_label=str(token.get("outcome")) if token.get("outcome") is not None else None,
            )
            for token in payload.get("tokens", [])
            if token.get("token_id") or token.get("asset_id")
        ]
        tags = payload.get("tags") or []
        category = None
        if isinstance(tags, list) and tags:
            category = str(tags[0])
        return NormalizedMarket(
            condition_id=str(payload.get("condition_id") or payload.get("market") or ""),
            question=str(
                payload.get("question") or payload.get("title") or payload.get("market_slug")
            ),
            market_id=(
                str(payload.get("question_id"))
                if payload.get("question_id") is not None
                else None
            ),
            event_id=None,
            category=category,
            slug=payload.get("market_slug"),
            end_date=_parse_datetime(payload.get("end_date_iso")),
            active=bool(payload.get("active", True)),
            closed=bool(payload.get("closed", False)),
            neg_risk=bool(payload.get("neg_risk") or False),
            min_tick_size=_to_float(payload.get("minimum_tick_size")),
            min_order_size=_to_float(payload.get("minimum_order_size")),
            tokens=tokens,
            raw_json=payload,
        )


def _estimate_liquidity_score(bids: list[dict[str, Any]], asks: list[dict[str, Any]]) -> float:
    top_sizes = 0.0
    for side in (bids[:3], asks[:3]):
        for level in side:
            top_sizes += _to_float(level.get("size")) or 0.0
    return top_sizes


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

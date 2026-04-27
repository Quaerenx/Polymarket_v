from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.domain import OrderBookSnapshot
from pm_alpha_bot.logging import get_logger

logger = get_logger(__name__)


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, (int, float)):
        raw = float(value)
    else:
        text = str(value).strip()
        raw = float(text)
    if raw > 10_000_000_000:
        raw /= 1000
    return datetime.fromtimestamp(raw, tz=UTC)


def _estimate_liquidity_score(
    bids: Sequence[dict[str, Any]] | None,
    asks: Sequence[dict[str, Any]] | None,
) -> float | None:
    levels = list((bids or [])[:3]) + list((asks or [])[:3])
    if not levels:
        return None
    return sum(_to_float(level.get("size")) or 0.0 for level in levels)


def _best_levels(
    bids: Sequence[dict[str, Any]] | None,
    asks: Sequence[dict[str, Any]] | None,
) -> tuple[float | None, float | None]:
    best_bid = _to_float(bids[0].get("price")) if bids else None
    best_ask = _to_float(asks[0].get("price")) if asks else None
    return best_bid, best_ask


def _merged_snapshot(
    *,
    condition_id: str,
    token_id: str,
    ts: datetime,
    existing: Any | None,
    best_bid: float | None = None,
    best_ask: float | None = None,
    spread: float | None = None,
    last_trade_price: float | None = None,
    liquidity_score: float | None = None,
    raw_json: dict[str, Any] | None = None,
) -> OrderBookSnapshot:
    merged_bid = best_bid if best_bid is not None else getattr(existing, "best_bid", None)
    merged_ask = best_ask if best_ask is not None else getattr(existing, "best_ask", None)
    merged_spread = spread
    if merged_spread is None and merged_bid is not None and merged_ask is not None:
        merged_spread = merged_ask - merged_bid
    midpoint = None
    existing_midpoint = getattr(existing, "midpoint", None)
    if merged_bid is not None and merged_ask is not None:
        midpoint = (merged_bid + merged_ask) / 2
    elif existing_midpoint is not None:
        midpoint = float(existing_midpoint)
    return OrderBookSnapshot(
        ts=ts,
        condition_id=condition_id or str(getattr(existing, "condition_id", "")),
        token_id=token_id,
        best_bid=merged_bid,
        best_ask=merged_ask,
        midpoint=midpoint,
        spread=merged_spread,
        last_trade_price=(
            last_trade_price
            if last_trade_price is not None
            else getattr(existing, "last_trade_price", None)
        ),
        liquidity_score=(
            liquidity_score
            if liquidity_score is not None
            else getattr(existing, "liquidity_score", None)
        ),
        raw_json=raw_json,
    )


def normalize_market_message(
    payload: dict[str, Any],
    get_existing: Callable[[str], Any | None] | None = None,
) -> list[OrderBookSnapshot]:
    """Normalize a WebSocket market event into zero or more orderbook snapshots."""
    event_type = payload.get("event_type")
    if event_type not in {"book", "price_change", "last_trade_price", "best_bid_ask"}:
        return []
    get_existing = get_existing or (lambda _token_id: None)
    market = str(payload.get("market") or payload.get("condition_id") or "")
    ts = _parse_timestamp(payload.get("timestamp"))
    if event_type == "book":
        token_id = str(payload.get("asset_id") or payload.get("token_id") or "")
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        best_bid, best_ask = _best_levels(bids, asks)
        return [
            _merged_snapshot(
                condition_id=market,
                token_id=token_id,
                ts=ts,
                existing=get_existing(token_id),
                best_bid=best_bid,
                best_ask=best_ask,
                last_trade_price=_to_float(payload.get("last_trade_price")),
                liquidity_score=_estimate_liquidity_score(bids, asks),
                raw_json=payload,
            )
        ]
    if event_type == "best_bid_ask":
        token_id = str(payload.get("asset_id") or payload.get("token_id") or "")
        return [
            _merged_snapshot(
                condition_id=market,
                token_id=token_id,
                ts=ts,
                existing=get_existing(token_id),
                best_bid=_to_float(payload.get("best_bid")),
                best_ask=_to_float(payload.get("best_ask")),
                spread=_to_float(payload.get("spread")),
                raw_json=payload,
            )
        ]
    if event_type == "last_trade_price":
        token_id = str(payload.get("asset_id") or payload.get("token_id") or "")
        return [
            _merged_snapshot(
                condition_id=market,
                token_id=token_id,
                ts=ts,
                existing=get_existing(token_id),
                last_trade_price=_to_float(payload.get("price")),
                raw_json=payload,
            )
        ]
    snapshots: list[OrderBookSnapshot] = []
    for change in payload.get("price_changes", []):
        token_id = str(change.get("asset_id") or change.get("token_id") or "")
        if not token_id:
            continue
        snapshots.append(
            _merged_snapshot(
                condition_id=market,
                token_id=token_id,
                ts=ts,
                existing=get_existing(token_id),
                best_bid=_to_float(change.get("best_bid")),
                best_ask=_to_float(change.get("best_ask")),
                raw_json={"event_type": event_type, **change},
            )
        )
    return snapshots


class MarketWebSocketClient:
    """WebSocket client for Polymarket market-channel streaming."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def build_subscription(self, token_ids: Sequence[str]) -> str:
        """Return the official market-channel subscription payload."""
        return json.dumps(
            {
                "assets_ids": list(token_ids),
                "type": "market",
                "custom_feature_enabled": True,
            }
        )

    async def stream_messages(
        self,
        token_ids: Sequence[str],
        *,
        max_messages: int | None = None,
        timeout_sec: float | None = None,
        max_reconnects: int = 3,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield raw JSON messages from the market WebSocket with reconnect and heartbeat."""
        message_count = 0
        reconnect_attempt = 0
        deadline = monotonic() + timeout_sec if timeout_sec is not None else None
        while True:
            if deadline is not None and monotonic() >= deadline:
                return
            try:
                async with websockets.connect(
                    self.settings.poly_ws_market_url,
                    ping_interval=None,
                ) as websocket:
                    await websocket.send(self.build_subscription(token_ids))
                    ping_task = asyncio.create_task(self._ping_loop(websocket))
                    try:
                        while True:
                            if deadline is not None and monotonic() >= deadline:
                                return
                            remaining_timeout = None
                            if deadline is not None:
                                remaining_timeout = max(0.1, deadline - monotonic())
                            try:
                                raw_message = await asyncio.wait_for(
                                    websocket.recv(),
                                    timeout=remaining_timeout,
                                )
                            except TimeoutError:
                                return
                            if isinstance(raw_message, bytes):
                                raw_message = raw_message.decode("utf-8")
                            if raw_message in {"PING", "PONG"}:
                                continue
                            payload = json.loads(raw_message)
                            yield payload
                            message_count += 1
                            if max_messages is not None and message_count >= max_messages:
                                return
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
                return
            except ConnectionClosed:
                reconnect_attempt += 1
                if reconnect_attempt > max_reconnects:
                    raise
                logger.warning(
                    "market_websocket_reconnect",
                    extra={"attempt": reconnect_attempt},
                )
                await asyncio.sleep(min(2**reconnect_attempt, 5))

    async def _ping_loop(self, websocket: Any) -> None:
        """Send the documented heartbeat message periodically."""
        try:
            while True:
                await asyncio.sleep(10)
                await websocket.send("PING")
        except asyncio.CancelledError:
            raise

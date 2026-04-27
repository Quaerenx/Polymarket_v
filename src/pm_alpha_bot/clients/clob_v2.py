from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.domain import (
    BalanceAllowanceSnapshot,
    LiveOpenOrder,
    LiveOrderSubmission,
    LiveTradeFill,
    OrderBookSnapshot,
)


class LiveTradingDisabledError(RuntimeError):
    """Raised when live trading is requested while disabled."""


class LiveTradingClientError(RuntimeError):
    """Raised when the live trading client cannot complete a request."""


@dataclass(slots=True)
class ClobSdkBindings:
    ClobClient: type[Any]
    ApiCreds: type[Any]
    AssetType: type[Any]
    BalanceAllowanceParams: type[Any]
    OrderArgsV2: type[Any]
    PartialCreateOrderOptions: type[Any]
    OpenOrderParams: type[Any]
    TradeParams: type[Any]
    OrderPayload: type[Any]
    BuilderConfig: type[Any] | None = None


def _as_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    return {"value": payload}


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    text = str(value).strip()
    if text.isdigit():
        return _parse_datetime(int(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized == "canceled":
        return "cancelled"
    return normalized or None


def _normalize_orderbook_snapshot(payload: Any, *, token_id: str) -> OrderBookSnapshot:
    data = _as_dict(payload)
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    best_bid = _book_level_price(bids)
    best_ask = _book_level_price(asks)
    midpoint = None
    spread = None
    if best_bid is not None and best_ask is not None:
        midpoint = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
    return OrderBookSnapshot(
        ts=_parse_datetime(data.get("timestamp")) or datetime.now(UTC),
        condition_id=str(data.get("market") or data.get("condition_id") or ""),
        token_id=str(data.get("asset_id") or data.get("token_id") or token_id),
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread=spread,
        last_trade_price=_to_float(data.get("last_trade_price")),
        liquidity_score=None,
        raw_json=data,
    )


def _book_level_price(levels: Any) -> float | None:
    if not isinstance(levels, list) or not levels:
        return None
    level = levels[0]
    if isinstance(level, dict):
        return _to_float(level.get("price"))
    if isinstance(level, (list, tuple)) and level:
        return _to_float(level[0])
    return None


class ClobV2TradingClient:
    """Wrapper around the optional Polymarket CLOB V2 SDK."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sdk_client: Any | None = None,
        sdk_bindings: ClobSdkBindings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._sdk_client: Any | None = sdk_client
        self._sdk_bindings = sdk_bindings

    def _ensure_credentials(self, *, require_live_enabled: bool) -> None:
        if require_live_enabled and not self.settings.enable_live_trading:
            raise LiveTradingDisabledError("Live trading is disabled by configuration.")
        if not self.settings.live_credentials_present:
            raise LiveTradingDisabledError("Live trading credentials are missing.")
        if self.settings.poly_signature_type != 0 and not self.settings.poly_funder_address.strip():
            raise LiveTradingDisabledError(
                "POLY_FUNDER_ADDRESS is required for non-EOA signature types."
            )

    def _load_sdk_bindings(self) -> ClobSdkBindings:
        if self._sdk_bindings is not None:
            return self._sdk_bindings
        try:
            from py_clob_client_v2.client import ClobClient  # type: ignore[import-untyped]
            from py_clob_client_v2.clob_types import (  # type: ignore[import-untyped]
                ApiCreds,
                AssetType,
                BalanceAllowanceParams,
                BuilderConfig,
                OpenOrderParams,
                OrderArgsV2,
                OrderPayload,
                PartialCreateOrderOptions,
                TradeParams,
            )
        except ImportError as exc:
            raise LiveTradingDisabledError(
                "py-clob-client-v2 is not installed. Install the live extra to enable trading."
            ) from exc
        self._sdk_bindings = ClobSdkBindings(
            ClobClient=ClobClient,
            ApiCreds=ApiCreds,
            AssetType=AssetType,
            BalanceAllowanceParams=BalanceAllowanceParams,
            BuilderConfig=BuilderConfig,
            OrderArgsV2=OrderArgsV2,
            PartialCreateOrderOptions=PartialCreateOrderOptions,
            OpenOrderParams=OpenOrderParams,
            TradeParams=TradeParams,
            OrderPayload=OrderPayload,
        )
        return self._sdk_bindings

    def _ensure_sdk(self, *, require_live_enabled: bool) -> Any:
        self._ensure_credentials(require_live_enabled=require_live_enabled)
        if self._sdk_client is not None:
            return self._sdk_client
        bindings = self._load_sdk_bindings()
        builder_config: Any | None = None
        if self.settings.poly_builder_code and bindings.BuilderConfig is not None:
            builder_config = bindings.BuilderConfig(builder_code=self.settings.poly_builder_code)
        creds = bindings.ApiCreds(
            api_key=self.settings.poly_api_key,
            api_secret=self.settings.poly_api_secret,
            api_passphrase=self.settings.poly_api_passphrase,
        )
        self._sdk_client = bindings.ClobClient(
            host=self.settings.poly_clob_host,
            key=self.settings.poly_private_key,
            creds=creds,
            chain_id=137,
            signature_type=self.settings.poly_signature_type,
            funder=self.settings.poly_funder_address or None,
            builder_config=builder_config,
        )
        return self._sdk_client

    def create_limit_order(
        self,
        *,
        token_id: str,
        price: float,
        size: float,
        side: str,
        tick_size: str | None = None,
        neg_risk: bool | None = None,
        expiration: int = 0,
        post_only: bool = True,
        order_type: str = "GTC",
    ) -> LiveOrderSubmission:
        """Create and submit a post-only live limit order."""
        resolved_order_type = order_type.upper()
        resolved_side = side.upper()
        if resolved_order_type not in {"GTC", "GTD"}:
            raise LiveTradingClientError("Only GTC and GTD live orders are supported.")
        if not post_only:
            raise LiveTradingClientError("Only post-only live orders are supported.")
        if resolved_order_type == "GTD" and expiration <= 0:
            raise LiveTradingClientError("GTD orders require a positive expiration timestamp.")
        if resolved_side not in {"BUY", "SELL"}:
            raise LiveTradingClientError(f"Unsupported order side: {side}")

        client = self._ensure_sdk(require_live_enabled=True)
        bindings = self._load_sdk_bindings()
        options = bindings.PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        try:
            order = client.create_order(
                bindings.OrderArgsV2(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=resolved_side,
                    expiration=expiration,
                ),
                options,
            )
            response = client.post_order(
                order,
                order_type=resolved_order_type,
                post_only=post_only,
            )
        except Exception as exc:  # pragma: no cover - SDK/network error mapping
            raise LiveTradingClientError(f"Failed to submit live order: {exc}") from exc
        payload = _as_dict(response)
        return LiveOrderSubmission(
            success=bool(payload.get("success", False)),
            external_order_id=str(
                payload.get("orderID") or payload.get("orderId") or payload.get("id") or ""
            )
            or None,
            status=_normalize_status(payload.get("status")),
            order_type=str(payload.get("orderType") or resolved_order_type),
            post_only=bool(payload.get("postOnly", post_only)),
            taking_amount=str(payload.get("takingAmount") or "") or None,
            making_amount=str(payload.get("makingAmount") or "") or None,
            trade_ids=[str(item) for item in payload.get("tradeIDs", []) if item],
            transaction_hashes=[
                str(item) for item in payload.get("transactionsHashes", []) if item
            ],
            error_msg=str(payload.get("errorMsg") or "") or None,
            raw_json=payload,
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a single order by ID."""
        client = self._ensure_sdk(require_live_enabled=False)
        bindings = self._load_sdk_bindings()
        try:
            response = client.cancel_order(bindings.OrderPayload(orderID=order_id))
        except Exception as exc:  # pragma: no cover - SDK/network error mapping
            raise LiveTradingClientError(f"Failed to cancel live order {order_id}: {exc}") from exc
        return _as_dict(response)

    def get_open_orders(
        self,
        *,
        order_id: str | None = None,
        market: str | None = None,
        asset_id: str | None = None,
        only_first_page: bool = True,
    ) -> list[LiveOpenOrder]:
        """Return normalized open orders."""
        client = self._ensure_sdk(require_live_enabled=False)
        bindings = self._load_sdk_bindings()
        params = None
        if order_id or market or asset_id:
            params = bindings.OpenOrderParams(id=order_id, market=market, asset_id=asset_id)
        try:
            response = client.get_open_orders(params=params, only_first_page=only_first_page)
        except Exception as exc:  # pragma: no cover - SDK/network error mapping
            raise LiveTradingClientError(f"Failed to query open live orders: {exc}") from exc
        return [self._normalize_open_order(item) for item in response]

    def get_order(self, order_id: str) -> LiveOpenOrder:
        """Return the latest state for a single order."""
        client = self._ensure_sdk(require_live_enabled=False)
        try:
            response = client.get_order(order_id)
        except Exception as exc:  # pragma: no cover - SDK/network error mapping
            raise LiveTradingClientError(f"Failed to query live order {order_id}: {exc}") from exc
        return self._normalize_open_order(response)

    def get_trades(
        self,
        *,
        market: str | None = None,
        asset_id: str | None = None,
        after: int | None = None,
        before: int | None = None,
        maker_address: str | None = None,
        only_first_page: bool = True,
    ) -> list[LiveTradeFill]:
        """Return normalized trade history."""
        client = self._ensure_sdk(require_live_enabled=False)
        bindings = self._load_sdk_bindings()
        params = bindings.TradeParams(
            market=market,
            asset_id=asset_id,
            after=after,
            before=before,
            maker_address=maker_address,
        )
        try:
            response = client.get_trades(params=params, only_first_page=only_first_page)
        except Exception as exc:  # pragma: no cover - SDK/network error mapping
            raise LiveTradingClientError(f"Failed to query live trades: {exc}") from exc
        return [self._normalize_trade(item) for item in response]

    def get_orderbook_snapshot(self, token_id: str) -> OrderBookSnapshot:
        """Fetch a synchronous CLOB orderbook snapshot immediately before live submit."""
        sdk_client = self._sdk_client
        if sdk_client is not None:
            for method_name in ("get_orderbook", "get_order_book"):
                method = getattr(sdk_client, method_name, None)
                if method is None:
                    continue
                try:
                    return _normalize_orderbook_snapshot(method(token_id), token_id=token_id)
                except Exception as exc:  # pragma: no cover - SDK/network error mapping
                    raise LiveTradingClientError(
                        f"Failed to query CLOB orderbook for {token_id}: {exc}"
                    ) from exc
        try:
            with httpx.Client(
                base_url=self.settings.poly_clob_host,
                timeout=self.settings.http_timeout_sec,
            ) as client:
                response = client.get("/book", params={"token_id": token_id})
                response.raise_for_status()
                return _normalize_orderbook_snapshot(response.json(), token_id=token_id)
        except Exception as exc:  # pragma: no cover - network dependent
            raise LiveTradingClientError(
                f"Failed to query CLOB orderbook for {token_id}: {exc}"
            ) from exc

    def post_heartbeat(self, heartbeat_id: str = "") -> dict[str, Any]:
        """Send a heartbeat for live-order session safety."""
        client = self._ensure_sdk(require_live_enabled=False)
        try:
            response = client.post_heartbeat(heartbeat_id)
        except Exception as exc:  # pragma: no cover - SDK/network error mapping
            raise LiveTradingClientError(f"Failed to post heartbeat: {exc}") from exc
        return _as_dict(response)

    def get_balance_allowance(
        self,
        *,
        asset_type: str,
        token_id: str | None = None,
    ) -> BalanceAllowanceSnapshot:
        """Return normalized balance and allowance for a collateral or token asset."""
        client = self._ensure_sdk(require_live_enabled=False)
        bindings = self._load_sdk_bindings()
        resolved_asset_type = asset_type.upper()
        if resolved_asset_type not in {"COLLATERAL", "CONDITIONAL"}:
            raise LiveTradingClientError(f"Unsupported asset type: {asset_type}")
        params = bindings.BalanceAllowanceParams(
            asset_type=getattr(bindings.AssetType, resolved_asset_type),
            token_id=token_id,
        )
        try:
            response = client.get_balance_allowance(params=params)
        except Exception as exc:  # pragma: no cover - SDK/network error mapping
            raise LiveTradingClientError(
                f"Failed to query balance allowance for {resolved_asset_type}: {exc}"
            ) from exc
        payload = _as_dict(response)
        balance = _to_float(payload.get("balance"))
        allowance = _to_float(payload.get("allowance"))
        available: float | None = None
        if balance is not None and allowance is not None:
            available = min(balance, allowance)
        return BalanceAllowanceSnapshot(
            asset_type=resolved_asset_type,
            token_id=token_id,
            balance=balance,
            allowance=allowance,
            available=available,
            raw_json=payload,
        )

    def cancel_all(self) -> dict[str, Any]:
        """Cancel all remote orders for the authenticated account."""
        client = self._ensure_sdk(require_live_enabled=False)
        try:
            response = client.cancel_all()
        except Exception as exc:  # pragma: no cover - SDK/network error mapping
            raise LiveTradingClientError(f"Failed to cancel all live orders: {exc}") from exc
        return _as_dict(response)

    def _normalize_open_order(self, payload: Any) -> LiveOpenOrder:
        item = _as_dict(payload)
        external_order_id = str(item.get("id") or item.get("orderID") or item.get("orderId") or "")
        if not external_order_id:
            raise LiveTradingClientError(f"Order response is missing an ID: {item}")
        return LiveOpenOrder(
            external_order_id=external_order_id,
            status=_normalize_status(item.get("status")),
            condition_id=str(item.get("market") or "") or None,
            token_id=str(item.get("asset_id") or item.get("assetId") or "") or None,
            side=str(item.get("side") or "") or None,
            original_size=_to_float(item.get("original_size") or item.get("size")),
            size_matched=_to_float(item.get("size_matched")),
            price=_to_float(item.get("price")),
            outcome=str(item.get("outcome") or "") or None,
            order_type=str(item.get("order_type") or item.get("orderType") or "") or None,
            expiration=_to_int(item.get("expiration")),
            created_at=_parse_datetime(item.get("created_at") or item.get("createdAt")),
            raw_json=item,
        )

    def _normalize_trade(self, payload: Any) -> LiveTradeFill:
        item = _as_dict(payload)
        maker_orders = item.get("maker_orders")
        maker_order_ids: list[str] = []
        if isinstance(maker_orders, list):
            for maker_order in maker_orders:
                if not isinstance(maker_order, dict):
                    continue
                order_id = maker_order.get("order_id") or maker_order.get("id")
                if order_id:
                    maker_order_ids.append(str(order_id))
        return LiveTradeFill(
            ts=_parse_datetime(item.get("match_time") or item.get("last_update"))
            or datetime.now(UTC),
            trade_id=str(item.get("id") or "") or None,
            taker_order_id=str(item.get("taker_order_id") or "") or None,
            maker_order_ids=maker_order_ids,
            condition_id=str(item.get("market") or "") or None,
            token_id=str(item.get("asset_id") or item.get("assetId") or "") or None,
            side=str(item.get("side") or "") or None,
            price=_to_float(item.get("price")),
            size=_to_float(item.get("size")),
            fee=_to_float(item.get("fee") or item.get("fee_usdc") or item.get("feeUsdc")),
            status=_normalize_status(item.get("status")),
            tx_hash=str(item.get("transaction_hash") or item.get("transactionHash") or "") or None,
            raw_json=item,
        )

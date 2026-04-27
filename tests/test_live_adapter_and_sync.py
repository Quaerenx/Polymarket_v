from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pm_alpha_bot.clients.clob_v2 import (
    ClobSdkBindings,
    ClobV2TradingClient,
    LiveTradingDisabledError,
)
from pm_alpha_bot.config import Settings
from pm_alpha_bot.db.models import Market, MarketSnapshot, Signal
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.db.session import session_scope
from pm_alpha_bot.domain import PreparedLiveOrder
from pm_alpha_bot.execution.live import (
    LiveKillSwitchTriggeredError,
    execute_live_once,
    reset_live_kill_switch,
    run_live_supervisor,
    run_live_supervisor_iteration,
    sync_live_orders,
)


class _FakeApiCreds:
    def __init__(self, api_key: str, api_secret: str, api_passphrase: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase


class _FakeAssetType:
    COLLATERAL = "COLLATERAL"
    CONDITIONAL = "CONDITIONAL"


class _FakeBalanceAllowanceParams:
    def __init__(
        self,
        asset_type: str | None = None,
        token_id: str | None = None,
        signature_type: int = -1,
    ) -> None:
        self.asset_type = asset_type
        self.token_id = token_id
        self.signature_type = signature_type


class _FakeOrderArgsV2:
    def __init__(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        expiration: int = 0,
    ) -> None:
        self.token_id = token_id
        self.price = price
        self.size = size
        self.side = side
        self.expiration = expiration


class _FakePartialCreateOrderOptions:
    def __init__(self, tick_size: str | None = None, neg_risk: bool | None = None) -> None:
        self.tick_size = tick_size
        self.neg_risk = neg_risk


class _FakeOpenOrderParams:
    def __init__(
        self,
        id: str | None = None,
        market: str | None = None,
        asset_id: str | None = None,
    ) -> None:
        self.id = id
        self.market = market
        self.asset_id = asset_id


class _FakeTradeParams:
    def __init__(
        self,
        id: str | None = None,
        maker_address: str | None = None,
        market: str | None = None,
        asset_id: str | None = None,
        before: int | None = None,
        after: int | None = None,
    ) -> None:
        self.id = id
        self.maker_address = maker_address
        self.market = market
        self.asset_id = asset_id
        self.before = before
        self.after = after


class _FakeOrderPayload:
    def __init__(self, order_id: str | None = None, **kwargs: str) -> None:
        self.orderID = order_id or kwargs["orderID"]


class _FakeSdkClient:
    def __init__(
        self,
        *,
        order_response: dict[str, Any] | None = None,
        open_orders: list[dict[str, Any]] | None = None,
        order_detail: dict[str, Any] | None = None,
        trades: list[dict[str, Any]] | None = None,
        balance_allowances: dict[tuple[str, str | None], dict[str, Any]] | None = None,
        orderbook_response: dict[str, Any] | None = None,
        heartbeat_error: Exception | None = None,
    ) -> None:
        self.order_response = order_response or {}
        self.open_orders = open_orders or []
        self.order_detail = order_detail or {}
        self.trades = trades or []
        self.balance_allowances = balance_allowances or {}
        self.orderbook_response = orderbook_response
        self.heartbeat_error = heartbeat_error
        self.last_order_args: _FakeOrderArgsV2 | None = None
        self.last_order_options: _FakePartialCreateOrderOptions | None = None
        self.last_post_order_type: str | None = None
        self.last_post_only: bool | None = None
        self.post_order_calls = 0
        self.last_cancel_order_id: str | None = None
        self.cancel_all_called = False
        self.last_open_params: _FakeOpenOrderParams | None = None
        self.last_trade_params: _FakeTradeParams | None = None
        self.last_balance_params: _FakeBalanceAllowanceParams | None = None
        self.heartbeat_ids_sent: list[str] = []
        self.orderbook_calls = 0

    def create_order(
        self,
        order_args: _FakeOrderArgsV2,
        options: _FakePartialCreateOrderOptions,
    ) -> dict[str, Any]:
        self.last_order_args = order_args
        self.last_order_options = options
        return {"signed": True}

    def post_order(
        self,
        order: dict[str, Any],
        order_type: str = "GTC",
        post_only: bool = False,
    ) -> dict[str, Any]:
        _ = order
        self.post_order_calls += 1
        self.last_post_order_type = order_type
        self.last_post_only = post_only
        return self.order_response

    def cancel_order(self, payload: _FakeOrderPayload) -> dict[str, Any]:
        self.last_cancel_order_id = payload.orderID
        return {"canceled": [payload.orderID], "not_canceled": {}}

    def get_open_orders(
        self,
        params: _FakeOpenOrderParams | None = None,
        only_first_page: bool = False,
    ) -> list[dict[str, Any]]:
        _ = only_first_page
        self.last_open_params = params
        return self.open_orders

    def get_order(self, order_id: str) -> dict[str, Any]:
        if self.order_detail:
            return self.order_detail
        return {
            "id": order_id,
            "status": "live",
            "market": "cond-1",
            "asset_id": "token-1",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "0",
            "price": "0.41",
            "order_type": "GTC",
            "created_at": str(int(datetime.now(UTC).timestamp())),
        }

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        self.orderbook_calls += 1
        if self.orderbook_response is not None:
            return self.orderbook_response
        return {
            "market": "cond-1",
            "asset_id": token_id,
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "bids": [{"price": "0.39", "size": "100"}],
            "asks": [{"price": "0.42", "size": "100"}],
        }

    def get_trades(
        self,
        params: _FakeTradeParams | None = None,
        only_first_page: bool = False,
    ) -> list[dict[str, Any]]:
        _ = only_first_page
        self.last_trade_params = params
        return self.trades

    def post_heartbeat(self, heartbeat_id: str = "") -> dict[str, Any]:
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        self.heartbeat_ids_sent.append(heartbeat_id)
        return {"heartbeat_id": f"hb-{len(self.heartbeat_ids_sent)}"}

    def get_balance_allowance(
        self,
        params: _FakeBalanceAllowanceParams | None = None,
    ) -> dict[str, Any]:
        self.last_balance_params = params
        key = (
            str(params.asset_type) if params is not None and params.asset_type is not None else "",
            params.token_id if params is not None else None,
        )
        return self.balance_allowances.get(
            key,
            {"balance": "1000", "allowance": "1000"},
        )

    def cancel_all(self) -> dict[str, Any]:
        self.cancel_all_called = True
        return {
            "canceled": [str(item.get("id")) for item in self.open_orders if item.get("id")],
            "not_canceled": {},
        }


def _sdk_bindings() -> ClobSdkBindings:
    return ClobSdkBindings(
        ClobClient=_FakeSdkClient,
        ApiCreds=_FakeApiCreds,
        AssetType=_FakeAssetType,
        BalanceAllowanceParams=_FakeBalanceAllowanceParams,
        OrderArgsV2=_FakeOrderArgsV2,
        PartialCreateOrderOptions=_FakePartialCreateOrderOptions,
        OpenOrderParams=_FakeOpenOrderParams,
        TradeParams=_FakeTradeParams,
        OrderPayload=_FakeOrderPayload,
        BuilderConfig=None,
    )


def _live_settings(sqlite_settings: Settings) -> Settings:
    return sqlite_settings.model_copy(
        update={
            "enable_live_trading": True,
            "poly_private_key": "pk",
            "poly_api_key": "api",
            "poly_api_secret": "secret",
            "poly_api_passphrase": "pass",
        }
    )


def test_clob_v2_client_normalizes_submit_query_and_cancel(sqlite_settings: Settings) -> None:
    settings = _live_settings(sqlite_settings)
    fake_sdk = _FakeSdkClient(
        order_response={
            "success": True,
            "orderID": "ord-1",
            "status": "live",
            "makingAmount": "100",
            "takingAmount": "41",
            "errorMsg": "",
            "tradeIDs": ["trade-1"],
            "transactionsHashes": ["0xtx"],
        },
        open_orders=[
            {
                "id": "ord-1",
                "status": "live",
                "market": "cond-1",
                "asset_id": "token-1",
                "side": "BUY",
                "original_size": "10",
                "size_matched": "4",
                "price": "0.41",
                "order_type": "GTC",
                "created_at": "1710000000",
            }
        ],
        trades=[
            {
                "id": "trade-1",
                "market": "cond-1",
                "asset_id": "token-1",
                "side": "BUY",
                "size": "4",
                "price": "0.41",
                "status": "CONFIRMED",
                "match_time": "1710000010",
                "transaction_hash": "0xtx",
                "maker_orders": [{"order_id": "ord-1", "matched_amount": "4"}],
            }
        ],
    )
    client = ClobV2TradingClient(
        settings,
        sdk_client=fake_sdk,
        sdk_bindings=_sdk_bindings(),
    )

    submission = client.create_limit_order(
        token_id="token-1",
        price=0.41,
        size=10.0,
        side="BUY",
        tick_size="0.01",
        neg_risk=False,
    )
    open_orders = client.get_open_orders(asset_id="token-1")
    trades = client.get_trades(asset_id="token-1")
    cancel_summary = client.cancel_order("ord-1")

    assert submission.external_order_id == "ord-1"
    assert submission.status == "live"
    assert fake_sdk.last_order_args is not None
    assert fake_sdk.last_order_args.price == pytest.approx(0.41)
    assert fake_sdk.last_post_order_type == "GTC"
    assert fake_sdk.last_post_only is True
    assert open_orders[0].size_matched == pytest.approx(4.0)
    assert trades[0].maker_order_ids == ["ord-1"]
    assert trades[0].tx_hash == "0xtx"
    assert cancel_summary["canceled"] == ["ord-1"]


def test_sync_live_orders_backfills_remote_open_orders(
    repo,
    sqlite_settings: Settings,
) -> None:
    settings = sqlite_settings.model_copy(
        update={
            "enable_live_trading": False,
            "poly_private_key": "pk",
            "poly_api_key": "api",
            "poly_api_secret": "secret",
            "poly_api_passphrase": "pass",
        }
    )
    client = ClobV2TradingClient(
        settings,
        sdk_client=_FakeSdkClient(
            open_orders=[
                {
                    "id": "ord-remote",
                    "status": "live",
                    "market": "cond-remote",
                    "asset_id": "token-remote",
                    "side": "BUY",
                    "original_size": "8",
                    "size_matched": "0",
                    "price": "0.33",
                    "order_type": "GTC",
                    "created_at": "1710000000",
                }
            ]
        ),
        sdk_bindings=_sdk_bindings(),
    )

    summary = sync_live_orders(repo, settings=settings, client=client)
    order = repo.get_order_by_external_order_id("ord-remote")

    assert summary["orders_synced"] == 1
    assert order is not None
    assert order.mode == "live"
    assert order.status == "live"
    assert order.token_id == "token-remote"


def test_execute_live_once_submits_and_syncs_fills(
    repo,
    sqlite_settings: Settings,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    settings = _live_settings(sqlite_settings)
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Question",
            category="POLITICS",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
            end_date=now + timedelta(days=1),
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            best_bid=0.39,
            best_ask=0.42,
            midpoint=0.405,
            spread=0.03,
        )
    )
    repo.session.add(
        Signal(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            direction="BUY",
            market_midpoint=0.405,
            effective_entry_price=0.419,
            fair_prob=0.70,
            edge_bps=2810,
            confidence=0.9,
            source_wallet_count=2,
            reason_json={"signal_id": 1},
            status="new",
        )
    )
    repo.session.commit()

    async def _not_blocked(_settings: Settings) -> bool:
        return False

    monkeypatch.setattr("pm_alpha_bot.execution.live._check_geoblock", _not_blocked)
    fake_sdk = _FakeSdkClient(
        order_response={
            "success": True,
            "orderID": "ord-1",
            "status": "live",
            "errorMsg": "",
            "tradeIDs": ["trade-1"],
        },
        open_orders=[
            {
                "id": "ord-1",
                "status": "live",
                "market": "cond-1",
                "asset_id": "token-1",
                "side": "BUY",
                "original_size": "10",
                "size_matched": "4",
                "price": "0.41",
                "order_type": "GTC",
                "created_at": str(int(now.timestamp())),
            }
        ],
        trades=[
            {
                "id": "trade-1",
                "market": "cond-1",
                "asset_id": "token-1",
                "side": "BUY",
                "size": "4",
                "price": "0.41",
                "status": "CONFIRMED",
                "match_time": str(int(now.timestamp())),
                "transaction_hash": "0xtx",
                "maker_orders": [{"order_id": "ord-1", "matched_amount": "4"}],
            }
        ],
    )
    client = ClobV2TradingClient(
        settings,
        sdk_client=fake_sdk,
        sdk_bindings=_sdk_bindings(),
    )

    result = execute_live_once(
        repo,
        settings=settings,
        explicit_live=True,
        client=client,
    )
    order = repo.get_order_by_external_order_id("ord-1")

    assert result.external_order_id == "ord-1"
    assert result.submitted_status == "live"
    assert result.synced_status == "partially_filled"
    assert result.fills_synced == 1
    assert order is not None
    assert order.status == "partially_filled"
    assert order.price == pytest.approx(0.41)
    assert order.reason_json is not None
    idempotency_key = order.reason_json["live_submission_idempotency_key"]
    lock_key = order.reason_json["live_submission_lock_key"]
    lock_state = repo.get_runtime_state(lock_key)
    assert lock_state is not None
    assert lock_state.state_json is not None
    assert lock_state.state_json["state"] == "submitted"
    assert lock_state.state_json["idempotency_key"] == idempotency_key
    assert lock_state.state_json["local_order_id"] == order.id
    assert lock_state.state_json["external_order_id"] == "ord-1"
    assert lock_state.state_json["pre_submit_orderbook"]["best_ask"] == pytest.approx(0.42)
    assert order.reason_json["pre_submit_orderbook"]["best_ask"] == pytest.approx(0.42)
    assert fake_sdk.orderbook_calls == 1
    assert repo.filled_size_for_order(order.id) == pytest.approx(4.0)
    processed_signals = repo.list_recent_signals(status="processed", limit=5)
    assert len(processed_signals) == 1
    assert processed_signals[0].reason_json is not None
    assert processed_signals[0].reason_json["live_order_id"] == order.id
    assert fake_sdk.post_order_calls == 1
    with pytest.raises(LiveTradingDisabledError, match="No fresh signal"):
        execute_live_once(
            repo,
            settings=settings,
            explicit_live=True,
            client=client,
        )
    assert fake_sdk.post_order_calls == 1
    event_types = [
        event.event_type
        for event in repo.list_runtime_events(category="live", limit=10)
    ]
    assert "live_order_submitted" in event_types
    assert "live_sync_completed" in event_types


def test_run_live_supervisor_iteration_posts_heartbeat_and_skips_submit_when_active(
    repo,
    sqlite_settings: Settings,
) -> None:
    settings = _live_settings(sqlite_settings)
    client = ClobV2TradingClient(
        settings,
        sdk_client=_FakeSdkClient(
            open_orders=[
                {
                    "id": "ord-1",
                    "status": "live",
                    "market": "cond-1",
                    "asset_id": "token-1",
                    "side": "BUY",
                    "original_size": "5",
                    "size_matched": "0",
                    "price": "0.41",
                    "order_type": "GTC",
                    "created_at": "1710000000",
                }
            ]
        ),
        sdk_bindings=_sdk_bindings(),
    )

    summary = run_live_supervisor_iteration(
        repo,
        settings=settings,
        client=client,
        heartbeat_id="hb-0",
        submit_new_orders=True,
    )

    assert summary["heartbeat_id"] == "hb-1"
    assert summary["orders_synced"] == 1
    assert summary["active_orders"] == 1
    assert summary["submitted"] is False
    assert client._sdk_client.heartbeat_ids_sent == ["hb-0"]


def test_execute_live_once_rejects_stale_pre_submit_orderbook(
    repo,
    sqlite_settings: Settings,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    settings = _live_settings(sqlite_settings)
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Question",
            category="POLITICS",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
            end_date=now + timedelta(days=1),
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            best_bid=0.39,
            best_ask=0.42,
            midpoint=0.405,
            spread=0.03,
        )
    )
    repo.session.add(
        Signal(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            direction="BUY",
            market_midpoint=0.405,
            effective_entry_price=0.419,
            fair_prob=0.70,
            edge_bps=2810,
            confidence=0.9,
            source_wallet_count=2,
            reason_json={"signal_id": 1},
            status="new",
        )
    )
    repo.session.commit()

    async def _not_blocked(_settings: Settings) -> bool:
        return False

    fake_sdk = _FakeSdkClient(
        orderbook_response={
            "market": "cond-1",
            "asset_id": "token-1",
            "timestamp": str(int((now - timedelta(seconds=30)).timestamp())),
            "bids": [{"price": "0.39", "size": "100"}],
            "asks": [{"price": "0.42", "size": "100"}],
        }
    )
    monkeypatch.setattr("pm_alpha_bot.execution.live._check_geoblock", _not_blocked)
    client = ClobV2TradingClient(settings, sdk_client=fake_sdk, sdk_bindings=_sdk_bindings())

    with pytest.raises(LiveTradingDisabledError, match="Pre-submit CLOB orderbook is stale"):
        execute_live_once(repo, settings=settings, explicit_live=True, client=client)

    failed_orders = repo.list_orders(mode="live", statuses=["failed"], limit=5)
    rejected_signals = repo.list_recent_signals(status="live_rejected", limit=5)
    assert fake_sdk.orderbook_calls == 1
    assert fake_sdk.post_order_calls == 0
    assert len(failed_orders) == 1
    assert failed_orders[0].reason_json is not None
    assert failed_orders[0].reason_json["live_submission_state"] == "pre_submit_rejected"
    assert len(rejected_signals) == 1
    assert rejected_signals[0].reason_json is not None
    assert "Pre-submit CLOB orderbook is stale" in rejected_signals[0].reason_json[
        "live_status_reason"
    ]


def test_run_live_supervisor_loop_can_submit_when_idle(
    repo,
    sqlite_settings: Settings,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    settings = _live_settings(sqlite_settings)
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Question",
            category="POLITICS",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
            end_date=now + timedelta(days=1),
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            best_bid=0.39,
            best_ask=0.42,
            midpoint=0.405,
            spread=0.03,
        )
    )
    repo.session.add(
        Signal(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            direction="BUY",
            market_midpoint=0.405,
            effective_entry_price=0.419,
            fair_prob=0.70,
            edge_bps=2810,
            confidence=0.9,
            source_wallet_count=2,
            reason_json={"signal_id": 1},
            status="new",
        )
    )
    repo.session.commit()

    async def _not_blocked(_settings: Settings) -> bool:
        return False

    monkeypatch.setattr("pm_alpha_bot.execution.live._check_geoblock", _not_blocked)
    client = ClobV2TradingClient(
        settings,
        sdk_client=_FakeSdkClient(
            order_response={
                "success": True,
                "orderID": "ord-2",
                "status": "live",
                "errorMsg": "",
            },
            open_orders=[],
            order_detail={
                "id": "ord-2",
                "status": "live",
                "market": "cond-1",
                "asset_id": "token-1",
                "side": "BUY",
                "original_size": "10",
                "size_matched": "0",
                "price": "0.41",
                "order_type": "GTC",
                "created_at": str(int(now.timestamp())),
            },
            trades=[],
        ),
        sdk_bindings=_sdk_bindings(),
    )

    @contextmanager
    def _repo_factory() -> Iterator[Any]:
        yield repo

    summaries = run_live_supervisor(
        _repo_factory,
        settings=settings,
        explicit_live=True,
        client=client,
        iterations=1,
        submit_new_orders=True,
    )
    order = repo.get_order_by_external_order_id("ord-2")

    assert len(summaries) == 1
    assert summaries[0]["heartbeat_id"] == "hb-1"
    assert summaries[0]["submitted"] is True
    assert summaries[0]["submitted_order_id"] == "ord-2"
    assert order is not None
    assert order.status == "live"


def test_execute_live_once_blocks_on_insufficient_collateral(
    repo,
    sqlite_settings: Settings,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    settings = _live_settings(sqlite_settings)
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Question",
            category="POLITICS",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
            end_date=now + timedelta(days=1),
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            best_bid=0.39,
            best_ask=0.42,
            midpoint=0.405,
            spread=0.03,
        )
    )
    repo.session.add(
        Signal(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            direction="BUY",
            market_midpoint=0.405,
            effective_entry_price=0.419,
            fair_prob=0.70,
            edge_bps=2810,
            confidence=0.9,
            source_wallet_count=2,
            reason_json={"signal_id": 1},
            status="new",
        )
    )
    repo.session.commit()

    async def _not_blocked(_settings: Settings) -> bool:
        return False

    monkeypatch.setattr("pm_alpha_bot.execution.live._check_geoblock", _not_blocked)
    client = ClobV2TradingClient(
        settings,
        sdk_client=_FakeSdkClient(
            balance_allowances={
                ("COLLATERAL", None): {"balance": "1", "allowance": "1"},
            }
        ),
        sdk_bindings=_sdk_bindings(),
    )

    with pytest.raises(LiveTradingDisabledError, match="insufficient_collateral_balance_allowance"):
        execute_live_once(
            repo,
            settings=settings,
            explicit_live=True,
            client=client,
        )
    rejected_signals = repo.list_recent_signals(status="live_rejected", limit=5)
    assert len(rejected_signals) == 1
    assert rejected_signals[0].reason_json is not None
    assert "insufficient_collateral_balance_allowance" in rejected_signals[0].reason_json[
        "live_status_reason"
    ]


def test_execute_live_once_persists_rejected_signal_when_outer_scope_rolls_back(
    sqlite_settings: Settings,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    settings = _live_settings(sqlite_settings)
    with session_scope(settings) as session:
        repo = Repository(session)
        repo.session.add(
            Market(
                condition_id="cond-1",
                question="Question",
                category="POLITICS",
                active=True,
                closed=False,
                min_tick_size=0.01,
                min_order_size=1.0,
                end_date=now + timedelta(days=1),
            )
        )
        repo.session.add(
            MarketSnapshot(
                ts=now,
                condition_id="cond-1",
                token_id="token-1",
                best_bid=0.39,
                best_ask=0.42,
                midpoint=0.405,
                spread=0.03,
            )
        )
        repo.session.add(
            Signal(
                ts=now,
                condition_id="cond-1",
                token_id="token-1",
                direction="BUY",
                market_midpoint=0.405,
                effective_entry_price=0.419,
                fair_prob=0.70,
                edge_bps=2810,
                confidence=0.9,
                source_wallet_count=2,
                reason_json={"signal_id": 1},
                status="new",
            )
        )

    async def _not_blocked(_settings: Settings) -> bool:
        return False

    monkeypatch.setattr("pm_alpha_bot.execution.live._check_geoblock", _not_blocked)
    client = ClobV2TradingClient(
        settings,
        sdk_client=_FakeSdkClient(
            balance_allowances={
                ("COLLATERAL", None): {"balance": "1", "allowance": "1"},
            }
        ),
        sdk_bindings=_sdk_bindings(),
    )
    with (
        pytest.raises(LiveTradingDisabledError, match="insufficient_collateral_balance_allowance"),
        session_scope(settings) as session,
    ):
        execute_live_once(
            Repository(session),
            settings=settings,
            explicit_live=True,
            client=client,
        )

    with session_scope(settings) as session:
        rejected_signals = Repository(session).list_recent_signals(
            status="live_rejected",
            limit=5,
        )
        assert len(rejected_signals) == 1
        assert rejected_signals[0].reason_json is not None
        assert "insufficient_collateral_balance_allowance" in rejected_signals[0].reason_json[
            "live_status_reason"
        ]


def test_execute_live_once_blocks_when_persistent_kill_switch_active(
    repo,
    sqlite_settings: Settings,
    monkeypatch,
) -> None:
    settings = _live_settings(sqlite_settings)
    repo.upsert_runtime_state(
        "live_kill_switch",
        {
            "tripped": True,
            "reason": "previous failure",
            "tripped_at": datetime.now(UTC).isoformat(),
        },
    )
    repo.session.commit()

    async def _not_blocked(_settings: Settings) -> bool:
        return False

    monkeypatch.setattr("pm_alpha_bot.execution.live._check_geoblock", _not_blocked)
    with pytest.raises(LiveTradingDisabledError, match="Persistent live kill switch"):
        execute_live_once(
            repo,
            settings=settings,
            explicit_live=True,
            client=ClobV2TradingClient(
                settings,
                sdk_client=_FakeSdkClient(),
                sdk_bindings=_sdk_bindings(),
            ),
        )


def test_run_live_supervisor_kill_switch_can_cancel_all(
    repo,
    sqlite_settings: Settings,
    monkeypatch,
) -> None:
    settings = _live_settings(sqlite_settings).model_copy(
        update={
            "live_max_consecutive_errors": 2,
            "live_cancel_all_on_kill_switch": True,
        }
    )
    order = repo.create_live_order(
        PreparedLiveOrder(
            signal_id=1,
            condition_id="cond-1",
            token_id="token-1",
            side="BUY",
            price=0.40,
            size=5.0,
        ),
        status="live",
        external_order_id="ord-live",
    )
    repo.session.commit()
    assert order.status == "live"

    async def _not_blocked(_settings: Settings) -> bool:
        return False

    monkeypatch.setattr("pm_alpha_bot.execution.live._check_geoblock", _not_blocked)
    client = ClobV2TradingClient(
        settings,
        sdk_client=_FakeSdkClient(
            open_orders=[{"id": "ord-live"}],
            heartbeat_error=RuntimeError("heartbeat failed"),
        ),
        sdk_bindings=_sdk_bindings(),
    )

    @contextmanager
    def _repo_factory() -> Iterator[Any]:
        yield repo

    with pytest.raises(LiveKillSwitchTriggeredError, match="kill switch triggered"):
        run_live_supervisor(
            _repo_factory,
            settings=settings,
            explicit_live=True,
            client=client,
            iterations=3,
        )

    refreshed = repo.get_order(order.id)
    assert refreshed is not None
    assert refreshed.status == "cancelled"
    assert client._sdk_client.cancel_all_called is True
    persisted = repo.live_operations_state()
    assert persisted["kill_switch"] is not None
    assert persisted["kill_switch"]["tripped"] is True
    assert persisted["supervisor"] is not None
    assert persisted["supervisor"]["status"] == "killed"
    event_types = [
        event.event_type
        for event in repo.list_runtime_events(category="live", limit=10)
    ]
    assert "live_supervisor_error" in event_types
    assert "live_kill_switch_triggered" in event_types


def test_run_live_supervisor_iteration_skips_new_submit_when_kill_switch_active(
    repo,
    sqlite_settings: Settings,
) -> None:
    settings = _live_settings(sqlite_settings)
    repo.upsert_runtime_state(
        "live_kill_switch",
        {
            "tripped": True,
            "reason": "manual hold",
            "tripped_at": datetime.now(UTC).isoformat(),
        },
    )
    repo.session.commit()
    client = ClobV2TradingClient(
        settings,
        sdk_client=_FakeSdkClient(open_orders=[]),
        sdk_bindings=_sdk_bindings(),
    )

    summary = run_live_supervisor_iteration(
        repo,
        settings=settings,
        client=client,
        submit_new_orders=True,
    )

    assert summary["kill_switch_active"] is True
    assert summary["submitted"] is False
    assert summary["skipped_submission_reason"] == "persistent_kill_switch_active"
    persisted = repo.live_operations_state()
    assert persisted["supervisor"] is not None
    assert persisted["supervisor"]["status"] == "ok"
    latest_event = repo.list_runtime_events(category="live", limit=1)[0]
    assert latest_event.event_type == "live_supervisor_iteration"


def test_reset_live_kill_switch_clears_persisted_state(
    repo,
    sqlite_settings: Settings,
    monkeypatch,
) -> None:
    settings = _live_settings(sqlite_settings).model_copy(
        update={"alert_webhook_url": "https://example.test/webhook"}
    )
    repo.upsert_runtime_state(
        "live_kill_switch",
        {
            "tripped": True,
            "reason": "manual hold",
            "consecutive_errors": 3,
            "tripped_at": datetime.now(UTC).isoformat(),
        },
    )
    repo.session.commit()
    sent: list[dict[str, Any]] = []

    def _send_alert(
        event_type: str,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
        settings: Settings | None = None,
    ) -> bool:
        sent.append(
            {
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
                "settings": settings,
            }
        )
        return True

    monkeypatch.setattr("pm_alpha_bot.execution.live.send_alert", _send_alert)
    summary = reset_live_kill_switch(
        repo,
        settings=settings,
        acknowledge=True,
        reset_reason="test_operator_confirmed_reset",
    )

    assert summary["tripped"] is False
    assert summary["reset_acknowledged"] is True
    assert summary["reset_reason"] == "test_operator_confirmed_reset"
    assert summary["previous_reason"] == "manual hold"
    persisted = repo.live_operations_state()
    assert persisted["kill_switch"] is not None
    assert persisted["kill_switch"]["tripped"] is False
    assert sent[0]["event_type"] == "live_kill_switch_reset"
    latest_event = repo.list_runtime_events(category="live", limit=1)[0]
    assert latest_event.event_type == "live_kill_switch_reset"


def test_reset_live_kill_switch_requires_operator_acknowledgement(
    repo,
    sqlite_settings: Settings,
) -> None:
    settings = _live_settings(sqlite_settings)
    repo.upsert_runtime_state(
        "live_kill_switch",
        {
            "tripped": True,
            "reason": "manual hold",
            "consecutive_errors": 3,
            "tripped_at": datetime.now(UTC).isoformat(),
        },
    )
    repo.session.commit()

    with pytest.raises(LiveTradingDisabledError, match="operator acknowledgement"):
        reset_live_kill_switch(repo, settings=settings)

    persisted = repo.live_operations_state()
    assert persisted["kill_switch"] is not None
    assert persisted["kill_switch"]["tripped"] is True


def test_reset_live_kill_switch_blocks_when_active_live_orders_remain(
    repo,
    sqlite_settings: Settings,
) -> None:
    settings = _live_settings(sqlite_settings)
    repo.upsert_runtime_state(
        "live_kill_switch",
        {
            "tripped": True,
            "reason": "manual hold",
            "consecutive_errors": 3,
            "tripped_at": datetime.now(UTC).isoformat(),
        },
    )
    repo.create_live_order(
        PreparedLiveOrder(
            signal_id=991,
            condition_id="cond-1",
            token_id="token-1",
            side="BUY",
            price=0.40,
            size=5.0,
        ),
        status="live",
        external_order_id="ord-live",
    )
    repo.session.commit()

    with pytest.raises(LiveTradingDisabledError, match="active live orders"):
        reset_live_kill_switch(
            repo,
            settings=settings,
            acknowledge=True,
            reset_reason="test_operator_confirmed_reset",
        )

    summary = reset_live_kill_switch(
        repo,
        settings=settings,
        acknowledge=True,
        reset_reason="test_operator_confirmed_reset",
        allow_active_orders=True,
    )
    assert summary["tripped"] is False
    assert summary["active_live_orders_at_reset"] == 1
    assert summary["allow_active_orders"] is True

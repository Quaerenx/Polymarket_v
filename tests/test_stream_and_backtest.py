from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pm_alpha_bot.backtest.replay import (
    ReplayOrder,
    ReplayPosition,
    ReplayState,
    _build_risk_state,
    _favorable_fill_price,
    replay_backtest,
)
from pm_alpha_bot.clients.clob_v2 import LiveTradingDisabledError
from pm_alpha_bot.clients.websocket_market import MarketWebSocketClient
from pm_alpha_bot.db.models import Market, MarketSnapshot, MarketToken, Signal, WalletActivity
from pm_alpha_bot.domain import WalletScoreRecord
from pm_alpha_bot.execution.live import execute_live_once
from pm_alpha_bot.ingest.stream_market import stream_market


class _FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self._messages = messages
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def recv(self) -> str:
        return await self.__anext__()


class _FakeConnection:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> _FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.mark.asyncio()
async def test_stream_market_persists_snapshots(repo, monkeypatch) -> None:
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Question",
            category="POLITICS",
            active=True,
            closed=False,
        )
    )
    repo.session.add(
        MarketToken(condition_id="cond-1", token_id="token-1", outcome="Yes", side_label="Yes")
    )
    repo.session.commit()
    messages = [
        '{"event_type":"book","asset_id":"token-1","market":"cond-1","bids":[{"price":"0.40","size":"10"}],"asks":[{"price":"0.42","size":"12"}],"timestamp":"1710000000000"}',
        '{"event_type":"best_bid_ask","asset_id":"token-1","market":"cond-1","best_bid":"0.41","best_ask":"0.43","spread":"0.02","timestamp":"1710000001000"}',
    ]
    fake_socket = _FakeWebSocket(messages)
    monkeypatch.setattr(
        "pm_alpha_bot.clients.websocket_market.websockets.connect",
        lambda *args, **kwargs: _FakeConnection(fake_socket),
    )
    summary = await stream_market(
        repo,
        token_ids=["token-1"],
        max_messages=2,
        timeout_sec=1,
    )
    latest = repo.latest_snapshot_for_token("token-1")
    assert summary["messages"] == 2
    assert summary["snapshots"] >= 2
    assert latest is not None
    assert latest.best_bid == pytest.approx(0.41)
    assert latest.best_ask == pytest.approx(0.43)
    assert fake_socket.sent
    assert MarketWebSocketClient().build_subscription(["token-1"]).startswith("{")


def test_replay_backtest_runs_without_lookahead(repo, sqlite_settings) -> None:
    now = datetime.now(UTC)
    t_prev = now - timedelta(minutes=50)
    t0 = now - timedelta(seconds=30)
    t1 = now + timedelta(minutes=1)
    t2 = now + timedelta(minutes=2)
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Will it rain?",
            category="POLITICS",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
            end_date=now + timedelta(days=3),
        )
    )
    repo.session.add(
        MarketToken(condition_id="cond-1", token_id="token-yes", outcome="Yes", side_label="Yes")
    )
    repo.session.add(
        MarketSnapshot(
            ts=t_prev,
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=0.34,
            best_ask=0.36,
            midpoint=0.35,
            spread=0.02,
            last_trade_price=0.35,
            liquidity_score=200.0,
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=t0,
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=0.52,
            best_ask=0.56,
            midpoint=0.54,
            spread=0.04,
            last_trade_price=0.54,
            liquidity_score=200.0,
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=t1,
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=0.50,
            best_ask=0.51,
            midpoint=0.505,
            spread=0.01,
            last_trade_price=0.51,
            liquidity_score=200.0,
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=t2,
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=0.61,
            best_ask=0.63,
            midpoint=0.62,
            spread=0.02,
            last_trade_price=0.62,
            liquidity_score=200.0,
        )
    )
    wallet_a = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    wallet_b = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    wallet_c = "0xcccccccccccccccccccccccccccccccccccccccc"
    repo.session.add_all(
        [
            WalletActivity(
                proxy_wallet=wallet_a,
                condition_id="cond-1",
                token_id="token-yes",
                side="buy",
                price=0.40,
                size=10,
                ts=t0 - timedelta(minutes=1),
            ),
            WalletActivity(
                proxy_wallet=wallet_b,
                condition_id="cond-1",
                token_id="token-yes",
                side="buy",
                price=0.40,
                size=10,
                ts=t0 - timedelta(minutes=2),
            ),
            WalletActivity(
                proxy_wallet=wallet_c,
                condition_id="cond-1",
                token_id="token-yes",
                side="buy",
                price=0.40,
                size=10,
                ts=t0 - timedelta(minutes=3),
            ),
        ]
    )
    repo.save_wallet_scores(
        [
            WalletScoreRecord(
                proxy_wallet=wallet_a,
                as_of=t_prev,
                category="POLITICS",
                score=0.82,
                roi=0.10,
                pnl=120.0,
                closed_market_count=30,
                trade_count=50,
                raw_metrics_json={"watchlist_eligible": True},
            ),
            WalletScoreRecord(
                proxy_wallet=wallet_b,
                as_of=t_prev,
                category="POLITICS",
                score=0.79,
                roi=0.09,
                pnl=110.0,
                closed_market_count=30,
                trade_count=50,
                raw_metrics_json={"watchlist_eligible": True},
            ),
            WalletScoreRecord(
                proxy_wallet=wallet_c,
                as_of=t_prev,
                category="POLITICS",
                score=0.76,
                roi=0.08,
                pnl=100.0,
                closed_market_count=30,
                trade_count=50,
                raw_metrics_json={"watchlist_eligible": True},
            ),
        ]
    )
    repo.session.commit()
    report = replay_backtest(
        repo,
        from_dt=t_prev,
        to_dt=t2,
        category="POLITICS",
        settings=sqlite_settings.model_copy(
            update={"replay_fee_bps": 100.0, "replay_slippage_bps": 100.0}
        ),
    )
    assert report.fill_model == "optimistic"
    assert report.total_trades >= 1
    assert report.average_edge_bps >= 500
    assert report.fees_total > 0
    assert report.unrealized_pnl > 0
    assert report.source_wallet_attribution[wallet_a] >= 1


def test_replay_backtest_pessimistic_fill_is_more_conservative(repo, sqlite_settings) -> None:
    now = datetime.now(UTC)
    t_prev = now - timedelta(minutes=50)
    t0 = now - timedelta(seconds=30)
    t1 = now + timedelta(minutes=1)
    t2 = now + timedelta(minutes=2)
    t3 = now + timedelta(minutes=3)
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Will it rain?",
            category="POLITICS",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
            end_date=now + timedelta(days=3),
        )
    )
    repo.session.add(
        MarketToken(condition_id="cond-1", token_id="token-yes", outcome="Yes", side_label="Yes")
    )
    repo.session.add_all(
        [
            MarketSnapshot(
                ts=t_prev,
                condition_id="cond-1",
                token_id="token-yes",
                best_bid=0.34,
                best_ask=0.36,
                midpoint=0.35,
                spread=0.02,
                last_trade_price=0.35,
                liquidity_score=200.0,
            ),
            MarketSnapshot(
                ts=t0,
                condition_id="cond-1",
                token_id="token-yes",
                best_bid=0.52,
                best_ask=0.56,
                midpoint=0.54,
                spread=0.04,
                last_trade_price=0.54,
                liquidity_score=200.0,
            ),
            MarketSnapshot(
                ts=t1,
                condition_id="cond-1",
                token_id="token-yes",
                best_bid=0.50,
                best_ask=0.51,
                midpoint=0.505,
                spread=0.01,
                last_trade_price=0.51,
                liquidity_score=200.0,
            ),
            MarketSnapshot(
                ts=t2,
                condition_id="cond-1",
                token_id="token-yes",
                best_bid=0.49,
                best_ask=0.50,
                midpoint=0.495,
                spread=0.01,
                last_trade_price=0.50,
                liquidity_score=200.0,
            ),
            MarketSnapshot(
                ts=t3,
                condition_id="cond-1",
                token_id="token-yes",
                best_bid=0.60,
                best_ask=0.62,
                midpoint=0.61,
                spread=0.02,
                last_trade_price=0.61,
                liquidity_score=200.0,
            ),
        ]
    )
    wallet_a = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    wallet_b = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    wallet_c = "0xcccccccccccccccccccccccccccccccccccccccc"
    repo.session.add_all(
        [
            WalletActivity(
                proxy_wallet=wallet_a,
                condition_id="cond-1",
                token_id="token-yes",
                side="buy",
                price=0.40,
                size=10,
                ts=t0 - timedelta(minutes=1),
            ),
            WalletActivity(
                proxy_wallet=wallet_b,
                condition_id="cond-1",
                token_id="token-yes",
                side="buy",
                price=0.40,
                size=10,
                ts=t0 - timedelta(minutes=2),
            ),
            WalletActivity(
                proxy_wallet=wallet_c,
                condition_id="cond-1",
                token_id="token-yes",
                side="buy",
                price=0.40,
                size=10,
                ts=t0 - timedelta(minutes=3),
            ),
        ]
    )
    repo.save_wallet_scores(
        [
            WalletScoreRecord(
                proxy_wallet=wallet_a,
                as_of=t_prev,
                category="POLITICS",
                score=0.82,
                roi=0.10,
                pnl=120.0,
                closed_market_count=30,
                trade_count=50,
                raw_metrics_json={"watchlist_eligible": True},
            ),
            WalletScoreRecord(
                proxy_wallet=wallet_b,
                as_of=t_prev,
                category="POLITICS",
                score=0.79,
                roi=0.09,
                pnl=110.0,
                closed_market_count=30,
                trade_count=50,
                raw_metrics_json={"watchlist_eligible": True},
            ),
            WalletScoreRecord(
                proxy_wallet=wallet_c,
                as_of=t_prev,
                category="POLITICS",
                score=0.76,
                roi=0.08,
                pnl=100.0,
                closed_market_count=30,
                trade_count=50,
                raw_metrics_json={"watchlist_eligible": True},
            ),
        ]
    )
    repo.session.commit()

    settings = sqlite_settings.model_copy(
        update={"replay_fee_bps": 100.0, "replay_slippage_bps": 100.0}
    )
    optimistic = replay_backtest(
        repo,
        from_dt=t_prev,
        to_dt=t3,
        category="POLITICS",
        settings=settings,
        fill_model="optimistic",
    )
    pessimistic = replay_backtest(
        repo,
        from_dt=t_prev,
        to_dt=t3,
        category="POLITICS",
        settings=settings,
        fill_model="pessimistic",
    )

    assert optimistic.fill_model == "optimistic"
    assert pessimistic.fill_model == "pessimistic"
    assert pessimistic.total_trades <= optimistic.total_trades
    assert pessimistic.unrealized_pnl <= optimistic.unrealized_pnl


def test_replay_pessimistic_fill_requires_dwell_and_queue_miss(sqlite_settings) -> None:
    now = datetime.now(UTC)
    settings = sqlite_settings.model_copy(
        update={"replay_fill_min_dwell_sec": 30.0, "replay_queue_miss_bps": 25.0}
    )
    order = ReplayOrder(
        order_id=1,
        created_at=now,
        condition_id="cond-1",
        token_id="token-yes",
        category="POLITICS",
        side="BUY",
        price=0.50,
        size=10.0,
        edge_bps=1000.0,
    )

    early_snapshot = MarketSnapshot(
        ts=now + timedelta(seconds=20),
        condition_id="cond-1",
        token_id="token-yes",
        best_bid=0.48,
        best_ask=0.49,
        midpoint=0.485,
        spread=0.01,
    )
    queue_touch_snapshot = MarketSnapshot(
        ts=now + timedelta(seconds=31),
        condition_id="cond-1",
        token_id="token-yes",
        best_bid=0.48,
        best_ask=0.499,
        midpoint=0.4895,
        spread=0.019,
    )
    queue_through_snapshot = MarketSnapshot(
        ts=now + timedelta(seconds=32),
        condition_id="cond-1",
        token_id="token-yes",
        best_bid=0.48,
        best_ask=0.497,
        midpoint=0.4885,
        spread=0.017,
    )

    assert (
        _favorable_fill_price(
            order,
            early_snapshot,
            settings=settings,
            fill_model="pessimistic",
            ts=early_snapshot.ts,
        )
        is None
    )
    assert (
        _favorable_fill_price(
            order,
            queue_touch_snapshot,
            settings=settings,
            fill_model="pessimistic",
            ts=queue_touch_snapshot.ts,
        )
        is None
    )
    assert order.queue_misses == 1
    assert _favorable_fill_price(
        order,
        queue_through_snapshot,
        settings=settings,
        fill_model="pessimistic",
        ts=queue_through_snapshot.ts,
    ) == pytest.approx(0.497)


def test_replay_risk_state_daily_loss_includes_unrealized() -> None:
    now = datetime.now(UTC)
    state = ReplayState(
        initial_capital=1000.0,
        positions={
            "token-1": ReplayPosition(
                condition_id="cond-1",
                token_id="token-1",
                category="POLITICS",
                size=10.0,
                avg_price=0.70,
                unrealized_pnl=-3.0,
            )
        },
    )
    current_snapshots = {
        "token-1": MarketSnapshot(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            best_bid=0.39,
            best_ask=0.41,
            midpoint=0.40,
            spread=0.02,
        )
    }

    risk_state = _build_risk_state(state, current_snapshots, 1000.0, now)

    assert risk_state.daily_loss_pct == pytest.approx(0.003)


def test_execute_live_once_requires_explicit_flag(repo, sqlite_settings, monkeypatch) -> None:
    now = datetime.now(UTC)
    settings = sqlite_settings.model_copy(
        update={
            "enable_live_trading": True,
            "poly_private_key": "pk",
            "poly_api_key": "api",
            "poly_api_secret": "secret",
            "poly_api_passphrase": "pass",
        }
    )
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Question",
            category="POLITICS",
            active=True,
            closed=False,
            min_order_size=1.0,
            end_date=now + timedelta(days=1),
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            best_bid=0.4,
            best_ask=0.42,
            midpoint=0.41,
            spread=0.02,
        )
    )
    repo.session.add(
        Signal(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            direction="BUY",
            market_midpoint=0.41,
            effective_entry_price=0.41,
            fair_prob=0.70,
            edge_bps=2900,
            confidence=0.9,
            source_wallet_count=2,
            reason_json={"signal_id": 1},
            status="new",
        )
    )
    repo.session.commit()

    async def _not_blocked(_settings: Any) -> bool:
        return False

    monkeypatch.setattr("pm_alpha_bot.execution.live._check_geoblock", _not_blocked)
    with pytest.raises(LiveTradingDisabledError, match="--live"):
        execute_live_once(repo, settings=settings, explicit_live=False)

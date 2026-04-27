from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pm_alpha_bot.clients.clob_v2 import LiveTradingDisabledError
from pm_alpha_bot.config import Settings
from pm_alpha_bot.db.models import Market, MarketSnapshot, Signal
from pm_alpha_bot.domain import PreparedLiveOrder, RiskState, SignalRecord
from pm_alpha_bot.execution.live import execute_live_once, prepare_live_order
from pm_alpha_bot.risk.limits import evaluate_signal_risk
from pm_alpha_bot.risk.sizing import kelly_fraction_binary, position_size_pct


def test_kelly_and_position_size() -> None:
    kelly = kelly_fraction_binary(0.7, 0.5)
    assert kelly == pytest.approx(0.4)
    assert position_size_pct(0.7, 0.5, 0.25, 0.03) == pytest.approx(0.03)
    assert position_size_pct(0.56, 0.5, 0.25, 0.10, confidence=0.5) == pytest.approx(0.015)


def test_risk_limit_rejects_stale_orderbook(repo, sqlite_settings: Settings) -> None:
    now = datetime.now(UTC)
    market = Market(
        condition_id="cond-1",
        question="Question",
        category="POLITICS",
        active=True,
        closed=False,
        min_order_size=1.0,
        end_date=now + timedelta(days=1),
    )
    snapshot = MarketSnapshot(
        ts=now - timedelta(minutes=5),
        condition_id="cond-1",
        token_id="token-1",
        best_bid=0.4,
        best_ask=0.5,
        midpoint=0.45,
        spread=0.1,
    )
    signal = SignalRecord(
        ts=now,
        condition_id="cond-1",
        token_id="token-1",
        direction="BUY",
        market_midpoint=0.45,
        effective_entry_price=0.44,
        fair_prob=0.7,
        edge_bps=2600,
        confidence=0.9,
        source_wallet_count=2,
    )
    decision = evaluate_signal_risk(
        signal=signal,
        market=market,
        snapshot=snapshot,
        risk_state=RiskState(
            capital=1000,
            cash=1000,
            equity=1000,
            daily_loss_pct=0.0,
            total_drawdown_pct=0.0,
            market_exposure_pct={},
            category_exposure_pct={},
        ),
        settings=sqlite_settings,
        as_of=now,
    )
    assert decision.passed is False
    assert decision.reason == "stale_orderbook"


def test_risk_sizing_uses_signal_confidence(repo, sqlite_settings: Settings) -> None:
    now = datetime.now(UTC)
    market = Market(
        condition_id="cond-1",
        question="Question",
        category="POLITICS",
        active=True,
        closed=False,
        min_order_size=1.0,
        end_date=now + timedelta(days=1),
    )
    snapshot = MarketSnapshot(
        ts=now,
        condition_id="cond-1",
        token_id="token-1",
        best_bid=0.49,
        best_ask=0.51,
        midpoint=0.50,
        spread=0.02,
    )
    decision = evaluate_signal_risk(
        signal=SignalRecord(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            direction="BUY",
            market_midpoint=0.50,
            effective_entry_price=0.50,
            fair_prob=0.56,
            edge_bps=600,
            confidence=0.5,
            source_wallet_count=2,
        ),
        market=market,
        snapshot=snapshot,
        risk_state=RiskState(
            capital=1000,
            cash=1000,
            equity=1000,
            daily_loss_pct=0.0,
            total_drawdown_pct=0.0,
            market_exposure_pct={},
            category_exposure_pct={},
        ),
        settings=sqlite_settings.model_copy(
            update={"max_market_exposure_pct": 0.10, "kelly_fraction": 0.25}
        ),
        as_of=now,
    )
    assert decision.passed is True
    assert decision.size_pct == pytest.approx(0.015)


def test_risk_limit_rejects_condition_exposure(repo, sqlite_settings: Settings) -> None:
    now = datetime.now(UTC)
    market = Market(
        condition_id="cond-1",
        question="Question",
        category="POLITICS",
        active=True,
        closed=False,
        min_order_size=1.0,
        end_date=now + timedelta(days=1),
    )
    snapshot = MarketSnapshot(
        ts=now,
        condition_id="cond-1",
        token_id="token-2",
        best_bid=0.49,
        best_ask=0.51,
        midpoint=0.50,
        spread=0.02,
    )
    decision = evaluate_signal_risk(
        signal=SignalRecord(
            ts=now,
            condition_id="cond-1",
            token_id="token-2",
            direction="BUY",
            market_midpoint=0.50,
            effective_entry_price=0.50,
            fair_prob=0.70,
            edge_bps=2000,
            confidence=1.0,
            source_wallet_count=3,
        ),
        market=market,
        snapshot=snapshot,
        risk_state=RiskState(
            capital=1000,
            cash=1000,
            equity=1000,
            daily_loss_pct=0.0,
            total_drawdown_pct=0.0,
            market_exposure_pct={},
            condition_exposure_pct={"cond-1": 0.025},
            event_exposure_pct={},
            category_exposure_pct={},
        ),
        settings=sqlite_settings.model_copy(
            update={
                "max_market_exposure_pct": 0.20,
                "max_condition_exposure_pct": 0.05,
                "max_event_exposure_pct": 0.20,
                "max_category_exposure_pct": 0.50,
            }
        ),
        as_of=now,
    )

    assert decision.passed is False
    assert decision.reason == "condition_exposure_limit"


def test_risk_limit_rejects_event_exposure(repo, sqlite_settings: Settings) -> None:
    now = datetime.now(UTC)
    market = Market(
        condition_id="cond-2",
        event_id="event-1",
        question="Question",
        category="POLITICS",
        active=True,
        closed=False,
        min_order_size=1.0,
        end_date=now + timedelta(days=1),
    )
    snapshot = MarketSnapshot(
        ts=now,
        condition_id="cond-2",
        token_id="token-2",
        best_bid=0.49,
        best_ask=0.51,
        midpoint=0.50,
        spread=0.02,
    )
    decision = evaluate_signal_risk(
        signal=SignalRecord(
            ts=now,
            condition_id="cond-2",
            token_id="token-2",
            direction="BUY",
            market_midpoint=0.50,
            effective_entry_price=0.50,
            fair_prob=0.70,
            edge_bps=2000,
            confidence=1.0,
            source_wallet_count=3,
        ),
        market=market,
        snapshot=snapshot,
        risk_state=RiskState(
            capital=1000,
            cash=1000,
            equity=1000,
            daily_loss_pct=0.0,
            total_drawdown_pct=0.0,
            market_exposure_pct={},
            condition_exposure_pct={},
            event_exposure_pct={"event-1": 0.04},
            category_exposure_pct={},
        ),
        settings=sqlite_settings.model_copy(
            update={
                "max_market_exposure_pct": 0.20,
                "max_condition_exposure_pct": 0.20,
                "max_event_exposure_pct": 0.05,
                "max_category_exposure_pct": 0.50,
            }
        ),
        as_of=now,
    )

    assert decision.passed is False
    assert decision.reason == "event_exposure_limit"


def test_live_trading_disabled_by_default(repo, sqlite_settings: Settings) -> None:
    with pytest.raises(LiveTradingDisabledError):
        execute_live_once(repo, settings=sqlite_settings)


def test_prepare_live_order_requires_fresh_snapshot(repo, sqlite_settings: Settings) -> None:
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
            event_id="event-1",
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
            ts=now - timedelta(minutes=5),
            condition_id="cond-1",
            token_id="token-1",
            best_bid=0.4,
            best_ask=0.5,
            midpoint=0.45,
            spread=0.1,
        )
    )
    repo.session.add(
        Signal(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            direction="BUY",
            market_midpoint=0.45,
            effective_entry_price=0.44,
            fair_prob=0.7,
            edge_bps=2600,
            confidence=0.9,
            source_wallet_count=2,
            status="new",
        )
    )
    repo.session.commit()
    with pytest.raises(LiveTradingDisabledError, match="stale"):
        prepare_live_order(repo, settings=settings, as_of=now)
    rejected_signals = repo.list_recent_signals(status="live_rejected", limit=5)
    assert len(rejected_signals) == 1
    assert rejected_signals[0].reason_json is not None
    assert "stale" in rejected_signals[0].reason_json["live_status_reason"]


def test_live_trading_blocked_by_geoblock(repo, sqlite_settings: Settings, monkeypatch) -> None:
    settings = sqlite_settings.model_copy(
        update={
            "enable_live_trading": True,
            "poly_private_key": "pk",
            "poly_api_key": "api",
            "poly_api_secret": "secret",
            "poly_api_passphrase": "pass",
        }
    )

    async def _blocked(_settings: Settings) -> bool:
        return True

    monkeypatch.setattr("pm_alpha_bot.execution.live._check_geoblock", _blocked)
    with pytest.raises(LiveTradingDisabledError, match="Geoblock"):
        execute_live_once(repo, settings=settings, explicit_live=True)


def test_live_report_and_risk_state_use_live_fills(repo, sqlite_settings: Settings) -> None:
    now = datetime.now(UTC)
    repo.session.add(
        Market(
            condition_id="cond-1",
            event_id="event-1",
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
            best_bid=0.54,
            best_ask=0.56,
            midpoint=0.55,
            spread=0.02,
        )
    )
    buy_order = repo.create_live_order(
        PreparedLiveOrder(
            signal_id=1,
            condition_id="cond-1",
            token_id="token-1",
            side="BUY",
            price=0.40,
            size=10.0,
        ),
        status="filled",
        external_order_id="ord-buy",
    )
    sell_order = repo.create_live_order(
        PreparedLiveOrder(
            signal_id=2,
            condition_id="cond-1",
            token_id="token-1",
            side="SELL",
            price=0.60,
            size=4.0,
        ),
        status="filled",
        external_order_id="ord-sell",
    )
    repo.add_fill(
        buy_order,
        ts=now - timedelta(minutes=2),
        price=0.40,
        size=10.0,
        fee=0.10,
        raw_json={"trade_id": "trade-buy"},
    )
    repo.add_fill(
        sell_order,
        ts=now - timedelta(minutes=1),
        price=0.60,
        size=4.0,
        fee=0.05,
        raw_json={"trade_id": "trade-sell"},
    )
    repo.session.commit()

    report = repo.live_report(sqlite_settings.live_initial_capital)
    risk_state = repo.live_risk_state(sqlite_settings.live_initial_capital)

    assert report["fills_count"] == 2
    assert report["realized_pnl"] == pytest.approx(0.8)
    assert report["fees_total"] == pytest.approx(0.15)
    assert report["net_realized_pnl"] == pytest.approx(0.65)
    assert report["unrealized_pnl"] == pytest.approx(0.9)
    assert len(report["positions"]) == 1
    assert report["positions"][0]["side"] == "LONG"
    assert report["positions"][0]["net_size"] == pytest.approx(6.0)
    assert report["positions"][0]["avg_price"] == pytest.approx(0.4)
    assert risk_state.cash == pytest.approx(1000.65)
    assert risk_state.equity == pytest.approx(1001.55)
    assert risk_state.daily_loss_pct == pytest.approx(0.0)
    assert risk_state.market_exposure_pct["token-1"] == pytest.approx(0.0033)
    assert risk_state.condition_exposure_pct["cond-1"] == pytest.approx(0.0033)
    assert risk_state.event_exposure_pct["event-1"] == pytest.approx(0.0033)


def test_repository_rejects_duplicate_live_signal_and_active_token(repo) -> None:
    first = repo.create_live_order(
        PreparedLiveOrder(
            signal_id=1,
            condition_id="cond-1",
            token_id="token-1",
            side="BUY",
            price=0.40,
            size=5.0,
        ),
        status="created",
    )
    with pytest.raises(ValueError, match="signal_id=1"):
        repo.create_live_order(
            PreparedLiveOrder(
                signal_id=1,
                condition_id="cond-2",
                token_id="token-2",
                side="BUY",
                price=0.35,
                size=5.0,
            ),
            status="created",
        )
    with pytest.raises(ValueError, match="token_id=token-1"):
        repo.create_live_order(
            PreparedLiveOrder(
                signal_id=2,
                condition_id="cond-1",
                token_id="token-1",
                side="BUY",
                price=0.41,
                size=5.0,
            ),
            status="created",
        )
    repo.update_order_status(first, status="filled")
    second = repo.create_live_order(
        PreparedLiveOrder(
            signal_id=2,
            condition_id="cond-1",
            token_id="token-1",
            side="BUY",
            price=0.41,
            size=5.0,
        ),
        status="created",
    )
    assert second.id is not None


def test_repository_runtime_state_insert_once_is_atomic(repo) -> None:
    assert repo.insert_runtime_state_once("live_submission:test-key", {"state": "reserved"})
    assert not repo.insert_runtime_state_once("live_submission:test-key", {"state": "duplicate"})
    state = repo.get_runtime_state("live_submission:test-key")
    assert state is not None
    assert state.state_json == {"state": "reserved"}


def test_live_risk_state_daily_loss_includes_unrealized(repo, sqlite_settings: Settings) -> None:
    now = datetime.now(UTC)
    repo.session.add(
        Market(
            condition_id="cond-1",
            event_id="event-1",
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
            best_bid=0.39,
            best_ask=0.41,
            midpoint=0.40,
            spread=0.02,
        )
    )
    buy_order = repo.create_live_order(
        PreparedLiveOrder(
            signal_id=1,
            condition_id="cond-1",
            token_id="token-1",
            side="BUY",
            price=0.70,
            size=10.0,
        ),
        status="filled",
        external_order_id="ord-buy",
    )
    repo.add_fill(
        buy_order,
        ts=now - timedelta(minutes=1),
        price=0.70,
        size=10.0,
        fee=0.0,
        raw_json={"trade_id": "trade-buy"},
    )
    repo.session.commit()

    risk_state = repo.live_risk_state(sqlite_settings.live_initial_capital)

    assert risk_state.equity == pytest.approx(997.0)
    assert risk_state.daily_loss_pct == pytest.approx(0.003)
    assert risk_state.condition_exposure_pct["cond-1"] == pytest.approx(0.004)
    assert risk_state.event_exposure_pct["event-1"] == pytest.approx(0.004)


def test_paper_risk_state_daily_loss_includes_unrealized(repo, sqlite_settings: Settings) -> None:
    now = datetime.now(UTC)
    repo.session.add(
        Market(
            condition_id="cond-1",
            event_id="event-1",
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
            best_bid=0.39,
            best_ask=0.41,
            midpoint=0.40,
            spread=0.02,
        )
    )
    repo.upsert_paper_position(
        condition_id="cond-1",
        token_id="token-1",
        side="LONG",
        size=10.0,
        avg_price=0.70,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
    )
    repo.session.commit()

    risk_state = repo.paper_risk_state(sqlite_settings.paper_initial_capital)

    assert risk_state.equity == pytest.approx(997.0)
    assert risk_state.daily_loss_pct == pytest.approx(0.003)
    assert risk_state.condition_exposure_pct["cond-1"] == pytest.approx(0.004)
    assert risk_state.event_exposure_pct["event-1"] == pytest.approx(0.004)


def test_prepare_live_order_uses_live_risk_state(repo, sqlite_settings: Settings) -> None:
    now = datetime.now(UTC)
    settings = sqlite_settings.model_copy(
        update={
            "enable_live_trading": True,
            "live_initial_capital": 1000.0,
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
            best_bid=0.49,
            best_ask=0.51,
            midpoint=0.50,
            spread=0.02,
        )
    )
    repo.session.add(
        Signal(
            ts=now,
            condition_id="cond-1",
            token_id="token-1",
            direction="BUY",
            market_midpoint=0.50,
            effective_entry_price=0.48,
            fair_prob=0.7,
            edge_bps=2200,
            confidence=0.9,
            source_wallet_count=2,
            status="new",
        )
    )
    existing_order = repo.create_live_order(
        PreparedLiveOrder(
            signal_id=99,
            condition_id="cond-1",
            token_id="token-1",
            side="BUY",
            price=0.40,
            size=100.0,
        ),
        status="filled",
        external_order_id="ord-existing",
    )
    repo.add_fill(
        existing_order,
        ts=now - timedelta(minutes=1),
        price=0.40,
        size=100.0,
        raw_json={"trade_id": "trade-existing"},
    )
    repo.session.commit()

    with pytest.raises(LiveTradingDisabledError, match="market_exposure_limit"):
        prepare_live_order(repo, settings=settings, as_of=now)

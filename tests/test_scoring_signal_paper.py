from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pm_alpha_bot.db.models import Market, MarketSnapshot, MarketToken, Wallet, WalletActivity
from pm_alpha_bot.domain import PaperOrderRequest, SignalRecord
from pm_alpha_bot.execution.paper import PaperBroker
from pm_alpha_bot.scoring.wallet_score import calculate_wallet_scores
from pm_alpha_bot.strategy.filters import market_is_tradeable
from pm_alpha_bot.strategy.signal import _recency_weight, generate_signals


def _seed_market(repo) -> None:
    now = datetime.now(UTC)
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
    repo.session.flush()


def _seed_wallet(
    repo,
    wallet: str,
    pnl: float,
    concentration: float,
    score_inputs: int,
    *,
    now: datetime | None = None,
    activity_age_hours: float | None = None,
) -> None:
    now = now or datetime.now(UTC)
    closed_positions = [{"cashPnl": pnl / 30} for _ in range(30)]
    if concentration > 0:
        closed_positions[0]["cashPnl"] = pnl * concentration
        remaining = (pnl - closed_positions[0]["cashPnl"]) / 29
        for item in closed_positions[1:]:
            item["cashPnl"] = remaining
    trades = [{"id": index} for index in range(50)]
    repo.session.add(
        Wallet(
            proxy_wallet=wallet,
            username=wallet[-4:],
            first_seen=now,
            last_seen=now,
            raw_json={
                "leaderboard": {"pnl": pnl, "vol": 1000},
                "closed_positions": closed_positions,
                "trades": trades,
                "total_value": {"totalValue": 1000},
            },
        )
    )
    for index in range(score_inputs):
        activity_ts = (
            now - timedelta(hours=activity_age_hours, seconds=index)
            if activity_age_hours is not None
            else now - timedelta(minutes=index + 1)
        )
        repo.session.add(
            WalletActivity(
                proxy_wallet=wallet,
                condition_id="cond-1",
                token_id="token-yes",
                side="buy",
                price=0.4,
                size=10,
                ts=activity_ts,
            )
        )


def test_recency_weight_cuts_off_old_activity() -> None:
    assert _recency_weight(0.10) == pytest.approx(1.0)
    assert _recency_weight(0.75) == pytest.approx(0.8)
    assert _recency_weight(2.0) == pytest.approx(0.5)
    assert _recency_weight(6.0) == pytest.approx(0.2)
    assert _recency_weight(8.01) == pytest.approx(0.0)


def test_market_tradeability_uses_clob_accepting_order_flags() -> None:
    now = datetime.now(UTC)
    market = Market(
        condition_id="cond-sports",
        question="Sports market",
        active=True,
        closed=False,
        end_date=now - timedelta(hours=1),
        raw_json={
            "active": True,
            "closed": False,
            "archived": False,
            "accepting_orders": True,
            "enable_order_book": True,
        },
    )

    assert market_is_tradeable(market, now) is True

    market.raw_json = {**dict(market.raw_json or {}), "accepting_orders": False}
    assert market_is_tradeable(market, now) is False


def test_wallet_scoring_signal_generation_and_paper_fill(repo) -> None:
    _seed_market(repo)
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
    )
    _seed_wallet(
        repo,
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        pnl=110.0,
        concentration=0.2,
        score_inputs=55,
    )
    _seed_wallet(
        repo,
        "0xdddddddddddddddddddddddddddddddddddddddd",
        pnl=115.0,
        concentration=0.2,
        score_inputs=55,
    )
    _seed_wallet(
        repo,
        "0xcccccccccccccccccccccccccccccccccccccccc",
        pnl=-20.0,
        concentration=0.9,
        score_inputs=10,
    )
    t_prev = datetime.now(UTC) - timedelta(minutes=50, seconds=30)
    t0 = datetime.now(UTC) - timedelta(seconds=30)
    t1 = datetime.now(UTC) + timedelta(minutes=1)
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
    repo.session.commit()

    score_rows = calculate_wallet_scores(
        repo, category="POLITICS", as_of=datetime.now(UTC)
    )
    repo.save_wallet_scores(score_rows)
    top_scores = repo.latest_wallet_scores(category="POLITICS")
    assert top_scores[0].score is not None
    assert (top_scores[0].raw_metrics_json or {}).get("watchlist_eligible") is True

    signals = generate_signals(repo, category="POLITICS", as_of=datetime.now(UTC))
    assert len(signals) == 1
    assert signals[0].edge_bps is not None and signals[0].edge_bps > 0
    activity_age = signals[0].reason_json.get("source_wallet_activity_age_hours")
    assert activity_age is not None
    assert activity_age["max"] <= 8
    assert activity_age["cutoff"] == 8
    repo.save_signals(signals)
    repo.session.commit()

    broker = PaperBroker(repo)
    first_run = broker.run_once()
    repo.session.commit()
    assert first_run["created_orders"] >= 1
    processed = broker.process_open_orders(as_of=t1 + timedelta(seconds=1))
    repo.session.commit()
    assert processed >= 1
    positions = repo.list_paper_positions()
    assert positions
    assert positions[0].size > 0


def test_signal_generation_requires_three_wallet_consensus(repo, sqlite_settings) -> None:
    now = datetime.now(UTC)
    _seed_market(repo)
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=now,
    )
    _seed_wallet(
        repo,
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=now,
    )
    repo.session.add(
        MarketSnapshot(
            ts=now - timedelta(seconds=10),
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
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=now)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    signals = generate_signals(
        repo,
        category="POLITICS",
        settings=sqlite_settings,
        as_of=now,
    )

    assert sqlite_settings.min_wallet_consensus == 3
    assert signals == []


def test_signal_generation_ignores_stale_snapshot(repo, sqlite_settings) -> None:
    now = datetime.now(UTC)
    _seed_market(repo)
    for wallet in (
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "0xdddddddddddddddddddddddddddddddddddddddd",
    ):
        _seed_wallet(
            repo,
            wallet,
            pnl=120.0,
            concentration=0.2,
            score_inputs=55,
            now=now,
        )
    repo.session.add(
        MarketSnapshot(
            ts=now - timedelta(minutes=10),
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
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=now)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    signals = generate_signals(
        repo,
        category="POLITICS",
        settings=sqlite_settings,
        as_of=now,
    )

    assert signals == []


def test_signal_generation_ignores_non_trade_activity_side(repo, sqlite_settings) -> None:
    now = datetime.now(UTC)
    _seed_market(repo)
    for wallet in (
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "0xdddddddddddddddddddddddddddddddddddddddd",
    ):
        _seed_wallet(
            repo,
            wallet,
            pnl=120.0,
            concentration=0.2,
            score_inputs=0,
            now=now,
        )
        repo.session.add(
            WalletActivity(
                proxy_wallet=wallet,
                condition_id="cond-1",
                token_id="token-yes",
                side="MERGE",
                price=0.0,
                size=10,
                ts=now - timedelta(minutes=1),
            )
        )
    repo.session.add(
        MarketSnapshot(
            ts=now - timedelta(seconds=10),
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
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=now)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    signals = generate_signals(
        repo,
        category="POLITICS",
        settings=sqlite_settings,
        as_of=now,
    )

    assert signals == []


def test_signal_generation_ignores_activity_beyond_recency_cutoff(
    repo, sqlite_settings
) -> None:
    now = datetime.now(UTC)
    _seed_market(repo)
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=now,
        activity_age_hours=9.0,
    )
    _seed_wallet(
        repo,
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=now,
        activity_age_hours=9.0,
    )
    _seed_wallet(
        repo,
        "0xdddddddddddddddddddddddddddddddddddddddd",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=now,
        activity_age_hours=9.0,
    )
    repo.session.add(
        MarketSnapshot(
            ts=now - timedelta(seconds=10),
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
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=now)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    default_signals = generate_signals(
        repo,
        category="POLITICS",
        settings=sqlite_settings,
        as_of=now,
    )
    extended_lookback_signals = generate_signals(
        repo,
        category="POLITICS",
        settings=sqlite_settings.model_copy(update={"signal_activity_lookback_hours": 12}),
        as_of=now,
    )

    assert default_signals == []
    assert extended_lookback_signals == []


def test_paper_broker_applies_fee_and_slippage(repo, sqlite_settings) -> None:
    _seed_market(repo)
    now = datetime.now(UTC)
    t1 = now + timedelta(minutes=1)
    repo.session.add(
        MarketSnapshot(
            ts=now,
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=0.50,
            best_ask=0.53,
            midpoint=0.515,
            spread=0.03,
            last_trade_price=0.515,
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
    repo.session.commit()

    broker = PaperBroker(
        repo,
        settings=sqlite_settings.model_copy(
            update={"paper_fee_bps": 100.0, "paper_slippage_bps": 100.0}
        ),
    )
    created = broker.create_orders_from_signals(
        [
            SignalRecord(
                ts=now,
                condition_id="cond-1",
                token_id="token-yes",
                direction="BUY",
                market_midpoint=0.515,
                effective_entry_price=0.52,
                fair_prob=0.65,
                edge_bps=1600,
                confidence=0.9,
                source_wallet_count=2,
                reason_json={"signal_id": 321},
            )
        ],
        as_of=now,
    )
    repo.session.commit()
    processed = broker.process_open_orders(as_of=t1 + timedelta(seconds=1))
    repo.session.commit()

    assert created == 1
    assert processed == 1
    fills = repo.order_fill_rows(mode="paper")
    assert len(fills) == 1
    _, fill = fills[0]
    assert fill.price == pytest.approx(0.5151, rel=1e-4)
    assert fill.fee == pytest.approx(fill.price * fill.size * 0.01)
    position = repo.get_paper_position("token-yes")
    assert position is not None
    assert position.realized_pnl == pytest.approx(-(fill.fee or 0.0))


def test_paper_broker_requires_dwell_and_queue_miss_for_fill(repo, sqlite_settings) -> None:
    _seed_market(repo)
    now = datetime.now(UTC)
    order = repo.create_order(
        PaperOrderRequest(
            condition_id="cond-1",
            token_id="token-yes",
            side="BUY",
            price=0.52,
            size=10.0,
            reason_json={"signal_id": 999},
        ),
        mode="paper",
        status="resting",
    )
    order.created_at = now
    repo.session.add_all(
        [
            MarketSnapshot(
                ts=now + timedelta(seconds=10),
                condition_id="cond-1",
                token_id="token-yes",
                best_bid=0.50,
                best_ask=0.50,
                midpoint=0.50,
                spread=0.00,
                last_trade_price=0.50,
                liquidity_score=200.0,
            ),
            MarketSnapshot(
                ts=now + timedelta(seconds=40),
                condition_id="cond-1",
                token_id="token-yes",
                best_bid=0.51,
                best_ask=0.519,
                midpoint=0.5145,
                spread=0.009,
                last_trade_price=0.519,
                liquidity_score=200.0,
            ),
            MarketSnapshot(
                ts=now + timedelta(seconds=50),
                condition_id="cond-1",
                token_id="token-yes",
                best_bid=0.51,
                best_ask=0.517,
                midpoint=0.5135,
                spread=0.007,
                last_trade_price=0.517,
                liquidity_score=200.0,
            ),
        ]
    )
    repo.session.commit()
    broker = PaperBroker(
        repo,
        settings=sqlite_settings.model_copy(
            update={"paper_fill_min_dwell_sec": 30.0, "paper_queue_miss_bps": 25.0}
        ),
    )

    assert broker.process_open_orders(as_of=now + timedelta(seconds=20)) == 0
    assert repo.order_fill_rows(mode="paper") == []
    assert broker.process_open_orders(as_of=now + timedelta(seconds=45)) == 0
    assert repo.order_fill_rows(mode="paper") == []
    assert broker.process_open_orders(as_of=now + timedelta(seconds=60)) == 1

    fills = repo.order_fill_rows(mode="paper")
    assert len(fills) == 1
    _, fill = fills[0]
    assert fill.price == pytest.approx(0.5172585, rel=1e-5)


def test_paper_broker_rejects_duplicate_token_exposure(repo) -> None:
    _seed_market(repo)
    now = datetime.now(UTC)
    repo.session.add(
        MarketSnapshot(
            ts=now,
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=0.44,
            best_ask=0.46,
            midpoint=0.45,
            spread=0.02,
            last_trade_price=0.45,
            liquidity_score=200.0,
        )
    )
    repo.upsert_paper_position(
        condition_id="cond-1",
        token_id="token-yes",
        side="LONG",
        size=10.0,
        avg_price=0.40,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
    )
    repo.session.commit()

    broker = PaperBroker(repo)
    created = broker.create_orders_from_signals(
        [
            SignalRecord(
                ts=now,
                condition_id="cond-1",
                token_id="token-yes",
                direction="BUY",
                market_midpoint=0.45,
                effective_entry_price=0.44,
                fair_prob=0.60,
                edge_bps=1600,
                confidence=0.9,
                source_wallet_count=2,
                reason_json={"signal_id": 123},
            )
        ],
        as_of=now,
    )
    repo.session.commit()

    assert created == 1
    orders = repo.list_orders(mode="paper")
    assert len(orders) == 1
    assert orders[0].status == "rejected"
    assert (orders[0].reason_json or {}).get("risk", {}).get("reason") == (
        "duplicate_open_order_or_position"
    )

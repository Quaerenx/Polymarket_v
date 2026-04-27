from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pm_alpha_bot.db.models import Market, MarketSnapshot, MarketToken, Wallet, WalletActivity
from pm_alpha_bot.scoring.wallet_score import calculate_wallet_scores
from pm_alpha_bot.strategy.diagnostics import (
    analyze_signal_generation,
    analyze_signal_observations,
)


def _seed_market(
    repo,
    *,
    now: datetime,
    condition_id: str = "cond-1",
    token_id: str = "token-yes",
    category: str = "POLITICS",
) -> None:
    repo.session.add(
        Market(
            condition_id=condition_id,
            question="Will it rain?",
            category=category,
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
            end_date=now + timedelta(days=3),
        )
    )
    repo.session.add(
        MarketToken(condition_id=condition_id, token_id=token_id, outcome="Yes", side_label="Yes")
    )
    repo.session.flush()


def _seed_wallet(
    repo,
    wallet: str,
    pnl: float,
    concentration: float,
    score_inputs: int,
    *,
    now: datetime,
    condition_id: str = "cond-1",
    token_id: str = "token-yes",
) -> None:
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
        repo.session.add(
            WalletActivity(
                proxy_wallet=wallet,
                condition_id=condition_id,
                token_id=token_id,
                side="buy",
                price=0.4,
                size=10,
                ts=now - timedelta(minutes=index + 1),
            )
        )


def test_signal_diagnostics_reports_generated_signal(repo, sqlite_settings) -> None:
    analysis_as_of = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    _seed_market(repo, now=analysis_as_of)
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=analysis_as_of,
    )
    _seed_wallet(
        repo,
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=analysis_as_of,
    )
    _seed_wallet(
        repo,
        "0xdddddddddddddddddddddddddddddddddddddddd",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=analysis_as_of,
    )
    t_prev = analysis_as_of - timedelta(minutes=50)
    t0 = analysis_as_of - timedelta(seconds=10)
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
            best_bid=0.50,
            best_ask=0.54,
            midpoint=0.54,
            spread=0.02,
            last_trade_price=0.54,
            liquidity_score=200.0,
        )
    )
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=analysis_as_of)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    report = analyze_signal_generation(
        repo,
        category="POLITICS",
        settings=sqlite_settings,
        as_of=analysis_as_of,
        top_candidates=5,
    )

    assert report["wallet_scores_latest"] == 3
    assert report["current"]["eligible_wallets"] == 3
    assert report["current"]["generated"] == 1
    assert report["current"]["reason_counts"]["generated"] == 1
    assert report["current"]["top_candidates"][0]["reason"] == "generated"
    assert any(
        row["score_threshold"] == sqlite_settings.signal_min_wallet_score
        and row["consensus"] == sqlite_settings.min_wallet_consensus
        and row["generated"] == 1
        for row in report["sensitivity"]
    )


def test_signal_diagnostics_reports_cached_missing_snapshot_reason(repo, sqlite_settings) -> None:
    analysis_as_of = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    _seed_market(repo, now=analysis_as_of)
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=analysis_as_of,
    )
    repo.upsert_runtime_state(
        "missing_orderbooks",
        {
            "updated_at": analysis_as_of.isoformat(),
            "tokens": {"token-yes": analysis_as_of.isoformat()},
        },
    )
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=analysis_as_of)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    report = analyze_signal_generation(
        repo,
        category="POLITICS",
        settings=sqlite_settings,
        as_of=analysis_as_of,
        top_candidates=5,
    )

    assert report["current"]["generated"] == 0
    assert report["current"]["reason_counts"]["missing_snapshot_cached_404"] == 1
    assert report["current"]["top_candidates"][0]["reason"] == "missing_snapshot_cached_404"


def test_signal_diagnostics_reports_category_without_tradeable_orderbook(
    repo, sqlite_settings
) -> None:
    analysis_as_of = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    _seed_market(repo, now=analysis_as_of, category="Sports")
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=analysis_as_of,
    )
    for index in range(sqlite_settings.signal_liquidity_gate_min_category_samples):
        condition_id = f"cond-empty-{index}"
        repo.session.add(
            Market(
                condition_id=condition_id,
                question=f"Empty Sports {index}",
                category="Sports",
                active=True,
                closed=False,
                min_tick_size=0.01,
                min_order_size=1.0,
                end_date=analysis_as_of + timedelta(days=3),
            )
        )
        repo.session.add(
            MarketSnapshot(
                ts=analysis_as_of - timedelta(seconds=5),
                condition_id=condition_id,
                token_id=f"token-empty-{index}",
                best_bid=None,
                best_ask=None,
                midpoint=None,
                spread=None,
                liquidity_score=0.0,
            )
        )
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=analysis_as_of)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    report = analyze_signal_generation(
        repo,
        category="POLITICS",
        settings=sqlite_settings,
        as_of=analysis_as_of,
        top_candidates=5,
    )

    assert report["current"]["generated"] == 0
    assert report["current"]["reason_counts"]["market_category_no_tradeable_orderbook"] == 1
    assert (
        report["current"]["top_candidates"][0]["reason"]
        == "market_category_no_tradeable_orderbook"
    )


def test_signal_diagnostics_reports_empty_orderbook_with_last_trade_reason(
    repo, sqlite_settings
) -> None:
    analysis_as_of = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    _seed_market(repo, now=analysis_as_of)
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=analysis_as_of,
    )
    repo.session.add(
        MarketSnapshot(
            ts=analysis_as_of - timedelta(seconds=5),
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            last_trade_price=0.42,
            liquidity_score=0.0,
            raw_json={"bids": [], "asks": []},
        )
    )
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=analysis_as_of)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    report = analyze_signal_generation(
        repo,
        category="POLITICS",
        settings=sqlite_settings,
        as_of=analysis_as_of,
        top_candidates=5,
    )

    assert report["current"]["generated"] == 0
    assert report["current"]["observation_only"] == 1
    assert report["current"]["reason_counts"]["empty_orderbook_with_last_trade"] == 1
    assert report["current"]["top_candidates"][0]["reason"] == "empty_orderbook_with_last_trade"
    assert report["current"]["top_candidates"][0]["last_trade_price"] == 0.42
    assert report["current"]["top_candidates"][0]["source_wallet_count"] == 1
    assert (
        report["current"]["observation_candidates"][0]["reason"]
        == "empty_orderbook_with_last_trade"
    )
    assert report["current"]["observation_candidates"][0]["last_trade_price"] == 0.42
    assert report["current"]["observation_candidates"][0]["wallet_direction_score"] > 0
    assert report["current"]["observation_candidates"][0]["observation_score"] > 0
    assert report["current"]["observation_candidates"][0]["promotion_ready"] is False


def test_signal_diagnostics_reports_empty_orderbook_without_last_trade_reason(
    repo, sqlite_settings
) -> None:
    analysis_as_of = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    _seed_market(repo, now=analysis_as_of)
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=analysis_as_of,
    )
    repo.session.add(
        MarketSnapshot(
            ts=analysis_as_of - timedelta(seconds=5),
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            last_trade_price=None,
            liquidity_score=0.0,
            raw_json={"bids": [], "asks": []},
        )
    )
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=analysis_as_of)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    report = analyze_signal_generation(
        repo,
        category="POLITICS",
        settings=sqlite_settings,
        as_of=analysis_as_of,
        top_candidates=5,
    )

    assert report["current"]["generated"] == 0
    assert report["current"]["observation_only"] == 0
    assert report["current"]["reason_counts"]["empty_orderbook"] == 1
    assert report["current"]["top_candidates"][0]["reason"] == "empty_orderbook"
    assert report["current"]["observation_candidates"] == []


def test_signal_observations_focuses_on_observation_candidates(repo, sqlite_settings) -> None:
    analysis_as_of = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    _seed_market(repo, now=analysis_as_of)
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=analysis_as_of,
    )
    repo.session.add(
        MarketSnapshot(
            ts=analysis_as_of - timedelta(seconds=5),
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            last_trade_price=0.42,
            liquidity_score=0.0,
            raw_json={"bids": [], "asks": []},
        )
    )
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=analysis_as_of)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    report = analyze_signal_observations(
        repo,
        category="POLITICS",
        score_threshold=0.30,
        consensus=1,
        settings=sqlite_settings,
        as_of=analysis_as_of,
        top_candidates=5,
    )

    assert report["settings"]["report_mode"] == "observation"
    assert report["current"]["observation_only"] == 1
    assert report["current"]["promotion_ready"] == 1
    assert report["current"]["promotion_queue"] == 0
    assert report["current"]["generated"] == 0
    assert (
        report["current"]["observation_candidates"][0]["reason"]
        == "empty_orderbook_with_last_trade"
    )
    assert report["current"]["observation_candidates"][0]["promotion_ready"] is True
    assert report["current"]["observation_candidates"][0]["observation_score"] > 0
    assert report["current"]["observation_candidates"][0]["observation_streak"] == 1
    assert report["current"]["promotion_queue_candidates"] == []


def test_signal_observations_uses_persisted_watch_state(repo, sqlite_settings) -> None:
    analysis_as_of = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    _seed_market(repo, now=analysis_as_of)
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=55,
        now=analysis_as_of,
    )
    repo.session.add(
        MarketSnapshot(
            ts=analysis_as_of - timedelta(seconds=5),
            condition_id="cond-1",
            token_id="token-yes",
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            last_trade_price=0.42,
            liquidity_score=0.0,
            raw_json={"bids": [], "asks": []},
        )
    )
    repo.upsert_runtime_state(
        "signal_observation_watch:POLITICS",
        {
            "category": "POLITICS",
            "updated_at": analysis_as_of.isoformat(),
            "tokens": {
                "token-yes": {
                    "token_id": "token-yes",
                    "observation_streak": 3,
                    "first_seen_at": (analysis_as_of - timedelta(hours=2)).isoformat(),
                    "last_seen_at": analysis_as_of.isoformat(),
                }
            },
        },
    )
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=analysis_as_of)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    report = analyze_signal_observations(
        repo,
        category="POLITICS",
        score_threshold=0.30,
        consensus=1,
        settings=sqlite_settings,
        as_of=analysis_as_of,
        top_candidates=5,
    )

    assert report["current"]["persistent_ready"] == 1
    assert report["current"]["promotion_queue"] == 1
    assert report["current"]["observation_candidates"][0]["observation_streak"] == 3
    assert report["current"]["observation_candidates"][0]["persistent_observation"] is True
    assert report["current"]["observation_candidates"][0]["queued_for_promotion"] is True
    assert report["current"]["promotion_queue_candidates"][0]["token_id"] == "token-yes"


def test_signal_diagnostics_supports_token_filter(repo, sqlite_settings) -> None:
    analysis_as_of = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    _seed_market(repo, now=analysis_as_of, condition_id="cond-a", token_id="token-a")
    _seed_market(repo, now=analysis_as_of, condition_id="cond-b", token_id="token-b")
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=0,
        now=analysis_as_of,
        condition_id="cond-a",
        token_id="token-a",
    )
    repo.session.add(
        WalletActivity(
            proxy_wallet="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            condition_id="cond-b",
            token_id="token-b",
            side="buy",
            price=0.41,
            size=5.0,
            ts=analysis_as_of - timedelta(minutes=1),
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=analysis_as_of - timedelta(seconds=5),
            condition_id="cond-a",
            token_id="token-a",
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            last_trade_price=0.42,
            liquidity_score=0.0,
            raw_json={"bids": [], "asks": []},
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=analysis_as_of - timedelta(seconds=5),
            condition_id="cond-b",
            token_id="token-b",
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            last_trade_price=0.52,
            liquidity_score=0.0,
            raw_json={"bids": [], "asks": []},
        )
    )
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=analysis_as_of)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    report = analyze_signal_observations(
        repo,
        category="POLITICS",
        token_id="token-b",
        score_threshold=0.30,
        consensus=1,
        settings=sqlite_settings,
        as_of=analysis_as_of,
        top_candidates=5,
    )

    assert report["token_id"] == "token-b"
    assert report["current"]["tokens_seen"] == 1
    assert report["current"]["observation_candidates"][0]["token_id"] == "token-b"


def test_signal_diagnostics_supports_market_category_filter(repo, sqlite_settings) -> None:
    analysis_as_of = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    _seed_market(
        repo,
        now=analysis_as_of,
        condition_id="cond-crypto",
        token_id="token-crypto",
        category="Crypto",
    )
    _seed_market(
        repo,
        now=analysis_as_of,
        condition_id="cond-sports",
        token_id="token-sports",
        category="Sports",
    )
    _seed_wallet(
        repo,
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        pnl=120.0,
        concentration=0.2,
        score_inputs=1,
        now=analysis_as_of,
        condition_id="cond-crypto",
        token_id="token-crypto",
    )
    repo.session.add(
        WalletActivity(
            proxy_wallet="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            condition_id="cond-sports",
            token_id="token-sports",
            side="buy",
            price=0.51,
            size=5.0,
            ts=analysis_as_of - timedelta(minutes=1),
        )
    )
    repo.session.add_all(
        [
            MarketSnapshot(
                ts=analysis_as_of - timedelta(seconds=5),
                condition_id="cond-crypto",
                token_id="token-crypto",
                best_bid=None,
                best_ask=None,
                midpoint=None,
                spread=None,
                last_trade_price=0.42,
                liquidity_score=0.0,
                raw_json={"bids": [], "asks": []},
            ),
            MarketSnapshot(
                ts=analysis_as_of - timedelta(seconds=5),
                condition_id="cond-sports",
                token_id="token-sports",
                best_bid=None,
                best_ask=None,
                midpoint=None,
                spread=None,
                last_trade_price=0.52,
                liquidity_score=0.0,
                raw_json={"bids": [], "asks": []},
            ),
        ]
    )
    repo.session.commit()

    score_rows = calculate_wallet_scores(repo, category="POLITICS", as_of=analysis_as_of)
    repo.save_wallet_scores(score_rows)
    repo.session.commit()

    report = analyze_signal_observations(
        repo,
        category="POLITICS",
        market_category="crypto",
        score_threshold=0.30,
        consensus=1,
        settings=sqlite_settings,
        as_of=analysis_as_of,
        top_candidates=5,
    )

    assert report["market_category_filter"] == "crypto"
    assert report["current"]["tokens_seen"] == 1
    assert report["current"]["observation_candidates"][0]["token_id"] == "token-crypto"

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pm_alpha_bot.db.models import Market, MarketSnapshot, MarketToken, WalletActivity, WalletScore
from pm_alpha_bot.ops import refresh


def test_run_refresh_cycle_uses_explicit_limits(monkeypatch, sqlite_settings) -> None:
    calls: list[tuple[str, object]] = []

    def fake_markets(
        settings,
        *,
        limit: int,
        active_only: bool,
        market_category: str | None,
    ) -> int:
        calls.append(("markets", (settings, limit, active_only, market_category)))
        return 11

    def fake_leaderboard(settings, *, category: str, time_period: str, limit: int) -> int:
        calls.append(("leaderboard", (settings, category, time_period, limit)))
        return 7

    def fake_wallets(settings, *, limit: int) -> dict[str, object]:
        calls.append(("wallets", (settings, limit)))
        return {"processed": 5, "failed": 0}

    def fake_liquidity_wallets(settings, *, market_category: str | None) -> dict[str, int]:
        calls.append(("liquidity_wallets", (settings, market_category)))
        return {"wallets_discovered": 3, "wallets_upserted": 3}

    def fake_wallet_context(
        settings,
        *,
        wallet_limit: int,
        context_limit: int,
        market_category: str | None,
    ) -> dict[str, int]:
        calls.append(("wallet_context", (settings, wallet_limit, context_limit, market_category)))
        return {"markets_enriched": 3, "recent_wallet_orderbooks": 5}

    def fake_orderbook(settings, *, limit: int, market_category: str | None) -> int:
        calls.append(("orderbook", (settings, limit, market_category)))
        return 19

    def fake_scores(settings, *, category: str, leaderboard_time_period: str) -> dict[str, int]:
        calls.append(("scores", (settings, category, leaderboard_time_period)))
        return {"calculated": 4, "stored": 4}

    def fake_signal_context(
        settings,
        *,
        category: str,
        context_limit: int,
        market_category: str | None,
    ) -> dict[str, int]:
        calls.append(("signal_context", (settings, category, context_limit, market_category)))
        return {
            "eligible_wallets": 2,
            "candidate_markets": 6,
            "candidate_tokens": 8,
            "markets_enriched": 3,
            "orderbooks_enriched": 5,
        }

    def fake_signals(settings, *, category: str, market_category: str | None) -> dict[str, int]:
        calls.append(("signals", (settings, category, market_category)))
        return {"generated": 2, "stored": 2}

    def fake_observations(
        settings,
        *,
        category: str,
        market_category: str | None,
    ) -> dict[str, int]:
        calls.append(("observations", (settings, category, market_category)))
        return {
            "observation_only": 3,
            "promotion_ready": 1,
            "newly_ready": 1,
            "persistent_ready": 0,
            "promotion_queue": 0,
            "cleared": 0,
        }

    monkeypatch.setattr(refresh, "_ingest_markets_stage", fake_markets)
    monkeypatch.setattr(refresh, "_ingest_leaderboard_stage", fake_leaderboard)
    monkeypatch.setattr(refresh, "_liquidity_wallet_discovery_stage", fake_liquidity_wallets)
    monkeypatch.setattr(refresh, "_ingest_wallets_stage", fake_wallets)
    monkeypatch.setattr(refresh, "_wallet_context_stage", fake_wallet_context)
    monkeypatch.setattr(refresh, "_ingest_orderbook_stage", fake_orderbook)
    monkeypatch.setattr(refresh, "_score_wallets_stage", fake_scores)
    monkeypatch.setattr(refresh, "_signal_context_stage", fake_signal_context)
    monkeypatch.setattr(refresh, "_scan_signals_stage", fake_signals)
    monkeypatch.setattr(refresh, "_observation_watch_stage", fake_observations)

    summary = refresh.run_refresh_cycle(
        settings=sqlite_settings,
        leaderboard_category="POLITICS",
        leaderboard_time_period="WEEK",
        market_limit=23,
        wallet_limit=17,
        orderbook_limit=29,
        wallet_context_limit=41,
        active_only=False,
        market_category="Crypto",
    )

    assert summary["leaderboard_category"] == "POLITICS"
    assert summary["leaderboard_time_period"] == "WEEK"
    assert summary["market_limit"] == 23
    assert summary["wallet_limit"] == 17
    assert summary["orderbook_limit"] == 29
    assert summary["wallet_context_limit"] == 41
    assert summary["active_only"] is False
    assert summary["market_category"] == "Crypto"
    assert summary["tradeable_orderbook_focus"] is False
    assert summary["markets_ingested"] == 11
    assert summary["leaderboard_ingested"] == 7
    assert summary["liquidity_wallet_discovery"] == {
        "wallets_discovered": 3,
        "wallets_upserted": 3,
    }
    assert summary["wallets_refreshed"] == {"processed": 5, "failed": 0}
    assert summary["wallet_context"] == {"markets_enriched": 3, "recent_wallet_orderbooks": 5}
    assert summary["orderbook_snapshots"] == 19
    assert summary["wallet_scoring"] == {"calculated": 4, "stored": 4}
    assert summary["signal_context"] == {
        "eligible_wallets": 2,
        "candidate_markets": 6,
        "candidate_tokens": 8,
        "markets_enriched": 3,
        "orderbooks_enriched": 5,
    }
    assert summary["signal_scan"] == {"generated": 2, "stored": 2}
    assert summary["signal_observations"] == {
        "observation_only": 3,
        "promotion_ready": 1,
        "newly_ready": 1,
        "persistent_ready": 0,
        "promotion_queue": 0,
        "cleared": 0,
    }
    assert [name for name, _ in calls] == [
        "markets",
        "leaderboard",
        "liquidity_wallets",
        "wallets",
        "wallet_context",
        "orderbook",
        "scores",
        "signal_context",
        "signals",
        "observations",
    ]


def test_run_refresh_cycle_uses_default_limits(monkeypatch, sqlite_settings) -> None:
    calls: dict[str, int] = {}

    def fake_markets(
        settings,
        *,
        limit: int,
        active_only: bool,
        market_category: str | None,
    ) -> int:
        calls["market_limit"] = limit
        calls["active_only"] = int(active_only)
        calls["market_category"] = 1 if market_category else 0
        return 1

    def fake_leaderboard(settings, *, category: str, time_period: str, limit: int) -> int:
        calls["leaderboard_limit"] = limit
        return 1

    def fake_wallets(settings, *, limit: int) -> dict[str, object]:
        calls["wallet_limit"] = limit
        return {"processed": limit}

    def fake_wallet_context(
        settings,
        *,
        wallet_limit: int,
        context_limit: int,
        market_category: str | None,
    ) -> dict[str, int]:
        calls["wallet_limit_seen_by_context"] = wallet_limit
        calls["wallet_context_limit"] = context_limit
        calls["wallet_context_market_category"] = 1 if market_category else 0
        return {"markets_enriched": 0, "recent_wallet_orderbooks": 0}

    def fake_orderbook(settings, *, limit: int, market_category: str | None) -> int:
        calls["orderbook_limit"] = limit
        calls["orderbook_market_category"] = 1 if market_category else 0
        return limit

    monkeypatch.setattr(refresh, "_ingest_markets_stage", fake_markets)
    monkeypatch.setattr(refresh, "_ingest_leaderboard_stage", fake_leaderboard)
    monkeypatch.setattr(
        refresh,
        "_liquidity_wallet_discovery_stage",
        lambda settings, *, market_category: {"wallets_discovered": 0},
    )
    monkeypatch.setattr(refresh, "_ingest_wallets_stage", fake_wallets)
    monkeypatch.setattr(refresh, "_wallet_context_stage", fake_wallet_context)
    monkeypatch.setattr(refresh, "_ingest_orderbook_stage", fake_orderbook)
    monkeypatch.setattr(
        refresh,
        "_score_wallets_stage",
        lambda settings, *, category, leaderboard_time_period: {},
    )
    monkeypatch.setattr(
        refresh,
        "_signal_context_stage",
        lambda settings, *, category, context_limit, market_category: {
            "candidate_tokens": context_limit
        },
    )
    monkeypatch.setattr(
        refresh,
        "_scan_signals_stage",
        lambda settings, *, category, market_category: {},
    )
    monkeypatch.setattr(
        refresh,
        "_observation_watch_stage",
        lambda settings, *, category, market_category: {"promotion_ready": 0},
    )

    summary = refresh.run_refresh_cycle(
        settings=sqlite_settings,
        market_limit=31,
    )

    assert calls["market_limit"] == 31
    assert calls["wallet_limit"] == sqlite_settings.tracked_wallet_limit
    assert calls["wallet_limit_seen_by_context"] == sqlite_settings.tracked_wallet_limit
    assert calls["wallet_context_limit"] == sqlite_settings.wallet_market_enrichment_limit
    assert calls["orderbook_limit"] == 31
    assert calls["leaderboard_limit"] == sqlite_settings.leaderboard_limit
    assert summary["wallet_limit"] == sqlite_settings.tracked_wallet_limit
    assert summary["orderbook_limit"] == 31
    assert summary["wallet_context_limit"] == sqlite_settings.wallet_market_enrichment_limit
    assert summary["market_category"] is None
    assert summary["tradeable_orderbook_focus"] is False
    assert summary["signal_context"] == {
        "candidate_tokens": sqlite_settings.wallet_market_enrichment_limit
    }
    assert summary["signal_observations"] == {"promotion_ready": 0}


def test_observation_watch_stage_tracks_new_persistent_and_cleared(repo, sqlite_settings) -> None:
    now = datetime.now(UTC)
    repo.session.add(
        Market(
            condition_id="cond-obs",
            question="Observation market?",
            category="OVERALL",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
            end_date=now + timedelta(days=1),
        )
    )
    repo.session.add(
        MarketToken(
            condition_id="cond-obs",
            token_id="token-obs",
            outcome="Yes",
            side_label="Yes",
        )
    )
    repo.session.add(
        WalletScore(
            proxy_wallet="0xwallet1",
            category="OVERALL",
            as_of=now,
            score=0.81,
            roi=0.12,
            pnl=42.0,
            closed_market_count=45,
            trade_count=88,
            profit_concentration=0.22,
            raw_metrics_json={"recent_30d_pnl": 42.0},
        )
    )
    repo.session.add(
        WalletActivity(
            proxy_wallet="0xwallet1",
            condition_id="cond-obs",
            token_id="token-obs",
            side="buy",
            price=0.41,
            size=5.0,
            ts=now - timedelta(minutes=2),
        )
    )
    snapshot = MarketSnapshot(
        ts=now - timedelta(seconds=10),
        condition_id="cond-obs",
        token_id="token-obs",
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        last_trade_price=0.47,
        liquidity_score=0.0,
        raw_json={"bids": [], "asks": []},
    )
    repo.session.add(snapshot)
    repo.session.commit()

    first = refresh._observation_watch_stage(sqlite_settings, category="OVERALL")
    second = refresh._observation_watch_stage(sqlite_settings, category="OVERALL")
    third = refresh._observation_watch_stage(sqlite_settings, category="OVERALL")

    assert first == {
        "observation_only": 1,
        "promotion_ready": 1,
        "newly_ready": 1,
        "persistent_ready": 0,
        "promotion_queue": 0,
        "cleared": 0,
    }
    assert second == {
        "observation_only": 1,
        "promotion_ready": 1,
        "newly_ready": 0,
        "persistent_ready": 1,
        "promotion_queue": 0,
        "cleared": 0,
    }
    assert third == {
        "observation_only": 1,
        "promotion_ready": 1,
        "newly_ready": 0,
        "persistent_ready": 1,
        "promotion_queue": 1,
        "cleared": 0,
    }

    repo.session.refresh(snapshot)
    snapshot.last_trade_price = None
    repo.session.commit()

    fourth = refresh._observation_watch_stage(sqlite_settings, category="OVERALL")

    assert fourth == {
        "observation_only": 0,
        "promotion_ready": 0,
        "newly_ready": 0,
        "persistent_ready": 0,
        "promotion_queue": 0,
        "cleared": 1,
    }

    state = repo.get_runtime_state(refresh.observation_watch_state_key("OVERALL"))
    assert state is not None
    assert state.state_json is not None
    assert state.state_json["category"] == "OVERALL"
    assert state.state_json["tokens"] == {}

    signal_events = repo.list_runtime_events(category="signal", limit=10)
    event_types = [event.event_type for event in signal_events]
    assert "signal_observation_ready" in event_types
    assert "signal_observation_persistent" in event_types
    assert "signal_observation_queued" in event_types
    assert "signal_observation_cleared" in event_types


def test_observation_watch_stage_sends_alerts_for_queue_entry_and_clear(
    repo,
    sqlite_settings,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    repo.session.add(
        Market(
            condition_id="cond-alert",
            question="Alert market?",
            category="OVERALL",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
            end_date=now + timedelta(days=1),
        )
    )
    repo.session.add(
        MarketToken(
            condition_id="cond-alert",
            token_id="token-alert",
            outcome="Yes",
            side_label="Yes",
        )
    )
    repo.session.add(
        WalletScore(
            proxy_wallet="0xwallet-alert",
            category="OVERALL",
            as_of=now,
            score=0.82,
            roi=0.15,
            pnl=55.0,
            closed_market_count=50,
            trade_count=90,
            profit_concentration=0.20,
            raw_metrics_json={"recent_30d_pnl": 55.0},
        )
    )
    repo.session.add(
        WalletActivity(
            proxy_wallet="0xwallet-alert",
            condition_id="cond-alert",
            token_id="token-alert",
            side="buy",
            price=0.42,
            size=5.0,
            ts=now - timedelta(minutes=2),
        )
    )
    snapshot = MarketSnapshot(
        ts=now - timedelta(seconds=10),
        condition_id="cond-alert",
        token_id="token-alert",
        best_bid=None,
        best_ask=None,
        midpoint=None,
        spread=None,
        last_trade_price=0.49,
        liquidity_score=0.0,
        raw_json={"bids": [], "asks": []},
    )
    repo.session.add(snapshot)
    repo.session.commit()

    sent: list[dict[str, object]] = []

    def _send_alert(
        event_type: str,
        message: str,
        *,
        payload: dict[str, object] | None = None,
        settings=None,
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

    monkeypatch.setattr("pm_alpha_bot.ops.refresh.send_alert", _send_alert)

    refresh._observation_watch_stage(sqlite_settings, category="OVERALL")
    refresh._observation_watch_stage(sqlite_settings, category="OVERALL")
    refresh._observation_watch_stage(sqlite_settings, category="OVERALL")
    refresh._observation_watch_stage(sqlite_settings, category="OVERALL")
    refresh._observation_watch_stage(sqlite_settings, category="OVERALL")
    refresh._observation_watch_stage(sqlite_settings, category="OVERALL")

    repo.session.refresh(snapshot)
    snapshot.last_trade_price = None
    repo.session.commit()
    refresh._observation_watch_stage(sqlite_settings, category="OVERALL")

    assert [item["event_type"] for item in sent] == [
        "signal_observation_queued",
        "signal_observation_queue_reminder",
        "signal_observation_cleared",
    ]
    assert sent[0]["payload"]["token_id"] == "token-alert"
    assert sent[0]["payload"]["review_commands"][0].startswith(
        "pm-bot report signal-observations --category OVERALL"
    )
    assert "--token-id token-alert" in sent[0]["payload"]["review_commands"][0]
    assert sent[1]["payload"]["token_id"] == "token-alert"
    assert sent[1]["payload"]["observation_streak"] == 6
    assert sent[1]["payload"]["review_commands"][1] == (
        "pm-bot report signal-diagnostics --category OVERALL "
        "--top-candidates 20 --token-id token-alert"
    )
    assert sent[2]["payload"]["token_id"] == "token-alert"
    assert sent[2]["payload"]["review_commands"][2] == (
        "pm-bot report live-events --category signal --top 50"
    )

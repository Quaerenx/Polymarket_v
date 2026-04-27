from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from pm_alpha_bot.db.models import Market, MarketSnapshot, MarketToken
from pm_alpha_bot.domain import (
    LeaderboardEntry,
    NormalizedMarket,
    NormalizedMarketToken,
    OrderBookSnapshot,
    WalletActivityRecord,
    WalletScoreRecord,
)
from pm_alpha_bot.ingest.leaderboard import ingest_leaderboard
from pm_alpha_bot.ingest.liquidity_wallets import discover_liquid_trade_wallets
from pm_alpha_bot.ingest.orderbook import ingest_orderbook, select_tokens_for_orderbook_refresh
from pm_alpha_bot.ingest.wallet_context import (
    enrich_signal_inputs_for_category,
    ingest_orderbooks_for_recent_wallet_tokens,
)
from pm_alpha_bot.ingest.wallets import ingest_wallets
from pm_alpha_bot.scoring.wallet_score import calculate_wallet_scores


@pytest.mark.asyncio()
@respx.mock
async def test_ingest_leaderboard_and_wallets(repo) -> None:
    wallet = "0x1111111111111111111111111111111111111111"
    respx.get("https://data-api.polymarket.com/v1/leaderboard").mock(
        return_value=httpx.Response(
            200, json=[{"proxyWallet": wallet, "userName": "alpha", "pnl": 100, "vol": 1000}]
        )
    )
    count = await ingest_leaderboard(repo, category="OVERALL", time_period="MONTH", limit=10)
    assert count == 1
    leaderboard_rows = repo.latest_leaderboard_snapshots(
        category="OVERALL",
        time_period="MONTH",
    )
    assert len(leaderboard_rows) == 1
    assert leaderboard_rows[0].proxy_wallet == wallet
    assert leaderboard_rows[0].pnl == pytest.approx(100.0)

    respx.get("https://data-api.polymarket.com/positions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "proxyWallet": wallet,
                    "conditionId": "cond-1",
                    "asset": "token-1",
                    "size": 4,
                    "avgPrice": 0.5,
                }
            ],
        )
    )
    respx.get("https://data-api.polymarket.com/closed-positions").mock(
        return_value=httpx.Response(200, json=[{"conditionId": "cond-1", "cashPnl": 2.0}])
    )
    respx.get("https://data-api.polymarket.com/activity").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "conditionId": "cond-1",
                    "asset": "token-1",
                    "side": "buy",
                    "price": 0.5,
                    "size": 4,
                }
            ],
        )
    )
    respx.get("https://data-api.polymarket.com/trades").mock(
        return_value=httpx.Response(200, json=[{"id": "t1"}])
    )
    respx.get("https://data-api.polymarket.com/value").mock(
        return_value=httpx.Response(200, json={"totalValue": 100})
    )
    summary = await ingest_wallets(repo, limit=10)
    assert summary["wallets"] == 1
    persisted = repo.get_wallet(wallet)
    assert persisted is not None
    assert "closed_positions" in (persisted.raw_json or {})
    detail_snapshot = repo.latest_wallet_detail_snapshot(wallet)
    assert detail_snapshot is not None
    assert detail_snapshot.total_value_json == {"totalValue": 100}


@pytest.mark.asyncio()
@respx.mock
async def test_discover_liquid_trade_wallets_adds_wallet_activity_and_wallet(
    repo, sqlite_settings
) -> None:
    now = datetime(2024, 3, 9, 16, 0, tzinfo=UTC)
    wallet = "0x2222222222222222222222222222222222222222"
    respx.get("https://data-api.polymarket.com/trades").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "proxyWallet": wallet,
                    "conditionId": "cond-liquid",
                    "asset": "token-liquid",
                    "side": "BUY",
                    "price": 0.45,
                    "size": 10,
                    "timestamp": int(now.timestamp()),
                    "transactionHash": "0xtrade",
                    "pseudonym": "liquid-wallet",
                }
            ],
        )
    )
    respx.get("https://clob-v2.polymarket.com/markets/cond-liquid").mock(
        return_value=httpx.Response(
            200,
            json={
                "condition_id": "cond-liquid",
                "question": "Liquid crypto",
                "tags": ["Crypto"],
                "active": True,
                "closed": False,
                "accepting_orders": True,
                "enable_order_book": True,
                "minimum_tick_size": "0.01",
                "minimum_order_size": "1",
                "tokens": [{"token_id": "token-liquid", "outcome": "Yes"}],
            },
        )
    )
    respx.get("https://clob-v2.polymarket.com/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "cond-liquid",
                "asset_id": "token-liquid",
                "timestamp": str(int(now.timestamp())),
                "bids": [{"price": "0.44", "size": "10"}],
                "asks": [{"price": "0.46", "size": "12"}],
                "last_trade_price": "0.45",
            },
        )
    )

    summary = await discover_liquid_trade_wallets(
        repo,
        settings=sqlite_settings,
        trade_limit=10,
        wallet_limit=5,
        as_of=now,
    )

    assert summary["recent_trades"] == 1
    assert summary["markets_enriched"] == 1
    assert summary["orderbooks_enriched"] == 1
    assert summary["liquid_trade_rows"] == 1
    assert summary["wallets_upserted"] == 1
    persisted = repo.get_wallet(wallet)
    assert persisted is not None
    assert (persisted.raw_json or {})["liquidity_discovery"]["notional"] == pytest.approx(4.5)
    assert repo.recent_wallet_activity(proxy_wallets=[wallet])


@pytest.mark.asyncio()
@respx.mock
async def test_discover_liquid_trade_wallets_uses_liquid_market_trades(
    repo, sqlite_settings
) -> None:
    now = datetime(2024, 3, 9, 16, 0, tzinfo=UTC)
    wallet = "0x3333333333333333333333333333333333333333"
    repo.session.add(
        Market(
            condition_id="cond-liquid",
            question="Liquid crypto",
            category="Crypto",
            active=True,
            closed=False,
        )
    )
    repo.session.add(
        MarketToken(
            condition_id="cond-liquid",
            token_id="token-liquid",
            outcome="Yes",
            side_label="Yes",
        )
    )
    repo.session.add(
        MarketSnapshot(
            condition_id="cond-liquid",
            token_id="token-liquid",
            ts=now,
            best_bid=0.44,
            best_ask=0.46,
            midpoint=0.45,
            spread=0.02,
        )
    )
    repo.session.commit()

    def trades_response(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("market") == "cond-liquid":
            return httpx.Response(
                200,
                json=[
                    {
                        "proxyWallet": wallet,
                        "conditionId": "cond-liquid",
                        "asset": "token-liquid",
                        "side": "SELL",
                        "price": 0.46,
                        "size": 8,
                        "timestamp": int(now.timestamp()),
                        "transactionHash": "0xmarkettrade",
                    }
                ],
            )
        return httpx.Response(200, json=[])

    respx.get("https://data-api.polymarket.com/trades").mock(side_effect=trades_response)

    summary = await discover_liquid_trade_wallets(
        repo,
        settings=sqlite_settings,
        trade_limit=10,
        wallet_limit=5,
        as_of=now,
    )

    assert summary["recent_trades"] == 1
    assert summary["liquid_markets"] == 1
    assert summary["market_trades"] == 1
    assert summary["liquid_trade_rows"] == 1
    assert summary["wallets_upserted"] == 1
    persisted = repo.get_wallet(wallet)
    assert persisted is not None
    assert (persisted.raw_json or {})["liquidity_discovery"]["notional"] == pytest.approx(3.68)


def test_calculate_wallet_scores_uses_pit_leaderboard_universe(repo) -> None:
    wallet_old = "0x1111111111111111111111111111111111111111"
    wallet_new = "0x2222222222222222222222222222222222222222"
    t_old = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
    t_new = t_old + timedelta(days=1)
    repo.upsert_wallets_from_leaderboard(
        [
            LeaderboardEntry(
                proxyWallet=wallet_old,
                userName="old",
                pnl=100.0,
                vol=1000.0,
                category="POLITICS",
                raw_json={"pnl": 100.0, "vol": 1000.0},
            )
        ],
        time_period="MONTH",
        observed_at=t_old,
    )
    repo.update_wallet_details(
        wallet_old,
        closed_positions=[{"cashPnl": 5.0} for _ in range(30)],
        trades=[{"id": index} for index in range(50)],
        total_value={"totalValue": 1000.0},
        observed_at=t_old,
    )
    repo.upsert_wallets_from_leaderboard(
        [
            LeaderboardEntry(
                proxyWallet=wallet_old,
                userName="old",
                pnl=110.0,
                vol=1100.0,
                category="POLITICS",
                raw_json={"pnl": 110.0, "vol": 1100.0},
            ),
            LeaderboardEntry(
                proxyWallet=wallet_new,
                userName="new",
                pnl=300.0,
                vol=2000.0,
                category="POLITICS",
                raw_json={"pnl": 300.0, "vol": 2000.0},
            ),
        ],
        time_period="MONTH",
        observed_at=t_new,
    )
    repo.update_wallet_details(
        wallet_old,
        closed_positions=[{"cashPnl": 100.0}],
        trades=[{"id": index} for index in range(2)],
        total_value={"totalValue": 1000.0},
        observed_at=t_new,
    )
    repo.update_wallet_details(
        wallet_new,
        closed_positions=[{"cashPnl": 7.0} for _ in range(30)],
        trades=[{"id": index} for index in range(50)],
        total_value={"totalValue": 1000.0},
        observed_at=t_new,
    )
    repo.session.commit()

    early_rows = calculate_wallet_scores(
        repo,
        category="POLITICS",
        as_of=t_old + timedelta(hours=1),
        leaderboard_time_period="MONTH",
    )
    late_rows = calculate_wallet_scores(
        repo,
        category="POLITICS",
        as_of=t_new + timedelta(hours=1),
        leaderboard_time_period="MONTH",
    )

    assert [row.proxy_wallet for row in early_rows] == [wallet_old]
    assert {row.proxy_wallet for row in late_rows} == {wallet_old, wallet_new}
    assert (early_rows[0].raw_metrics_json or {}).get("pit_leaderboard") is True
    assert (early_rows[0].raw_metrics_json or {}).get("pit_wallet_details") is True
    assert early_rows[0].closed_market_count == 30
    late_by_wallet = {row.proxy_wallet: row for row in late_rows}
    assert late_by_wallet[wallet_old].closed_market_count == 1


def test_calculate_wallet_scores_includes_liquidity_discovered_wallets(repo) -> None:
    leaderboard_wallet = "0x1111111111111111111111111111111111111111"
    discovered_wallet = "0x2222222222222222222222222222222222222222"
    now = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
    repo.upsert_wallets_from_leaderboard(
        [
            LeaderboardEntry(
                proxyWallet=leaderboard_wallet,
                userName="leader",
                pnl=100.0,
                vol=1000.0,
                category="OVERALL",
                raw_json={"pnl": 100.0, "vol": 1000.0},
            )
        ],
        time_period="MONTH",
        observed_at=now,
    )
    repo.upsert_discovered_wallets(
        [
            {
                "proxy_wallet": discovered_wallet,
                "username": "discovered",
                "trade_count": 1,
                "notional": 5.0,
                "categories": ["Crypto"],
                "condition_ids": ["cond-liquid"],
                "token_ids": ["token-liquid"],
            }
        ],
        observed_at=now,
    )

    rows = calculate_wallet_scores(repo, category="OVERALL", as_of=now)

    by_wallet = {row.proxy_wallet: row for row in rows}
    assert set(by_wallet) == {leaderboard_wallet, discovered_wallet}
    assert (by_wallet[leaderboard_wallet].raw_metrics_json or {})["pit_leaderboard"] is True
    assert (by_wallet[discovered_wallet].raw_metrics_json or {})["pit_leaderboard"] is False


@pytest.mark.asyncio()
@respx.mock
async def test_ingest_orderbook(repo) -> None:
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
    respx.get("https://clob-v2.polymarket.com/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "cond-1",
                "asset_id": "token-1",
                "timestamp": "1710000000",
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.42", "size": "12"}],
                "last_trade_price": "0.41",
            },
        )
    )
    count = await ingest_orderbook(repo, limit=10)
    assert count == 1
    assert repo.latest_snapshot_for_token("token-1") is not None


@pytest.mark.asyncio()
@respx.mock
async def test_ingest_orderbook_respects_market_category(repo) -> None:
    repo.session.add_all(
        [
            Market(
                condition_id="cond-crypto",
                question="Crypto question",
                category="Crypto",
                active=True,
                closed=False,
            ),
            Market(
                condition_id="cond-sports",
                question="Sports question",
                category="Sports",
                active=True,
                closed=False,
            ),
        ]
    )
    repo.session.add_all(
        [
            MarketToken(
                condition_id="cond-crypto",
                token_id="token-crypto",
                outcome="Yes",
                side_label="Yes",
            ),
            MarketToken(
                condition_id="cond-sports",
                token_id="token-sports",
                outcome="Yes",
                side_label="Yes",
            ),
        ]
    )
    repo.session.commit()

    requested_tokens: list[str] = []

    def _book_response(request: httpx.Request) -> httpx.Response:
        token_id = str(request.url.params.get("token_id"))
        requested_tokens.append(token_id)
        return httpx.Response(
            200,
            json={
                "market": "cond-crypto" if token_id == "token-crypto" else "cond-sports",
                "asset_id": token_id,
                "timestamp": "1710000000",
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.42", "size": "12"}],
                "last_trade_price": "0.41",
            },
        )

    respx.get("https://clob-v2.polymarket.com/book").mock(side_effect=_book_response)

    count = await ingest_orderbook(repo, limit=10, market_category="crypto")

    assert count == 1
    assert requested_tokens == ["token-crypto"]
    assert repo.latest_snapshot_for_token("token-crypto") is not None
    assert repo.latest_snapshot_for_token("token-sports") is None


def test_orderbook_refresh_round_robins_categories_when_unfiltered(repo, sqlite_settings) -> None:
    repo.session.add_all(
        [
            Market(
                condition_id="cond-sports-a",
                question="Sports A",
                category="Sports",
                active=True,
                closed=False,
            ),
            Market(
                condition_id="cond-sports-b",
                question="Sports B",
                category="Sports",
                active=True,
                closed=False,
            ),
            Market(
                condition_id="cond-crypto-a",
                question="Crypto A",
                category="Crypto",
                active=True,
                closed=False,
            ),
            Market(
                condition_id="cond-politics-a",
                question="Politics A",
                category="Politics",
                active=True,
                closed=False,
            ),
        ]
    )
    repo.session.add_all(
        [
            MarketToken(
                condition_id="cond-sports-a",
                token_id="token-sports-a",
                outcome="Yes",
                side_label="Yes",
            ),
            MarketToken(
                condition_id="cond-sports-b",
                token_id="token-sports-b",
                outcome="Yes",
                side_label="Yes",
            ),
            MarketToken(
                condition_id="cond-crypto-a",
                token_id="token-crypto-a",
                outcome="Yes",
                side_label="Yes",
            ),
            MarketToken(
                condition_id="cond-politics-a",
                token_id="token-politics-a",
                outcome="Yes",
                side_label="Yes",
            ),
        ]
    )
    repo.session.commit()

    token_ids = select_tokens_for_orderbook_refresh(
        repo,
        limit=3,
        settings=sqlite_settings,
    )

    assert token_ids == ["token-sports-a", "token-crypto-a", "token-politics-a"]


@pytest.mark.asyncio()
@respx.mock
async def test_ingest_orderbook_skips_missing_books(repo) -> None:
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Question",
            category="POLITICS",
            active=True,
            closed=False,
        )
    )
    repo.session.add_all(
        [
            MarketToken(condition_id="cond-1", token_id="token-1", outcome="Yes", side_label="Yes"),
            MarketToken(condition_id="cond-1", token_id="token-404", outcome="No", side_label="No"),
        ]
    )
    repo.session.commit()
    def _book_response(request: httpx.Request) -> httpx.Response:
        token_id = request.url.params.get("token_id")
        if token_id == "token-1":
            return httpx.Response(
                200,
                json={
                    "market": "cond-1",
                    "asset_id": "token-1",
                    "timestamp": "1710000000",
                    "bids": [{"price": "0.40", "size": "10"}],
                    "asks": [{"price": "0.42", "size": "12"}],
                    "last_trade_price": "0.41",
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    respx.get("https://clob-v2.polymarket.com/book").mock(side_effect=_book_response)

    count = await ingest_orderbook(repo, limit=10)

    assert count == 1
    assert repo.latest_snapshot_for_token("token-1") is not None
    assert repo.latest_snapshot_for_token("token-404") is None


@pytest.mark.asyncio()
@respx.mock
async def test_ingest_orderbook_skips_cached_missing_books(repo, sqlite_settings) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Question",
            category="POLITICS",
            active=True,
            closed=False,
        )
    )
    repo.session.add_all(
        [
            MarketToken(condition_id="cond-1", token_id="token-1", outcome="Yes", side_label="Yes"),
            MarketToken(condition_id="cond-1", token_id="token-404", outcome="No", side_label="No"),
        ]
    )
    repo.upsert_runtime_state(
        "missing_orderbooks",
        {"updated_at": now.isoformat(), "tokens": {"token-404": now.isoformat()}},
    )
    repo.session.commit()

    requested_tokens: list[str] = []

    def _book_response(request: httpx.Request) -> httpx.Response:
        token_id = str(request.url.params.get("token_id"))
        requested_tokens.append(token_id)
        if token_id == "token-1":
            return httpx.Response(
                200,
                json={
                    "market": "cond-1",
                    "asset_id": "token-1",
                    "timestamp": "1710000000",
                    "bids": [{"price": "0.40", "size": "10"}],
                    "asks": [{"price": "0.42", "size": "12"}],
                    "last_trade_price": "0.41",
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    respx.get("https://clob-v2.polymarket.com/book").mock(side_effect=_book_response)

    count = await ingest_orderbook(repo, limit=10, settings=sqlite_settings)

    assert count == 1
    assert requested_tokens == ["token-1"]
    assert repo.latest_snapshot_for_token("token-1") is not None
    assert repo.latest_snapshot_for_token("token-404") is None


@pytest.mark.asyncio()
@respx.mock
async def test_ingest_orderbook_focuses_on_tradeable_tokens(repo, sqlite_settings) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    repo.session.add_all(
        [
            Market(
                condition_id="cond-tradeable",
                question="Tradeable",
                category="POLITICS",
                active=True,
                closed=False,
            ),
            Market(
                condition_id="cond-dead",
                question="Dead",
                category="POLITICS",
                active=True,
                closed=False,
            ),
        ]
    )
    repo.session.add_all(
        [
            MarketToken(
                condition_id="cond-tradeable",
                token_id="token-live",
                outcome="Yes",
                side_label="Yes",
            ),
            MarketToken(
                condition_id="cond-dead",
                token_id="token-dead",
                outcome="Yes",
                side_label="Yes",
            ),
        ]
    )
    repo.save_snapshots(
        [
            OrderBookSnapshot(
                ts=now,
                condition_id="cond-tradeable",
                token_id="token-live",
                best_bid=0.40,
                best_ask=0.42,
                midpoint=0.41,
                spread=0.02,
                last_trade_price=0.41,
                raw_json={
                    "bids": [{"price": "0.40", "size": "10"}],
                    "asks": [{"price": "0.42", "size": "12"}],
                },
            ),
            OrderBookSnapshot(
                ts=now,
                condition_id="cond-dead",
                token_id="token-dead",
                best_bid=None,
                best_ask=None,
                midpoint=None,
                spread=None,
                raw_json={"bids": [], "asks": []},
            ),
        ]
    )
    repo.session.commit()

    requested_tokens: list[str] = []

    def _book_response(request: httpx.Request) -> httpx.Response:
        token_id = str(request.url.params.get("token_id"))
        requested_tokens.append(token_id)
        return httpx.Response(
            200,
            json={
                "market": "cond-tradeable",
                "asset_id": token_id,
                "timestamp": "1710000000",
                "bids": [{"price": "0.41", "size": "10"}],
                "asks": [{"price": "0.43", "size": "12"}],
                "last_trade_price": "0.42",
            },
        )

    respx.get("https://clob-v2.polymarket.com/book").mock(side_effect=_book_response)

    count = await ingest_orderbook(
        repo,
        limit=1,
        settings=sqlite_settings.model_copy(update={"tradeable_orderbook_focus": True}),
    )

    assert count == 1
    assert requested_tokens == ["token-live"]


@pytest.mark.asyncio()
@respx.mock
async def test_ingest_orderbook_focus_discovers_when_slots_remain(repo, sqlite_settings) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    repo.session.add_all(
        [
            Market(
                condition_id="cond-tradeable",
                question="Tradeable",
                category="POLITICS",
                active=True,
                closed=False,
            ),
            Market(
                condition_id="cond-discovery",
                question="Discovery",
                category="POLITICS",
                active=True,
                closed=False,
            ),
        ]
    )
    repo.session.add_all(
        [
            MarketToken(
                condition_id="cond-tradeable",
                token_id="token-live",
                outcome="Yes",
                side_label="Yes",
            ),
            MarketToken(
                condition_id="cond-discovery",
                token_id="token-discovery",
                outcome="Yes",
                side_label="Yes",
            ),
        ]
    )
    repo.save_snapshots(
        [
            OrderBookSnapshot(
                ts=now,
                condition_id="cond-tradeable",
                token_id="token-live",
                best_bid=0.40,
                best_ask=0.42,
                midpoint=0.41,
                spread=0.02,
                last_trade_price=0.41,
                raw_json={
                    "bids": [{"price": "0.40", "size": "10"}],
                    "asks": [{"price": "0.42", "size": "12"}],
                },
            )
        ]
    )
    repo.session.commit()

    requested_tokens: list[str] = []

    def _book_response(request: httpx.Request) -> httpx.Response:
        token_id = str(request.url.params.get("token_id"))
        requested_tokens.append(token_id)
        condition_id = "cond-tradeable" if token_id == "token-live" else "cond-discovery"
        return httpx.Response(
            200,
            json={
                "market": condition_id,
                "asset_id": token_id,
                "timestamp": str(int(now.timestamp())),
                "bids": [{"price": "0.41", "size": "10"}],
                "asks": [{"price": "0.43", "size": "12"}],
                "last_trade_price": "0.42",
            },
        )

    respx.get("https://clob-v2.polymarket.com/book").mock(side_effect=_book_response)

    count = await ingest_orderbook(
        repo,
        limit=2,
        settings=sqlite_settings.model_copy(update={"tradeable_orderbook_focus": True}),
    )

    assert count == 2
    assert requested_tokens == ["token-live", "token-discovery"]


@pytest.mark.asyncio()
@respx.mock
async def test_ingest_recent_wallet_orderbooks_skips_missing_books(repo) -> None:
    wallet = "0x1111111111111111111111111111111111111111"
    now = datetime.now(UTC).replace(microsecond=0)
    repo.session.add(
        Market(
            condition_id="cond-1",
            question="Question",
            category="POLITICS",
            active=True,
            closed=False,
        )
    )
    repo.session.add_all(
        [
            MarketToken(condition_id="cond-1", token_id="token-1", outcome="Yes", side_label="Yes"),
            MarketToken(condition_id="cond-1", token_id="token-404", outcome="No", side_label="No"),
        ]
    )
    repo.add_wallet_activity(
        [
            WalletActivityRecord(
                proxy_wallet=wallet,
                ts=now,
                condition_id="cond-1",
                token_id="token-1",
                side="buy",
                price=0.5,
                size=4,
            ),
            WalletActivityRecord(
                proxy_wallet=wallet,
                ts=now - timedelta(seconds=1),
                condition_id="cond-1",
                token_id="token-404",
                side="buy",
                price=0.5,
                size=4,
            ),
        ]
    )
    repo.session.commit()

    def _book_response(request: httpx.Request) -> httpx.Response:
        token_id = request.url.params.get("token_id")
        if token_id == "token-1":
            return httpx.Response(
                200,
                json={
                    "market": "cond-1",
                    "asset_id": "token-1",
                    "timestamp": "1710000000",
                    "bids": [{"price": "0.40", "size": "10"}],
                    "asks": [{"price": "0.42", "size": "12"}],
                    "last_trade_price": "0.41",
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    respx.get("https://clob-v2.polymarket.com/book").mock(side_effect=_book_response)

    count = await ingest_orderbooks_for_recent_wallet_tokens(
        repo,
        proxy_wallets=[wallet],
        since=now,
        until=now,
        limit=10,
    )

    assert count == 1
    assert repo.latest_snapshot_for_token("token-1") is not None
    assert repo.latest_snapshot_for_token("token-404") is None


@pytest.mark.asyncio()
@respx.mock
async def test_enrich_signal_inputs_backfills_missing_market_and_snapshot(
    repo, sqlite_settings
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    wallet = "0x1111111111111111111111111111111111111111"
    repo.add_wallet_activity(
        [
            WalletActivityRecord(
                proxy_wallet=wallet,
                ts=now,
                condition_id="cond-signal",
                token_id="token-signal",
                side="buy",
                price=0.5,
                size=4,
            )
        ]
    )
    repo.save_wallet_scores(
        [
            WalletScoreRecord(
                proxy_wallet=wallet,
                as_of=now,
                category="OVERALL",
                pnl=100.0,
                roi=0.1,
                win_rate=0.6,
                closed_market_count=30,
                trade_count=50,
                max_drawdown=0.1,
                profit_concentration=0.2,
                score=0.5,
                raw_metrics_json={"recent_30d_pnl": 50.0},
            )
        ]
    )
    repo.session.commit()

    respx.get("https://clob-v2.polymarket.com/markets/cond-signal").mock(
        return_value=httpx.Response(
            200,
            json={
                "condition_id": "cond-signal",
                "question": "Question",
                "active": True,
                "closed": False,
                "minimum_tick_size": "0.01",
                "minimum_order_size": "1",
                "tokens": [{"token_id": "token-signal", "outcome": "Yes"}],
            },
        )
    )
    respx.get("https://clob-v2.polymarket.com/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "cond-signal",
                "asset_id": "token-signal",
                "timestamp": "1710000000",
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.42", "size": "12"}],
                "last_trade_price": "0.41",
            },
        )
    )

    summary = await enrich_signal_inputs_for_category(
        repo,
        category="OVERALL",
        settings=sqlite_settings,
        as_of=now,
        limit=10,
    )

    assert summary == {
        "eligible_wallets": 1,
        "candidate_markets": 1,
        "candidate_tokens": 1,
        "markets_enriched": 1,
        "orderbooks_enriched": 1,
    }
    assert repo.get_market_for_token("token-signal") is not None
    assert repo.latest_snapshot_for_token("token-signal", as_of=now) is not None


@pytest.mark.asyncio()
@respx.mock
async def test_enrich_signal_inputs_refreshes_stale_empty_snapshot(repo, sqlite_settings) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    wallet = "0x1111111111111111111111111111111111111111"
    repo.session.add(
        Market(
            condition_id="cond-signal",
            question="Question",
            category="Sports",
            active=True,
            closed=False,
        )
    )
    repo.session.add(
        MarketToken(
            condition_id="cond-signal",
            token_id="token-signal",
            outcome="Yes",
            side_label="Yes",
        )
    )
    repo.add_wallet_activity(
        [
            WalletActivityRecord(
                proxy_wallet=wallet,
                ts=now,
                condition_id="cond-signal",
                token_id="token-signal",
                side="buy",
                price=0.5,
                size=4,
            )
        ]
    )
    repo.save_wallet_scores(
        [
            WalletScoreRecord(
                proxy_wallet=wallet,
                as_of=now,
                category="OVERALL",
                pnl=100.0,
                roi=0.1,
                win_rate=0.6,
                closed_market_count=30,
                trade_count=50,
                max_drawdown=0.1,
                profit_concentration=0.2,
                score=0.5,
                raw_metrics_json={"recent_30d_pnl": 50.0},
            )
        ]
    )
    repo.save_snapshots(
        [
            OrderBookSnapshot(
                ts=now - timedelta(minutes=5),
                condition_id="cond-signal",
                token_id="token-signal",
                best_bid=None,
                best_ask=None,
                midpoint=None,
                spread=None,
                raw_json={"bids": [], "asks": []},
            )
        ]
    )
    repo.session.commit()

    requested_tokens: list[str] = []

    def _book_response(request: httpx.Request) -> httpx.Response:
        token_id = str(request.url.params.get("token_id"))
        requested_tokens.append(token_id)
        return httpx.Response(
            200,
            json={
                "market": "cond-signal",
                "asset_id": token_id,
                "timestamp": str(int(now.timestamp())),
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.42", "size": "12"}],
                "last_trade_price": "0.41",
            },
        )

    respx.get("https://clob-v2.polymarket.com/book").mock(side_effect=_book_response)

    summary = await enrich_signal_inputs_for_category(
        repo,
        category="OVERALL",
        settings=sqlite_settings,
        as_of=now,
        limit=10,
    )

    assert summary == {
        "eligible_wallets": 1,
        "candidate_markets": 0,
        "candidate_tokens": 1,
        "markets_enriched": 0,
        "orderbooks_enriched": 1,
    }
    assert requested_tokens == ["token-signal"]
    refreshed = repo.latest_snapshot_for_token("token-signal", as_of=now)
    assert refreshed is not None
    assert refreshed.best_bid == pytest.approx(0.40)


@pytest.mark.asyncio()
@respx.mock
async def test_enrich_signal_inputs_refreshes_non_tradeable_market_metadata(
    repo, sqlite_settings
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    wallet = "0x1111111111111111111111111111111111111111"
    repo.session.add(
        Market(
            condition_id="cond-signal",
            question="Question",
            category="Sports",
            active=True,
            closed=False,
            end_date=now - timedelta(hours=1),
        )
    )
    repo.session.add(
        MarketToken(
            condition_id="cond-signal",
            token_id="token-signal",
            outcome="Yes",
            side_label="Yes",
        )
    )
    repo.add_wallet_activity(
        [
            WalletActivityRecord(
                proxy_wallet=wallet,
                ts=now,
                condition_id="cond-signal",
                token_id="token-signal",
                side="buy",
                price=0.5,
                size=4,
            )
        ]
    )
    repo.save_wallet_scores(
        [
            WalletScoreRecord(
                proxy_wallet=wallet,
                as_of=now,
                category="OVERALL",
                pnl=100.0,
                roi=0.1,
                win_rate=0.6,
                closed_market_count=30,
                trade_count=50,
                max_drawdown=0.1,
                profit_concentration=0.2,
                score=0.5,
                raw_metrics_json={"recent_30d_pnl": 50.0},
            )
        ]
    )
    repo.session.commit()

    respx.get("https://clob-v2.polymarket.com/markets/cond-signal").mock(
        return_value=httpx.Response(
            200,
            json={
                "condition_id": "cond-signal",
                "question": "Question",
                "active": True,
                "closed": False,
                "accepting_orders": True,
                "enable_order_book": True,
                "end_date_iso": (now - timedelta(hours=1)).isoformat(),
                "minimum_tick_size": "0.01",
                "minimum_order_size": "1",
                "tokens": [{"token_id": "token-signal", "outcome": "Yes"}],
            },
        )
    )
    respx.get("https://clob-v2.polymarket.com/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "cond-signal",
                "asset_id": "token-signal",
                "timestamp": str(int(now.timestamp())),
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.42", "size": "12"}],
                "last_trade_price": "0.41",
            },
        )
    )

    summary = await enrich_signal_inputs_for_category(
        repo,
        category="OVERALL",
        settings=sqlite_settings,
        as_of=now,
        limit=10,
    )

    assert summary == {
        "eligible_wallets": 1,
        "candidate_markets": 1,
        "candidate_tokens": 1,
        "markets_enriched": 1,
        "orderbooks_enriched": 1,
    }


@pytest.mark.asyncio()
async def test_enrich_signal_inputs_skips_category_without_tradeable_orderbooks(
    repo, sqlite_settings
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    wallet = "0x1111111111111111111111111111111111111111"
    repo.session.add(
        Market(
            condition_id="cond-signal",
            question="Question",
            category="Sports",
            active=True,
            closed=False,
            raw_json={
                "active": True,
                "closed": False,
                "accepting_orders": True,
                "enable_order_book": True,
            },
        )
    )
    repo.session.add(
        MarketToken(
            condition_id="cond-signal",
            token_id="token-signal",
            outcome="Yes",
            side_label="Yes",
        )
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
            )
        )
        repo.session.add(
            MarketSnapshot(
                ts=now - timedelta(minutes=1),
                condition_id=condition_id,
                token_id=f"token-empty-{index}",
                best_bid=None,
                best_ask=None,
                midpoint=None,
                spread=None,
                liquidity_score=0.0,
            )
        )
    repo.add_wallet_activity(
        [
            WalletActivityRecord(
                proxy_wallet=wallet,
                ts=now,
                condition_id="cond-signal",
                token_id="token-signal",
                side="buy",
                price=0.5,
                size=4,
            )
        ]
    )
    repo.save_wallet_scores(
        [
            WalletScoreRecord(
                proxy_wallet=wallet,
                as_of=now,
                category="OVERALL",
                pnl=100.0,
                roi=0.1,
                win_rate=0.6,
                closed_market_count=30,
                trade_count=50,
                max_drawdown=0.1,
                profit_concentration=0.2,
                score=0.5,
                raw_metrics_json={"recent_30d_pnl": 50.0},
            )
        ]
    )
    repo.session.commit()

    summary = await enrich_signal_inputs_for_category(
        repo,
        category="OVERALL",
        settings=sqlite_settings,
        as_of=now,
        limit=10,
    )

    assert summary == {
        "eligible_wallets": 1,
        "candidate_markets": 0,
        "candidate_tokens": 0,
        "markets_enriched": 0,
        "orderbooks_enriched": 0,
    }


def test_upsert_markets_deduplicates_pending_condition_ids(repo) -> None:
    count = repo.upsert_markets(
        [
            NormalizedMarket(
                condition_id="cond-dup",
                question="Question A",
                slug="question-a",
                tokens=[
                    NormalizedMarketToken(
                        condition_id="cond-dup",
                        token_id="token-a",
                        outcome="Yes",
                        side_label="Yes",
                    )
                ],
            ),
            NormalizedMarket(
                condition_id="cond-dup",
                question="Question A refreshed",
                slug="question-a-refreshed",
                tokens=[
                    NormalizedMarketToken(
                        condition_id="cond-dup",
                        token_id="token-a",
                        outcome="Yes",
                        side_label="Yes",
                    ),
                    NormalizedMarketToken(
                        condition_id="cond-dup",
                        token_id="token-b",
                        outcome="No",
                        side_label="No",
                    ),
                ],
            ),
        ]
    )

    repo.session.commit()

    assert count == 1
    market = repo.get_market_by_condition_id("cond-dup")
    assert market is not None
    assert market.question == "Question A refreshed"
    assert market.slug == "question-a-refreshed"
    tokens = sorted(token.token_id for token in repo.list_active_tokens(limit=10))
    assert tokens == ["token-a", "token-b"]


def test_upsert_markets_updates_existing_condition_id(repo) -> None:
    repo.upsert_markets(
        [
            NormalizedMarket(
                condition_id="cond-existing",
                question="Original Question",
                slug="original-question",
                tokens=[
                    NormalizedMarketToken(
                        condition_id="cond-existing",
                        token_id="token-original",
                        outcome="Yes",
                        side_label="Yes",
                    )
                ],
            )
        ]
    )
    repo.session.commit()

    count = repo.upsert_markets(
        [
            NormalizedMarket(
                condition_id="cond-existing",
                question="Updated Question",
                slug="updated-question",
                category="POLITICS",
                tokens=[
                    NormalizedMarketToken(
                        condition_id="cond-existing",
                        token_id="token-original",
                        outcome="Yes",
                        side_label="Yes",
                    ),
                    NormalizedMarketToken(
                        condition_id="cond-existing",
                        token_id="token-new",
                        outcome="No",
                        side_label="No",
                    ),
                ],
            )
        ]
    )
    repo.session.commit()

    assert count == 1
    market = repo.get_market_by_condition_id("cond-existing")
    assert market is not None
    assert market.question == "Updated Question"
    assert market.slug == "updated-question"
    assert market.category == "POLITICS"
    tokens = sorted(
        token.token_id
        for token in repo.session.query(MarketToken)
        .filter(MarketToken.condition_id == "cond-existing")
        .all()
    )
    assert tokens == ["token-new", "token-original"]

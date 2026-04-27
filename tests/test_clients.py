from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from pm_alpha_bot.clients.clob_public import ClobPublicClient
from pm_alpha_bot.clients.data_api import DataApiClient
from pm_alpha_bot.clients.gamma import GammaClient


@pytest.mark.asyncio()
@respx.mock
async def test_gamma_market_normalization() -> None:
    respx.get("https://gamma-api.polymarket.com/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "market-1",
                    "question": "Will it rain?",
                    "conditionId": "cond-1",
                    "category": "WEATHER",
                    "slug": "will-it-rain",
                    "active": True,
                    "closed": False,
                    "orderPriceMinTickSize": 0.01,
                    "orderMinSize": 1,
                    "clobTokenIds": '["token-yes","token-no"]',
                    "outcomes": '["Yes","No"]',
                }
            ],
        )
    )
    async with GammaClient() as client:
        markets = await client.list_markets(limit=10, active_only=True)
    assert len(markets) == 1
    assert markets[0].condition_id == "cond-1"
    assert [token.token_id for token in markets[0].tokens] == ["token-yes", "token-no"]


@pytest.mark.asyncio()
@respx.mock
async def test_data_api_normalization() -> None:
    wallet = "0x1111111111111111111111111111111111111111"
    respx.get("https://data-api.polymarket.com/v1/leaderboard").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"proxyWallet": wallet, "userName": "alpha", "pnl": 100, "vol": 1000, "rank": "1"}
            ],
        )
    )
    respx.get("https://data-api.polymarket.com/positions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "proxyWallet": wallet,
                    "asset": "token-yes",
                    "conditionId": "cond-1",
                    "size": 10,
                    "avgPrice": 0.45,
                    "currentValue": 6.0,
                    "cashPnl": 1.0,
                    "realizedPnl": 0.5,
                    "curPrice": 0.6,
                    "outcome": "Yes",
                }
            ],
        )
    )
    async with DataApiClient() as client:
        leaderboard = await client.get_leaderboard(
            category="OVERALL", time_period="MONTH", limit=10
        )
        positions = await client.get_current_positions(wallet)
    assert leaderboard[0].proxy_wallet == wallet
    assert leaderboard[0].volume == 1000
    assert positions[0].token_id == "token-yes"
    assert positions[0].updated_at <= datetime.now(UTC)


@pytest.mark.asyncio()
@respx.mock
async def test_data_api_recent_trades_normalization() -> None:
    wallet = "0x1111111111111111111111111111111111111111"
    respx.get("https://data-api.polymarket.com/trades").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "proxyWallet": wallet,
                    "asset": "token-yes",
                    "conditionId": "cond-1",
                    "side": "BUY",
                    "size": 10,
                    "price": 0.45,
                    "timestamp": 1710000000,
                    "transactionHash": "0xabc",
                }
            ],
        )
    )
    async with DataApiClient() as client:
        trades = await client.get_recent_trades(limit=10)
    assert trades[0].proxy_wallet == wallet
    assert trades[0].condition_id == "cond-1"
    assert trades[0].token_id == "token-yes"
    assert trades[0].side == "BUY"


@pytest.mark.asyncio()
@respx.mock
async def test_data_api_market_trades_filters_condition() -> None:
    wallet = "0x1111111111111111111111111111111111111111"
    respx.get("https://data-api.polymarket.com/trades").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "proxyWallet": wallet,
                    "asset": "token-yes",
                    "conditionId": "cond-1",
                    "side": "BUY",
                    "size": 10,
                    "price": 0.45,
                    "timestamp": 1710000000,
                    "transactionHash": "0xabc",
                },
                {
                    "proxyWallet": wallet,
                    "asset": "token-other",
                    "conditionId": "cond-2",
                    "side": "BUY",
                    "size": 5,
                    "price": 0.55,
                    "timestamp": 1710000001,
                    "transactionHash": "0xdef",
                },
            ],
        )
    )
    async with DataApiClient() as client:
        trades = await client.get_market_trades("cond-1", limit=10)
    assert len(trades) == 1
    assert trades[0].condition_id == "cond-1"
    assert trades[0].token_id == "token-yes"


@pytest.mark.asyncio()
@respx.mock
async def test_clob_orderbook_normalization() -> None:
    respx.get("https://clob-v2.polymarket.com/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "cond-1",
                "asset_id": "token-yes",
                "timestamp": "1710000000",
                "bids": [{"price": "0.44", "size": "100"}],
                "asks": [{"price": "0.46", "size": "150"}],
                "min_order_size": "1",
                "tick_size": "0.01",
                "neg_risk": False,
                "last_trade_price": "0.45",
            },
        )
    )
    async with ClobPublicClient() as client:
        snapshot = await client.get_orderbook("token-yes")
    assert snapshot.condition_id == "cond-1"
    assert snapshot.midpoint == pytest.approx(0.45)
    assert snapshot.spread == pytest.approx(0.02)
    assert snapshot.liquidity_score == pytest.approx(250.0)


@pytest.mark.asyncio()
@respx.mock
async def test_clob_404_is_not_retried() -> None:
    route = respx.get("https://clob-v2.polymarket.com/book").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    async with ClobPublicClient() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_orderbook("token-missing")
    assert route.call_count == 1

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from pm_alpha_bot.db.models import Market, WalletPosition
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.domain import WalletScoreRecord
from pm_alpha_bot.scoring.clv import approximate_wallet_clv
from pm_alpha_bot.scoring.metrics import (
    clamp,
    max_drawdown_from_pnl_series,
    min_max_normalize,
    profit_concentration,
    safe_ratio,
)
from pm_alpha_bot.time import ensure_utc


def _numeric(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def calculate_wallet_scores(
    repo: Repository,
    category: str,
    as_of: datetime | None = None,
    leaderboard_time_period: str = "MONTH",
) -> list[WalletScoreRecord]:
    """Calculate wallet scores and watchlist eligibility."""
    resolved_as_of = as_of or datetime.now(UTC)
    market_rows = repo.session.execute(select(Market.condition_id, Market.category)).all()
    markets: dict[str, str | None] = {
        condition_id: market_category for condition_id, market_category in market_rows
    }
    raw_rows: list[dict[str, Any]] = []
    leaderboard_rows = repo.latest_leaderboard_snapshots(
        category=category,
        as_of=resolved_as_of,
        time_period=leaderboard_time_period,
    )
    wallet_map = {
        wallet.proxy_wallet: wallet
        for wallet in repo.list_wallets(limit=None, active_only=True)
    }
    if leaderboard_rows:
        leaderboard_wallets = {row.proxy_wallet for row in leaderboard_rows}
        wallet_sources = [
            (
                wallet_map.get(row.proxy_wallet),
                row.raw_json or {},
                row.observed_at,
                row.time_period,
                True,
            )
            for row in leaderboard_rows
        ]
        wallet_sources.extend(
            (
                wallet,
                (wallet.raw_json or {}).get("leaderboard", {}),
                resolved_as_of,
                leaderboard_time_period,
                False,
            )
            for wallet in wallet_map.values()
            if wallet.proxy_wallet not in leaderboard_wallets
            and isinstance((wallet.raw_json or {}).get("liquidity_discovery"), dict)
        )
    else:
        wallet_sources = [
            (
                wallet,
                (wallet.raw_json or {}).get("leaderboard", {}),
                resolved_as_of,
                leaderboard_time_period,
                False,
            )
            for wallet in wallet_map.values()
        ]
    for (
        wallet,
        leaderboard,
        leaderboard_observed_at,
        leaderboard_period,
        pit_leaderboard,
    ) in wallet_sources:
        if wallet is None:
            continue
        unavailable: list[str] = []
        activity = repo.recent_wallet_activity(
            proxy_wallets=[wallet.proxy_wallet],
            since=resolved_as_of - timedelta(days=30),
            until=resolved_as_of,
        )
        positions = list(
            repo.session.scalars(
                select(WalletPosition).where(WalletPosition.proxy_wallet == wallet.proxy_wallet)
            )
        )
        wallet_blob = wallet.raw_json or {}
        detail_snapshot = repo.latest_wallet_detail_snapshot(
            wallet.proxy_wallet,
            as_of=resolved_as_of,
        )
        if detail_snapshot is not None:
            closed_positions = detail_snapshot.closed_positions_json or []
            trades = detail_snapshot.trades_json or []
            total_value_blob = detail_snapshot.total_value_json or {}
            detail_observed_at = detail_snapshot.observed_at
            pit_wallet_details = True
        else:
            closed_positions = wallet_blob.get("closed_positions", [])
            trades = wallet_blob.get("trades", [])
            total_value_blob = wallet_blob.get("total_value", {})
            detail_observed_at = resolved_as_of
            pit_wallet_details = False

        pnl = _numeric(leaderboard.get("pnl"))
        if pnl is None:
            pnl = sum(position.total_pnl or 0.0 for position in positions)
            if pnl == 0 and not positions:
                unavailable.append("pnl")

        base_value = (
            _numeric(total_value_blob.get("totalValue"))
            or _numeric(total_value_blob.get("value"))
            or sum(position.current_value or 0.0 for position in positions)
            or _numeric(leaderboard.get("vol"))
        )
        roi = safe_ratio(pnl, abs(base_value) if base_value is not None else None, default=0.0)
        if base_value is None:
            unavailable.append("roi")

        closed_market_count = len(closed_positions) or len({item.condition_id for item in activity})
        trade_count = len(trades) or len(activity)

        closed_pnls = [
            _numeric(item.get("cashPnl")) or _numeric(item.get("realizedPnl")) or 0.0
            for item in closed_positions
        ]
        win_rate = safe_ratio(
            sum(1 for pnl_item in closed_pnls if pnl_item > 0), len(closed_pnls), default=0.5
        )
        if not closed_pnls:
            unavailable.append("win_rate")

        recent_7d = [
            (item.price or 0.0)
            * (item.size or 0.0)
            * (-1 if (item.side or "").lower().startswith("sell") else 1)
            for item in activity
            if ensure_utc(item.ts) >= resolved_as_of - timedelta(days=7)
        ]
        recent_30d = [
            (item.price or 0.0)
            * (item.size or 0.0)
            * (-1 if (item.side or "").lower().startswith("sell") else 1)
            for item in activity
        ]
        recent_7d_pnl = sum(recent_7d) if recent_7d else None
        recent_30d_pnl = (
            _numeric(leaderboard.get("pnl"))
            if leaderboard
            else (sum(recent_30d) if recent_30d else None)
        )
        if recent_7d_pnl is None:
            unavailable.append("recent_7d_pnl")
        if recent_30d_pnl is None:
            unavailable.append("recent_30d_pnl")

        clv = approximate_wallet_clv(repo, wallet.proxy_wallet, resolved_as_of)
        clv_values = [value for value in clv.values() if value is not None]
        clv_score = sum(clv_values) / len(clv_values) if clv_values else None
        if clv_score is None:
            unavailable.append("clv")

        drawdown = max_drawdown_from_pnl_series(closed_pnls or recent_30d)
        if drawdown is None:
            unavailable.append("max_drawdown")

        concentration = profit_concentration(
            closed_pnls or [position.current_value or 0.0 for position in positions]
        )
        if concentration is None:
            unavailable.append("profit_concentration")

        category_counts = Counter(
            market_category
            for market_category in (markets.get(item.condition_id) for item in activity)
            if market_category
        )
        total_labeled = sum(category_counts.values())
        category_specialization = safe_ratio(
            category_counts.get(category, 0), total_labeled, default=0.5
        )
        if total_labeled == 0:
            unavailable.append("category_specialization")

        recent_token_ids = [item.token_id for item in activity if item.token_id]
        spreads: list[float] = []
        for token_id in set(recent_token_ids):
            snapshot = repo.latest_snapshot_for_token(token_id)
            if snapshot is not None and snapshot.spread is not None:
                spreads.append(snapshot.spread * 10000)
        replicability = (
            None if not spreads else clamp(1 - (sum(spreads) / len(spreads)) / 1000, 0.0, 1.0)
        )
        if replicability is None:
            unavailable.append("replicability")

        raw_rows.append(
            {
                "proxy_wallet": wallet.proxy_wallet,
                "category": category,
                "as_of": resolved_as_of,
                "pnl": pnl,
                "roi": roi,
                "win_rate": win_rate,
                "closed_market_count": closed_market_count,
                "trade_count": trade_count,
                "recent_7d_pnl": recent_7d_pnl,
                "recent_30d_pnl": recent_30d_pnl,
                "clv_1h": clv["1h"],
                "clv_6h": clv["6h"],
                "clv_24h": clv["24h"],
                "clv_score": clv_score,
                "max_drawdown": drawdown,
                "profit_concentration": concentration,
                "category_specialization": category_specialization,
                "replicability": replicability,
                "recent_performance_score": safe_ratio(
                    (recent_7d_pnl or 0.0) + (recent_30d_pnl or 0.0),
                    2.0,
                    default=0.0,
                ),
                "leaderboard_observed_at": leaderboard_observed_at,
                "leaderboard_time_period": leaderboard_period,
                "pit_leaderboard": pit_leaderboard,
                "wallet_details_observed_at": detail_observed_at,
                "pit_wallet_details": pit_wallet_details,
                "unavailable": unavailable,
            }
        )

    pnl_values = [row["pnl"] for row in raw_rows if row["pnl"] is not None]
    roi_values = [row["roi"] for row in raw_rows if row["roi"] is not None]
    recent_values = [
        row["recent_performance_score"]
        for row in raw_rows
        if row["recent_performance_score"] is not None
    ]
    clv_values = [row["clv_score"] for row in raw_rows if row["clv_score"] is not None]
    specialization_values = [
        row["category_specialization"]
        for row in raw_rows
        if row["category_specialization"] is not None
    ]
    drawdown_values = [row["max_drawdown"] for row in raw_rows if row["max_drawdown"] is not None]
    replicability_values = [
        row["replicability"] for row in raw_rows if row["replicability"] is not None
    ]

    records: list[WalletScoreRecord] = []
    for row in raw_rows:
        normalized_pnl = min_max_normalize(row["pnl"], pnl_values)
        normalized_roi = min_max_normalize(row["roi"], roi_values)
        normalized_recent = min_max_normalize(row["recent_performance_score"], recent_values)
        normalized_clv = min_max_normalize(row["clv_score"], clv_values)
        normalized_specialization = min_max_normalize(
            row["category_specialization"], specialization_values
        )
        normalized_drawdown = min_max_normalize(row["max_drawdown"], drawdown_values, inverse=True)
        normalized_replicability = min_max_normalize(row["replicability"], replicability_values)
        score = (
            0.20 * normalized_pnl
            + 0.20 * normalized_roi
            + 0.15 * normalized_recent
            + 0.15 * normalized_clv
            + 0.10 * normalized_specialization
            + 0.10 * normalized_drawdown
            + 0.10 * normalized_replicability
        )
        watchlist_eligible = (
            row["closed_market_count"] >= 30
            and row["trade_count"] >= 50
            and (row["recent_30d_pnl"] or 0.0) >= 0
            and row["roi"] >= 0.03
            and (
                (row["profit_concentration"] if row["profit_concentration"] is not None else 0.0)
                <= 0.40
            )
            and score >= 0.60
        )
        raw_metrics = {
            "recent_7d_pnl": row["recent_7d_pnl"],
            "recent_30d_pnl": row["recent_30d_pnl"],
            "category_specialization_score": normalized_specialization,
            "replicability_score": normalized_replicability,
            "clv_score": normalized_clv,
            "recent_performance_score": normalized_recent,
            "watchlist_eligible": watchlist_eligible,
            "leaderboard_observed_at": row["leaderboard_observed_at"].isoformat(),
            "leaderboard_time_period": row["leaderboard_time_period"],
            "pit_leaderboard": row["pit_leaderboard"],
            "wallet_details_observed_at": row["wallet_details_observed_at"].isoformat(),
            "pit_wallet_details": row["pit_wallet_details"],
            "unavailable_metrics": sorted(set(row["unavailable"])),
        }
        records.append(
            WalletScoreRecord(
                proxy_wallet=row["proxy_wallet"],
                as_of=row["as_of"],
                category=row["category"],
                pnl=row["pnl"],
                roi=row["roi"],
                win_rate=row["win_rate"],
                closed_market_count=row["closed_market_count"],
                trade_count=row["trade_count"],
                clv_1h=row["clv_1h"],
                clv_6h=row["clv_6h"],
                clv_24h=row["clv_24h"],
                max_drawdown=row["max_drawdown"],
                profit_concentration=row["profit_concentration"],
                score=score,
                raw_metrics_json=raw_metrics,
            )
        )
    return records

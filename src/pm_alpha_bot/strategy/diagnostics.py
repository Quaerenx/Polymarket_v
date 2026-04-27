from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.execution.orders import maker_limit_price
from pm_alpha_bot.ingest.orderbook_fetch import is_missing_orderbook_cached
from pm_alpha_bot.market_categories import market_category_matches, normalize_market_category
from pm_alpha_bot.scoring.metrics import clamp
from pm_alpha_bot.strategy.fair_value import compute_fair_probability
from pm_alpha_bot.strategy.filters import market_is_tradeable, snapshot_is_fresh, spread_bps
from pm_alpha_bot.strategy.liquidity import (
    MARKET_CATEGORY_NO_TRADEABLE_ORDERBOOK,
    MarketCategoryLiquiditySummary,
    market_category_has_recent_tradeable_orderbook,
)
from pm_alpha_bot.strategy.signal import (
    _activity_side_is_trade,
    _direction_sign,
    _recency_weight,
    _wallet_is_signal_eligible,
)
from pm_alpha_bot.time import ensure_utc

_OBSERVATION_WATCH_STATE_PREFIX = "signal_observation_watch:"


def observation_watch_state_key(category: str, market_category: str | None = None) -> str:
    """Return the runtime-state key for observation candidate tracking."""
    normalized_market_category = normalize_market_category(market_category)
    if normalized_market_category is None:
        return f"{_OBSERVATION_WATCH_STATE_PREFIX}{category.upper()}"
    return (
        f"{_OBSERVATION_WATCH_STATE_PREFIX}{category.upper()}:"
        f"{normalized_market_category}"
    )


def _snapshot_has_empty_book(snapshot: Any) -> bool:
    raw_json = snapshot.raw_json or {}
    bids = raw_json.get("bids")
    asks = raw_json.get("asks")
    return bool(
        snapshot.best_bid is None
        and snapshot.best_ask is None
        and isinstance(bids, list)
        and isinstance(asks, list)
        and not bids
        and not asks
    )


def _empty_book_reason(snapshot: Any) -> str:
    if _snapshot_has_empty_book(snapshot):
        return (
            "empty_orderbook_with_last_trade"
            if snapshot.last_trade_price is not None
            else "empty_orderbook"
        )
    return ""


def _compute_observation_score(
    *,
    wallet_direction_score: float,
    source_wallet_count: int,
    consensus: int,
    snapshot: Any,
    settings: Settings,
    as_of: datetime,
) -> float:
    age_sec = max(0.0, (as_of - ensure_utc(snapshot.ts)).total_seconds())
    freshness_score = clamp(
        1 - age_sec / max(float(settings.snapshot_stale_after_sec * 5), 1.0),
        0.0,
        1.0,
    )
    direction_score = clamp(wallet_direction_score, 0.0, 1.0)
    consensus_score = clamp(source_wallet_count / max(float(consensus), 1.0), 0.0, 1.0)
    last_trade_score = (
        1.0
        if snapshot.last_trade_price is not None and 0.01 <= snapshot.last_trade_price <= 0.99
        else 0.0
    )
    return round(
        0.45 * direction_score
        + 0.25 * consensus_score
        + 0.20 * freshness_score
        + 0.10 * last_trade_score,
        3,
    )


def analyze_signal_generation(
    repo: Repository,
    *,
    category: str,
    token_id: str | None = None,
    market_category: str | None = None,
    settings: Settings | None = None,
    as_of: datetime | None = None,
    score_thresholds: Sequence[float] = (0.35, 0.30, 0.25),
    consensus_values: Sequence[int] = (3, 2, 1),
    top_candidates: int = 10,
) -> dict[str, Any]:
    """Return a diagnostic report for the signal-generation pipeline."""
    resolved_settings = settings or get_settings()
    resolved_as_of = as_of or datetime.now(UTC)
    resolved_market_category = normalize_market_category(
        market_category or resolved_settings.market_category_filter
    )
    latest_scores = repo.latest_wallet_scores(category=category, as_of=resolved_as_of)
    score_values = sorted((row.score or 0.0) for row in latest_scores)
    rows_by_wallet = {row.proxy_wallet: row for row in latest_scores}
    current = _analyze_candidate_pool(
        repo,
        category=category,
        token_id=token_id,
        market_category=resolved_market_category,
        rows_by_wallet=rows_by_wallet,
        score_threshold=resolved_settings.signal_min_wallet_score,
        consensus=resolved_settings.min_wallet_consensus,
        settings=resolved_settings,
        as_of=resolved_as_of,
        top_candidates=top_candidates,
    )
    sensitivity = [
        _analyze_candidate_pool(
            repo,
            category=category,
            token_id=token_id,
            market_category=resolved_market_category,
            rows_by_wallet=rows_by_wallet,
            score_threshold=score_threshold,
            consensus=consensus,
            settings=resolved_settings,
            as_of=resolved_as_of,
            top_candidates=0,
        )
        for score_threshold in score_thresholds
        for consensus in consensus_values
    ]
    return {
        "category": category,
        "token_id": token_id,
        "market_category_filter": resolved_market_category,
        "as_of": resolved_as_of.isoformat(),
        "wallet_scores_latest": len(latest_scores),
        "score_stats": {
            "min": score_values[0] if score_values else 0.0,
            "p50": score_values[len(score_values) // 2] if score_values else 0.0,
            "max": score_values[-1] if score_values else 0.0,
        },
        "threshold_counts": {
            threshold: sum(1 for score in score_values if score >= threshold)
            for threshold in (0.60, 0.55, 0.50, 0.35, 0.30, 0.25)
        },
        "current": current,
        "sensitivity": sensitivity,
        "settings": {
            "signal_min_wallet_score": resolved_settings.signal_min_wallet_score,
            "signal_activity_lookback_hours": resolved_settings.signal_activity_lookback_hours,
            "market_category_filter": resolved_market_category or "",
            "min_wallet_consensus": resolved_settings.min_wallet_consensus,
            "min_edge_bps": resolved_settings.min_edge_bps,
            "max_spread_bps": resolved_settings.max_spread_bps,
            "observation_promotion_min_streak": resolved_settings.observation_promotion_min_streak,
            "observation_queue_reminder_every_streak": (
                resolved_settings.observation_queue_reminder_every_streak
            ),
        },
    }


def analyze_signal_observations(
    repo: Repository,
    *,
    category: str,
    token_id: str | None = None,
    market_category: str | None = None,
    score_threshold: float,
    consensus: int,
    settings: Settings | None = None,
    as_of: datetime | None = None,
    top_candidates: int = 20,
) -> dict[str, Any]:
    """Return a focused observation report for empty-book candidates."""
    resolved_settings = settings or get_settings()
    resolved_as_of = as_of or datetime.now(UTC)
    resolved_market_category = normalize_market_category(
        market_category or resolved_settings.market_category_filter
    )
    latest_scores = repo.latest_wallet_scores(category=category, as_of=resolved_as_of)
    score_values = sorted((row.score or 0.0) for row in latest_scores)
    rows_by_wallet = {row.proxy_wallet: row for row in latest_scores}
    current = _analyze_candidate_pool(
        repo,
        category=category,
        token_id=token_id,
        market_category=resolved_market_category,
        rows_by_wallet=rows_by_wallet,
        score_threshold=score_threshold,
        consensus=consensus,
        settings=resolved_settings,
        as_of=resolved_as_of,
        top_candidates=top_candidates,
    )
    return {
        "category": category,
        "token_id": token_id,
        "market_category_filter": resolved_market_category,
        "as_of": resolved_as_of.isoformat(),
        "wallet_scores_latest": len(latest_scores),
        "score_stats": {
            "min": score_values[0] if score_values else 0.0,
            "p50": score_values[len(score_values) // 2] if score_values else 0.0,
            "max": score_values[-1] if score_values else 0.0,
        },
        "threshold_counts": {
            threshold: sum(1 for score in score_values if score >= threshold)
            for threshold in (0.60, 0.55, 0.50, 0.35, 0.30, 0.25)
        },
        "current": current,
        "sensitivity": [],
        "settings": {
            "signal_min_wallet_score": score_threshold,
            "signal_activity_lookback_hours": resolved_settings.signal_activity_lookback_hours,
            "market_category_filter": resolved_market_category or "",
            "min_wallet_consensus": consensus,
            "min_edge_bps": resolved_settings.min_edge_bps,
            "max_spread_bps": resolved_settings.max_spread_bps,
            "observation_promotion_min_streak": resolved_settings.observation_promotion_min_streak,
            "observation_queue_reminder_every_streak": (
                resolved_settings.observation_queue_reminder_every_streak
            ),
            "report_mode": "observation",
        },
    }


def _analyze_candidate_pool(
    repo: Repository,
    *,
    category: str,
    token_id: str | None,
    market_category: str | None,
    rows_by_wallet: dict[str, Any],
    score_threshold: float,
    consensus: int,
    settings: Settings,
    as_of: datetime,
    top_candidates: int,
) -> dict[str, Any]:
    eligible = [
        row
        for row in rows_by_wallet.values()
        if _wallet_is_signal_eligible(row, min_score=score_threshold)
    ]
    if not eligible:
        return {
            "category": category,
            "token_id": token_id,
            "market_category_filter": market_category,
            "score_threshold": score_threshold,
            "consensus": consensus,
            "eligible_wallets": 0,
            "activity_rows": 0,
            "tokens_seen": 0,
            "generated": 0,
            "observation_only": 0,
            "promotion_ready": 0,
            "persistent_ready": 0,
            "promotion_queue": 0,
            "reason_counts": {},
            "top_candidates": [],
            "observation_candidates": [],
            "promotion_queue_candidates": [],
        }

    lookback_start = as_of - timedelta(hours=settings.signal_activity_lookback_hours)
    activity = repo.recent_wallet_activity(
        proxy_wallets=[row.proxy_wallet for row in eligible],
        since=lookback_start,
        until=as_of,
        token_ids=[token_id] if token_id else None,
        market_category=market_category,
    )
    per_token_wallet: dict[str, dict[str, Any]] = {}
    for item in activity:
        if item.token_id is None:
            continue
        if not _activity_side_is_trade(item.side):
            continue
        per_token_wallet.setdefault(item.token_id, {})
        current = per_token_wallet[item.token_id].get(item.proxy_wallet)
        if current is None or item.ts > current["ts"]:
            per_token_wallet[item.token_id][item.proxy_wallet] = {
                "ts": item.ts,
                "side": item.side,
                "size": item.size or 0.0,
            }

    reasons: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    generated = 0
    observation_only = 0
    promotion_ready = 0
    liquidity_cache: dict[str, MarketCategoryLiquiditySummary] = {}
    for token_id, wallet_map in per_token_wallet.items():
        outcome = _analyze_token_candidate(
            repo,
            token_id=token_id,
            wallet_map=wallet_map,
            rows_by_wallet=rows_by_wallet,
            market_category=market_category,
            consensus=consensus,
            settings=settings,
            as_of=as_of,
            liquidity_cache=liquidity_cache,
        )
        reasons[outcome["reason"]] += 1
        if outcome["reason"] == "generated":
            generated += 1
        if bool(outcome.get("observation_only", False)):
            observation_only += 1
        if bool(outcome.get("promotion_ready", False)):
            promotion_ready += 1
        if top_candidates > 0:
            candidates.append(outcome)

    top_rows = sorted(
        candidates,
        key=lambda item: (
            item["reason"] != "generated",
            -(item.get("source_wallet_count") or 0),
            -float(item.get("edge_bps") or 0.0),
        ),
    )[:top_candidates]
    observation_rows = sorted(
        (
            item
            for item in candidates
            if bool(item.get("observation_only", False))
            and (item.get("wallet_direction_score") or 0.0) > 0
        ),
        key=lambda item: (
            not bool(item.get("promotion_ready", False)),
            -float(item.get("observation_score") or 0.0),
            -(item.get("source_wallet_count") or 0),
            -float(item.get("last_trade_price") or 0.0),
        ),
    )[:top_candidates]
    pool = {
        "category": category,
        "token_id": token_id,
        "market_category_filter": market_category,
        "score_threshold": score_threshold,
        "consensus": consensus,
        "eligible_wallets": len(eligible),
        "activity_rows": len(activity),
        "tokens_seen": len(per_token_wallet),
        "generated": generated,
        "observation_only": observation_only,
        "promotion_ready": promotion_ready,
        "reason_counts": dict(reasons.most_common()),
        "top_candidates": top_rows,
        "observation_candidates": observation_rows,
        "promotion_queue_candidates": [],
    }
    _attach_observation_watch_state(
        repo,
        category=category,
        market_category=market_category,
        as_of=as_of,
        settings=settings,
        pool=pool,
    )
    return pool


def _attach_observation_watch_state(
    repo: Repository,
    *,
    category: str,
    market_category: str | None,
    as_of: datetime,
    settings: Settings,
    pool: dict[str, Any],
) -> None:
    state = repo.get_runtime_state(observation_watch_state_key(category, market_category))
    tokens = {}
    if state is not None and isinstance(state.state_json, dict):
        raw_tokens = state.state_json.get("tokens", {})
        if isinstance(raw_tokens, dict):
            tokens = raw_tokens
    persistent_ready = 0
    promotion_queue = 0
    for row in pool.get("observation_candidates", []):
        if not isinstance(row, dict):
            continue
        token_state = tokens.get(str(row.get("token_id", "")), {})
        if not isinstance(token_state, dict):
            token_state = {}
        streak = int(token_state.get("observation_streak", 0) or 0)
        if streak <= 0 and bool(row.get("promotion_ready", False)):
            streak = 1
        row["observation_streak"] = streak
        row["first_seen_at"] = token_state.get("first_seen_at") or (
            as_of.isoformat() if streak > 0 else None
        )
        row["last_seen_at"] = token_state.get("last_seen_at") or (
            as_of.isoformat() if streak > 0 else None
        )
        row["persistent_observation"] = streak >= 2 and bool(row.get("promotion_ready", False))
        row["queued_for_promotion"] = (
            streak >= settings.observation_promotion_min_streak
            and bool(row.get("promotion_ready", False))
        )
        if row["persistent_observation"]:
            persistent_ready += 1
        if row["queued_for_promotion"]:
            promotion_queue += 1
    pool["persistent_ready"] = persistent_ready
    pool["promotion_queue"] = promotion_queue
    queue_rows = [
        row
        for row in pool.get("observation_candidates", [])
        if isinstance(row, dict) and bool(row.get("queued_for_promotion", False))
    ]
    pool["promotion_queue_candidates"] = sorted(
        queue_rows,
        key=lambda item: (
            -int(item.get("observation_streak", 0) or 0),
            -float(item.get("observation_score") or 0.0),
            -(item.get("source_wallet_count") or 0),
        ),
    )


def _analyze_token_candidate(
    repo: Repository,
    *,
    token_id: str,
    wallet_map: dict[str, Any],
    rows_by_wallet: dict[str, Any],
    market_category: str | None,
    consensus: int,
    settings: Settings,
    as_of: datetime,
    liquidity_cache: dict[str, MarketCategoryLiquiditySummary] | None = None,
) -> dict[str, Any]:
    snapshot = repo.latest_snapshot_for_token(token_id, as_of=as_of)
    market = repo.get_market_for_token(token_id)
    candidate = {
        "token_id": token_id,
        "condition_id": getattr(market, "condition_id", None)
        or getattr(snapshot, "condition_id", None),
        "question": getattr(market, "question", None),
        "total_wallets": len(wallet_map),
        "source_wallet_count": 0,
        "reason": "",
        "edge_bps": None,
        "spread_bps": None,
        "snapshot_age_sec": None,
        "last_trade_price": getattr(snapshot, "last_trade_price", None),
        "wallet_direction_score": 0.0,
        "observation_only": False,
        "observation_score": None,
        "promotion_ready": False,
        "observation_streak": 0,
        "first_seen_at": None,
        "last_seen_at": None,
        "persistent_observation": False,
        "queued_for_promotion": False,
    }
    if snapshot is None and market is None:
        candidate["reason"] = "missing_snapshot_and_market"
        return candidate
    if market is None:
        candidate["reason"] = "missing_market"
        return candidate
    if not market_category_matches(market.category, market_category):
        candidate["reason"] = "market_category_filtered"
        return candidate
    if not market_category_has_recent_tradeable_orderbook(
        repo,
        market_category=market.category,
        settings=settings,
        as_of=as_of,
        cache=liquidity_cache,
    ):
        candidate["reason"] = MARKET_CATEGORY_NO_TRADEABLE_ORDERBOOK
        return candidate
    if snapshot is None:
        candidate["reason"] = (
            "missing_snapshot_cached_404"
            if is_missing_orderbook_cached(
                repo,
                token_id=token_id,
                settings=settings,
                as_of=as_of,
            )
            else "missing_snapshot"
        )
        return candidate
    if not market_is_tradeable(market, as_of):
        candidate["reason"] = "market_not_tradeable"
        return candidate
    candidate["snapshot_age_sec"] = max(
        0.0, (as_of - ensure_utc(snapshot.ts)).total_seconds()
    )

    wallet_direction_score = 0.0
    source_wallets: list[str] = []
    for wallet_address, metadata in wallet_map.items():
        wallet_score = rows_by_wallet[wallet_address].score or 0.0
        age_hours = max(0.0, (as_of - ensure_utc(metadata["ts"])).total_seconds() / 3600)
        weight = _recency_weight(age_hours)
        size_weight = min(max(metadata["size"], 1.0), 10.0) / 10
        contribution = wallet_score * _direction_sign(metadata["side"]) * weight * size_weight
        wallet_direction_score += contribution
        if contribution > 0:
            source_wallets.append(wallet_address)
    candidate["source_wallet_count"] = len(source_wallets)
    candidate["wallet_direction_score"] = wallet_direction_score
    empty_book_reason = _empty_book_reason(snapshot)
    if empty_book_reason:
        candidate["reason"] = empty_book_reason
        candidate["observation_only"] = snapshot.last_trade_price is not None
        candidate["observation_score"] = _compute_observation_score(
            wallet_direction_score=wallet_direction_score,
            source_wallet_count=len(source_wallets),
            consensus=consensus,
            snapshot=snapshot,
            settings=settings,
            as_of=as_of,
        )
        candidate["promotion_ready"] = bool(
            snapshot.last_trade_price is not None
            and wallet_direction_score > 0
            and len(source_wallets) >= consensus
            and snapshot_is_fresh(snapshot, settings, as_of)
        )
        return candidate
    if len(source_wallets) < consensus:
        candidate["reason"] = "insufficient_wallet_consensus"
        return candidate
    if wallet_direction_score <= 0:
        candidate["reason"] = "non_positive_wallet_direction"
        return candidate

    fair_prob, _ = compute_fair_probability(
        snapshot=snapshot,
        wallet_direction_score=wallet_direction_score,
        repo=repo,
        settings=settings,
        as_of=as_of,
    )
    tick_size = market.min_tick_size or 0.01
    entry_price = maker_limit_price(snapshot, tick_size)
    edge_bps = (fair_prob - entry_price) * 10000
    spread = spread_bps(snapshot)
    candidate["edge_bps"] = edge_bps
    candidate["spread_bps"] = spread
    if spread is None or spread > settings.max_spread_bps:
        candidate["reason"] = "spread_too_wide_or_missing"
        return candidate
    if edge_bps < settings.min_edge_bps:
        candidate["reason"] = "edge_below_threshold"
        return candidate
    if not snapshot_is_fresh(snapshot, settings, as_of):
        candidate["reason"] = "stale_snapshot"
        return candidate
    candidate["reason"] = "generated"
    return candidate

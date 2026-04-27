from __future__ import annotations

from datetime import datetime, timedelta

from pm_alpha_bot.config import Settings
from pm_alpha_bot.db.models import MarketSnapshot
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.scoring.metrics import clamp
from pm_alpha_bot.time import ensure_utc


def compute_momentum_alpha(repo: Repository, token_id: str, as_of: datetime) -> float:
    """Approximate 1h momentum alpha from snapshots."""
    snapshots = repo.snapshots_between(token_id, start=as_of - timedelta(hours=1), end=as_of)
    if len(snapshots) < 2:
        return 0.0
    first = next((snapshot for snapshot in snapshots if snapshot.midpoint is not None), None)
    last = next(
        (snapshot for snapshot in reversed(snapshots) if snapshot.midpoint is not None), None
    )
    if first is None or last is None or first.midpoint is None or last.midpoint is None:
        return 0.0
    price_change = last.midpoint - first.midpoint
    return clamp(price_change * 0.20, -0.03, 0.03)


def compute_liquidity_penalty(snapshot: MarketSnapshot) -> float:
    """Translate liquidity score to a penalty in probability space."""
    liquidity = snapshot.liquidity_score
    if liquidity is None:
        return 0.03
    if liquidity >= 100:
        return 0.0
    return clamp((100 - liquidity) / 100 * 0.03, 0.0, 0.03)


def compute_stale_data_penalty(
    snapshot: MarketSnapshot, settings: Settings, as_of: datetime
) -> float:
    """Return a stale data penalty when the snapshot is too old."""
    age = (as_of - ensure_utc(snapshot.ts)).total_seconds()
    return 0.05 if age > settings.snapshot_stale_after_sec else 0.0


def compute_fair_probability(
    *,
    snapshot: MarketSnapshot,
    wallet_direction_score: float,
    repo: Repository,
    settings: Settings,
    as_of: datetime,
) -> tuple[float, dict[str, float]]:
    """Compute the fair probability and its contributing components."""
    market_midpoint = snapshot.midpoint or 0.5
    wallet_alpha = clamp(wallet_direction_score * 0.03, -0.08, 0.08)
    momentum_alpha = compute_momentum_alpha(repo, snapshot.token_id, as_of)
    liquidity_penalty = compute_liquidity_penalty(snapshot)
    spread_penalty = (snapshot.spread or 0.0) / 2
    stale_data_penalty = compute_stale_data_penalty(snapshot, settings, as_of)
    fair_prob = clamp(
        market_midpoint
        + wallet_alpha
        + momentum_alpha
        - liquidity_penalty
        - spread_penalty
        - stale_data_penalty,
        0.01,
        0.99,
    )
    return fair_prob, {
        "wallet_alpha": wallet_alpha,
        "momentum_alpha": momentum_alpha,
        "liquidity_penalty": liquidity_penalty,
        "spread_penalty": spread_penalty,
        "stale_data_penalty": stale_data_penalty,
    }

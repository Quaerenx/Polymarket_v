from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.db.session import session_scope
from pm_alpha_bot.ingest.leaderboard import ingest_leaderboard
from pm_alpha_bot.ingest.liquidity_wallets import discover_liquid_trade_wallets
from pm_alpha_bot.ingest.markets import ingest_markets
from pm_alpha_bot.ingest.orderbook import ingest_orderbook
from pm_alpha_bot.ingest.wallet_context import (
    enrich_markets_from_recent_wallet_activity,
    enrich_signal_inputs_for_category,
    ingest_orderbooks_for_recent_wallet_tokens,
)
from pm_alpha_bot.ingest.wallets import ingest_wallets
from pm_alpha_bot.notifications import send_alert
from pm_alpha_bot.scoring.wallet_score import calculate_wallet_scores
from pm_alpha_bot.strategy.diagnostics import (
    analyze_signal_observations,
    observation_watch_state_key,
)
from pm_alpha_bot.strategy.signal import generate_signals


def _observation_review_commands(
    *,
    category: str,
    settings: Settings,
    token_id: str | None = None,
    market_category: str | None = None,
) -> list[str]:
    score_threshold = min(settings.signal_min_wallet_score, 0.30)
    token_arg = f" --token-id {token_id}" if token_id else ""
    market_category_arg = f" --market-category {market_category}" if market_category else ""
    return [
        (
            "pm-bot report signal-observations "
            f"--category {category} --score-threshold {score_threshold:.2f} "
            f"--consensus 1 --top-candidates 20{token_arg}{market_category_arg}"
        ),
        (
            "pm-bot report signal-diagnostics "
            f"--category {category} --top-candidates 20{token_arg}{market_category_arg}"
        ),
        "pm-bot report live-events --category signal --top 50",
    ]


def run_refresh_cycle(
    *,
    settings: Settings | None = None,
    leaderboard_category: str = "OVERALL",
    leaderboard_time_period: str = "MONTH",
    market_limit: int = 100,
    wallet_limit: int | None = None,
    orderbook_limit: int | None = None,
    wallet_context_limit: int | None = None,
    active_only: bool = True,
    market_category: str | None = None,
) -> dict[str, Any]:
    """Run one data refresh cycle using isolated DB sessions per stage."""
    resolved = settings or get_settings()
    resolved_wallet_limit = wallet_limit or resolved.tracked_wallet_limit
    resolved_orderbook_limit = orderbook_limit or market_limit
    resolved_wallet_context_limit = (
        wallet_context_limit or resolved.wallet_market_enrichment_limit
    )
    resolved_market_category: str | None = (
        market_category or resolved.market_category_filter or ""
    ).strip()
    if not resolved_market_category:
        resolved_market_category = None
    started_at = datetime.now(UTC)

    markets_count = _ingest_markets_stage(
        resolved,
        limit=market_limit,
        active_only=active_only,
        market_category=resolved_market_category,
    )
    leaderboard_count = _ingest_leaderboard_stage(
        resolved,
        category=leaderboard_category,
        time_period=leaderboard_time_period,
        limit=resolved.leaderboard_limit,
    )
    liquidity_wallet_summary = _liquidity_wallet_discovery_stage(
        resolved,
        market_category=resolved_market_category,
    )
    wallet_summary = _ingest_wallets_stage(
        resolved,
        limit=resolved_wallet_limit,
    )
    wallet_context_summary = _wallet_context_stage(
        resolved,
        wallet_limit=resolved_wallet_limit,
        context_limit=resolved_wallet_context_limit,
        market_category=resolved_market_category,
    )
    orderbook_count = _ingest_orderbook_stage(
        resolved,
        limit=resolved_orderbook_limit,
        market_category=resolved_market_category,
    )
    score_summary = _score_wallets_stage(
        resolved,
        category=leaderboard_category,
        leaderboard_time_period=leaderboard_time_period,
    )
    signal_context_summary = _signal_context_stage(
        resolved,
        category=leaderboard_category,
        context_limit=resolved_wallet_context_limit,
        market_category=resolved_market_category,
    )
    signal_summary = _scan_signals_stage(
        resolved,
        category=leaderboard_category,
        market_category=resolved_market_category,
    )
    observation_summary = _observation_watch_stage(
        resolved,
        category=leaderboard_category,
        market_category=resolved_market_category,
    )
    finished_at = datetime.now(UTC)

    return {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_sec": round((finished_at - started_at).total_seconds(), 3),
        "leaderboard_category": leaderboard_category,
        "leaderboard_time_period": leaderboard_time_period,
        "market_limit": market_limit,
        "wallet_limit": resolved_wallet_limit,
        "orderbook_limit": resolved_orderbook_limit,
        "wallet_context_limit": resolved_wallet_context_limit,
        "active_only": active_only,
        "market_category": resolved_market_category,
        "tradeable_orderbook_focus": resolved.tradeable_orderbook_focus,
        "markets_ingested": markets_count,
        "leaderboard_ingested": leaderboard_count,
        "liquidity_wallet_discovery": liquidity_wallet_summary,
        "wallets_refreshed": wallet_summary,
        "wallet_context": wallet_context_summary,
        "orderbook_snapshots": orderbook_count,
        "wallet_scoring": score_summary,
        "signal_context": signal_context_summary,
        "signal_scan": signal_summary,
        "signal_observations": observation_summary,
    }


def _ingest_markets_stage(
    settings: Settings,
    *,
    limit: int,
    active_only: bool,
    market_category: str | None,
) -> int:
    with session_scope(settings) as session:
        return asyncio.run(
            ingest_markets(
                Repository(session),
                limit=limit,
                active_only=active_only,
                market_category=market_category,
            )
        )


def _ingest_leaderboard_stage(
    settings: Settings,
    *,
    category: str,
    time_period: str,
    limit: int,
) -> int:
    with session_scope(settings) as session:
        return asyncio.run(
            ingest_leaderboard(
                Repository(session),
                category=category,
                time_period=time_period,
                limit=limit,
            )
        )


def _ingest_wallets_stage(
    settings: Settings,
    *,
    limit: int,
) -> dict[str, Any]:
    with session_scope(settings) as session:
        return dict(asyncio.run(ingest_wallets(Repository(session), limit=limit)))


def _liquidity_wallet_discovery_stage(
    settings: Settings,
    *,
    market_category: str | None,
) -> dict[str, int]:
    with session_scope(settings) as session:
        return asyncio.run(
            discover_liquid_trade_wallets(
                Repository(session),
                settings=settings,
                market_category=market_category,
            )
        )


def _ingest_orderbook_stage(
    settings: Settings,
    *,
    limit: int,
    market_category: str | None,
) -> int:
    with session_scope(settings) as session:
        return asyncio.run(
            ingest_orderbook(
                Repository(session),
                limit=limit,
                settings=settings,
                market_category=market_category,
            )
        )


def _wallet_context_stage(
    settings: Settings,
    *,
    wallet_limit: int,
    context_limit: int,
    market_category: str | None,
) -> dict[str, int]:
    with session_scope(settings) as session:
        repo = Repository(session)
        wallets = repo.list_wallets(limit=wallet_limit, active_only=True)
        proxy_wallets = [wallet.proxy_wallet for wallet in wallets]
        now = datetime.now(UTC)
        since = now - timedelta(hours=settings.wallet_market_lookback_hours)
        markets_enriched = asyncio.run(
            enrich_markets_from_recent_wallet_activity(
                repo,
                proxy_wallets=proxy_wallets,
                since=since,
                until=now,
                limit=context_limit,
                market_category=market_category,
            )
        )
        snapshots = asyncio.run(
            ingest_orderbooks_for_recent_wallet_tokens(
                repo,
                proxy_wallets=proxy_wallets,
                since=since,
                until=now,
                limit=context_limit,
                settings=settings,
                market_category=market_category,
            )
        )
        return {
            "markets_enriched": markets_enriched,
            "recent_wallet_orderbooks": snapshots,
        }


def _score_wallets_stage(
    settings: Settings,
    *,
    category: str,
    leaderboard_time_period: str,
) -> dict[str, int]:
    with session_scope(settings) as session:
        repo = Repository(session)
        rows = calculate_wallet_scores(
            repo,
            category=category,
            leaderboard_time_period=leaderboard_time_period,
        )
        stored = repo.save_wallet_scores(rows)
        return {
            "calculated": len(rows),
            "stored": stored,
        }


def _signal_context_stage(
    settings: Settings,
    *,
    category: str,
    context_limit: int,
    market_category: str | None,
) -> dict[str, int]:
    with session_scope(settings) as session:
        repo = Repository(session)
        return asyncio.run(
            enrich_signal_inputs_for_category(
                repo,
                category=category,
                settings=settings,
                as_of=datetime.now(UTC),
                limit=context_limit,
                market_category=market_category,
            )
        )


def _scan_signals_stage(
    settings: Settings,
    *,
    category: str,
    market_category: str | None,
) -> dict[str, int]:
    with session_scope(settings) as session:
        repo = Repository(session)
        rows = generate_signals(repo, category=category, market_category=market_category)
        stored = repo.save_signals(rows)
        return {
            "generated": len(rows),
            "stored": stored,
        }


def _observation_watch_stage(
    settings: Settings,
    *,
    category: str,
    market_category: str | None = None,
) -> dict[str, int]:
    with session_scope(settings) as session:
        repo = Repository(session)
        as_of = datetime.now(UTC)
        report = analyze_signal_observations(
            repo,
            category=category,
            market_category=market_category,
            score_threshold=min(settings.signal_min_wallet_score, 0.30),
            consensus=1,
            settings=settings,
            as_of=as_of,
            top_candidates=50,
        )
        current = report.get("current", {})
        current = current if isinstance(current, dict) else {}
        candidates = current.get("observation_candidates", [])
        candidate_rows = [row for row in candidates if isinstance(row, dict)]
        current_ready = {
            str(row.get("token_id", "")): {
                "token_id": str(row.get("token_id", "")),
                "condition_id": row.get("condition_id"),
                "reason": row.get("reason"),
                "observation_score": row.get("observation_score"),
                "wallet_direction_score": row.get("wallet_direction_score"),
                "source_wallet_count": row.get("source_wallet_count", 0),
                "last_trade_price": row.get("last_trade_price"),
                "observation_streak": int(row.get("observation_streak", 0) or 0),
                "persistent_observation": bool(row.get("persistent_observation", False)),
                "first_seen_at": row.get("first_seen_at"),
                "last_seen_at": row.get("last_seen_at"),
            }
            for row in candidate_rows
            if bool(row.get("promotion_ready", False)) and str(row.get("token_id", ""))
        }
        state_key = observation_watch_state_key(category, market_category)
        previous_state = repo.get_runtime_state(state_key)
        previous_tokens = {}
        if previous_state is not None and isinstance(previous_state.state_json, dict):
            raw_tokens = previous_state.state_json.get("tokens", {})
            if isinstance(raw_tokens, dict):
                previous_tokens = raw_tokens

        newly_ready = 0
        persistent_ready = 0
        queued_for_promotion = 0
        cleared = 0
        tracked_tokens: dict[str, dict[str, Any]] = {}
        reminder_every = max(int(settings.observation_queue_reminder_every_streak), 0)
        promotion_streak = max(int(settings.observation_promotion_min_streak), 1)
        for token_id, row in current_ready.items():
            previous = previous_tokens.get(token_id, {})
            if not isinstance(previous, dict):
                previous = {}
            streak = int(previous.get("observation_streak", 0) or 0) + 1
            queued = streak >= promotion_streak
            review_commands = _observation_review_commands(
                category=category,
                settings=settings,
                token_id=token_id,
                market_category=market_category,
            )
            tracked_tokens[token_id] = {
                **row,
                "observation_streak": streak,
                "first_seen_at": previous.get("first_seen_at") or as_of.isoformat(),
                "last_seen_at": as_of.isoformat(),
                "persistent_observation": streak >= 2,
                "queued_for_promotion": queued,
                "queued_at": previous.get("queued_at") or (as_of.isoformat() if queued else None),
                "review_commands": review_commands,
            }
            if streak == 1:
                newly_ready += 1
                repo.add_runtime_event(
                    category="signal",
                    event_type="signal_observation_ready",
                    level="warning",
                    message=f"Observation candidate {token_id} became promotion-ready.",
                    event_json=tracked_tokens[token_id],
                )
            elif streak == 2:
                persistent_ready += 1
                repo.add_runtime_event(
                    category="signal",
                    event_type="signal_observation_persistent",
                    level="warning",
                    message=f"Observation candidate {token_id} remained promotion-ready.",
                    event_json=tracked_tokens[token_id],
                )
            elif streak > 2:
                persistent_ready += 1
            if streak == promotion_streak:
                queued_for_promotion += 1
                repo.add_runtime_event(
                    category="signal",
                    event_type="signal_observation_queued",
                    level="warning",
                    message=f"Observation candidate {token_id} entered the promotion queue.",
                    event_json=tracked_tokens[token_id],
                )
                send_alert(
                    "signal_observation_queued",
                    f"Observation candidate {token_id} entered the promotion queue.",
                    payload=tracked_tokens[token_id],
                    settings=settings,
                )
            elif streak > promotion_streak:
                queued_for_promotion += 1
                if reminder_every > 0 and (streak - promotion_streak) % reminder_every == 0:
                    repo.add_runtime_event(
                        category="signal",
                        event_type="signal_observation_queue_reminder",
                        level="warning",
                        message=(
                            f"Observation candidate {token_id} is still in the promotion queue "
                            f"(streak={streak})."
                        ),
                        event_json=tracked_tokens[token_id],
                    )
                    send_alert(
                        "signal_observation_queue_reminder",
                        (
                            f"Observation candidate {token_id} remains in the promotion queue "
                            f"(streak={streak})."
                        ),
                        payload=tracked_tokens[token_id],
                        settings=settings,
                    )

        for token_id, previous in previous_tokens.items():
            if token_id in tracked_tokens:
                continue
            cleared += 1
            payload = dict(previous) if isinstance(previous, dict) else {"token_id": token_id}
            payload["token_id"] = token_id
            payload["cleared_at"] = as_of.isoformat()
            payload.setdefault(
                "review_commands",
                _observation_review_commands(
                    category=category,
                    settings=settings,
                    token_id=token_id,
                    market_category=market_category,
                ),
            )
            repo.add_runtime_event(
                category="signal",
                event_type="signal_observation_cleared",
                level="info",
                message=f"Observation candidate {token_id} cleared.",
                event_json=payload,
            )
            if bool(payload.get("queued_for_promotion", False)):
                send_alert(
                    "signal_observation_cleared",
                    f"Observation candidate {token_id} left the promotion queue.",
                    payload=payload,
                    settings=settings,
                )

        repo.upsert_runtime_state(
            state_key,
            {
                "category": category,
                "updated_at": as_of.isoformat(),
                "tokens": tracked_tokens,
            },
        )
        return {
            "observation_only": int(current.get("observation_only", 0) or 0),
            "promotion_ready": len(tracked_tokens),
            "newly_ready": newly_ready,
            "persistent_ready": persistent_ready,
            "promotion_queue": queued_for_promotion,
            "cleared": cleared,
        }

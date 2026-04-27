from __future__ import annotations

from collections.abc import Sequence

from rich.table import Table

from pm_alpha_bot.domain import BacktestReport


def _to_float(value: object) -> float:
    """Convert report values to float with a conservative default."""
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def build_wallet_table(rows: Sequence[object]) -> Table:
    """Render wallet score rows."""
    table = Table(title="Wallet Scores")
    table.add_column("Wallet")
    table.add_column("Category")
    table.add_column("Score")
    table.add_column("ROI")
    table.add_column("PnL")
    for row in rows:
        table.add_row(
            getattr(row, "proxy_wallet", ""),
            getattr(row, "category", ""),
            f"{(getattr(row, 'score', 0.0) or 0.0):.3f}",
            f"{(getattr(row, 'roi', 0.0) or 0.0):.3f}",
            f"{(getattr(row, 'pnl', 0.0) or 0.0):.2f}",
        )
    return table


def build_signal_table(rows: Sequence[object]) -> Table:
    """Render signal rows."""
    table = Table(title="Signals")
    table.add_column("Token")
    table.add_column("Direction")
    table.add_column("Edge bps")
    table.add_column("Fair Prob")
    table.add_column("Entry")
    for row in rows:
        table.add_row(
            getattr(row, "token_id", ""),
            getattr(row, "direction", ""),
            f"{(getattr(row, 'edge_bps', 0.0) or 0.0):.1f}",
            f"{(getattr(row, 'fair_prob', 0.0) or 0.0):.3f}",
            f"{(getattr(row, 'effective_entry_price', 0.0) or 0.0):.3f}",
        )
    return table


def build_paper_table(report: dict[str, object]) -> Table:
    """Render a summary of paper trading state."""
    table = Table(title="Paper Trading")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("fills_count", str(report.get("fills_count", 0)))
    table.add_row("realized_pnl", f"{_to_float(report.get('realized_pnl', 0.0)):.2f}")
    table.add_row(
        "unrealized_pnl",
        f"{_to_float(report.get('unrealized_pnl', 0.0)):.2f}",
    )
    positions = report.get("positions", [])
    table.add_row("positions", str(len(positions) if isinstance(positions, list) else 0))
    return table


def build_live_table(report: dict[str, object]) -> Table:
    """Render a summary of live trading state reconstructed from fills."""
    table = Table(title="Live Trading")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("orders_count", str(report.get("orders_count", 0)))
    table.add_row("open_orders_count", str(report.get("open_orders_count", 0)))
    table.add_row("fills_count", str(report.get("fills_count", 0)))
    table.add_row("realized_pnl", f"{_to_float(report.get('realized_pnl', 0.0)):.2f}")
    table.add_row(
        "daily_realized_pnl",
        f"{_to_float(report.get('daily_realized_pnl', 0.0)):.2f}",
    )
    table.add_row("fees_total", f"{_to_float(report.get('fees_total', 0.0)):.2f}")
    table.add_row(
        "net_realized_pnl",
        f"{_to_float(report.get('net_realized_pnl', 0.0)):.2f}",
    )
    table.add_row(
        "unrealized_pnl",
        f"{_to_float(report.get('unrealized_pnl', 0.0)):.2f}",
    )
    table.add_row("equity", f"{_to_float(report.get('equity', 0.0)):.2f}")
    positions = report.get("positions", [])
    table.add_row("positions", str(len(positions) if isinstance(positions, list) else 0))
    table.add_row("market_exposure_pct", str(report.get("market_exposure_pct", {})))
    table.add_row("condition_exposure_pct", str(report.get("condition_exposure_pct", {})))
    table.add_row("event_exposure_pct", str(report.get("event_exposure_pct", {})))
    table.add_row("category_exposure_pct", str(report.get("category_exposure_pct", {})))
    return table


def build_live_health_table(report: dict[str, object]) -> Table:
    """Render persisted live supervisor and kill-switch state."""
    table = Table(title="Live Health")
    table.add_column("Metric")
    table.add_column("Value")
    supervisor = report.get("supervisor")
    kill_switch = report.get("kill_switch")
    supervisor_state = supervisor if isinstance(supervisor, dict) else {}
    kill_switch_state = kill_switch if isinstance(kill_switch, dict) else {}
    table.add_row("supervisor_status", str(supervisor_state.get("status", "unknown")))
    table.add_row("last_heartbeat_id", str(supervisor_state.get("heartbeat_id", "")))
    table.add_row("last_iteration_at", str(supervisor_state.get("last_iteration_at", "")))
    table.add_row("last_success_at", str(supervisor_state.get("last_success_at", "")))
    table.add_row("last_error", str(supervisor_state.get("last_error", "")))
    table.add_row(
        "consecutive_errors",
        str(supervisor_state.get("consecutive_errors", 0)),
    )
    table.add_row("account_ok", str(supervisor_state.get("account_ok", "")))
    table.add_row("account_reason", str(supervisor_state.get("account_reason", "")))
    table.add_row("kill_switch_tripped", str(kill_switch_state.get("tripped", False)))
    table.add_row("kill_switch_reason", str(kill_switch_state.get("reason", "")))
    table.add_row("kill_switch_tripped_at", str(kill_switch_state.get("tripped_at", "")))
    table.add_row("kill_switch_reset_at", str(kill_switch_state.get("reset_at", "")))
    return table


def build_live_events_table(rows: Sequence[object]) -> Table:
    """Render recent live/runtime events."""
    table = Table(title="Live Events")
    table.add_column("TS")
    table.add_column("Level")
    table.add_column("Type")
    table.add_column("Message")
    for row in rows:
        table.add_row(
            str(getattr(row, "ts", "")),
            str(getattr(row, "level", "")),
            str(getattr(row, "event_type", "")),
            str(getattr(row, "message", "") or ""),
        )
    return table


def build_signal_diagnostics_summary_table(report: dict[str, object]) -> Table:
    """Render top-level signal diagnostics metrics."""
    table = Table(title="Signal Diagnostics Summary")
    table.add_column("Metric")
    table.add_column("Value")
    score_stats = report.get("score_stats", {})
    score_stats = score_stats if isinstance(score_stats, dict) else {}
    current = report.get("current", {})
    current = current if isinstance(current, dict) else {}
    settings = report.get("settings", {})
    settings = settings if isinstance(settings, dict) else {}
    threshold_counts = report.get("threshold_counts", {})
    threshold_counts = threshold_counts if isinstance(threshold_counts, dict) else {}
    table.add_row("category", str(report.get("category", "")))
    if report.get("token_id"):
        table.add_row("token_id", str(report.get("token_id", "")))
    if report.get("market_category_filter"):
        table.add_row("market_category_filter", str(report.get("market_category_filter", "")))
    table.add_row("as_of", str(report.get("as_of", "")))
    table.add_row("wallet_scores_latest", str(report.get("wallet_scores_latest", 0)))
    table.add_row("score_min", f"{_to_float(score_stats.get('min')):.3f}")
    table.add_row("score_p50", f"{_to_float(score_stats.get('p50')):.3f}")
    table.add_row("score_max", f"{_to_float(score_stats.get('max')):.3f}")
    table.add_row("gte_0.35", str(threshold_counts.get(0.35, 0)))
    table.add_row("gte_0.30", str(threshold_counts.get(0.30, 0)))
    table.add_row("gte_0.25", str(threshold_counts.get(0.25, 0)))
    table.add_row("current_threshold", str(settings.get("signal_min_wallet_score", "")))
    table.add_row("current_consensus", str(settings.get("min_wallet_consensus", "")))
    table.add_row(
        "promotion_min_streak",
        str(settings.get("observation_promotion_min_streak", "")),
    )
    table.add_row(
        "queue_reminder_every_streak",
        str(settings.get("observation_queue_reminder_every_streak", "")),
    )
    table.add_row("eligible_wallets", str(current.get("eligible_wallets", 0)))
    table.add_row("activity_rows", str(current.get("activity_rows", 0)))
    table.add_row("tokens_seen", str(current.get("tokens_seen", 0)))
    table.add_row("observation_only", str(current.get("observation_only", 0)))
    table.add_row("promotion_ready", str(current.get("promotion_ready", 0)))
    table.add_row("persistent_ready", str(current.get("persistent_ready", 0)))
    table.add_row("promotion_queue", str(current.get("promotion_queue", 0)))
    table.add_row("generated", str(current.get("generated", 0)))
    return table


def build_signal_diagnostics_reasons_table(report: dict[str, object]) -> Table:
    """Render current diagnostic reason counts."""
    table = Table(title="Signal Diagnostics Reasons")
    table.add_column("Reason")
    table.add_column("Count")
    current = report.get("current", {})
    reason_counts: object = getattr(current, "get", lambda *_: {})("reason_counts")
    items: list[tuple[object, object]] = []
    if isinstance(reason_counts, dict):
        items = list(reason_counts.items())
    for reason, count in items:
        table.add_row(str(reason), str(count))
    return table


def build_signal_diagnostics_candidates_table(report: dict[str, object]) -> Table:
    """Render top candidate tokens and their blocking reason."""
    table = Table(title="Signal Diagnostics Candidates")
    table.add_column("Token")
    table.add_column("Reason")
    table.add_column("Wallets")
    table.add_column("Last trade")
    table.add_column("Edge bps")
    table.add_column("Spread bps")
    table.add_column("Age sec")
    current = report.get("current", {})
    rows: object = getattr(current, "get", lambda *_: [])("top_candidates")
    candidate_rows: list[dict[str, object]] = []
    if isinstance(rows, list):
        candidate_rows = [row for row in rows if isinstance(row, dict)]
    for row in candidate_rows:
        edge_bps = row.get("edge_bps")
        spread = row.get("spread_bps")
        age_sec = row.get("snapshot_age_sec")
        last_trade = row.get("last_trade_price")
        table.add_row(
            str(row.get("token_id", "")),
            str(row.get("reason", "")),
            str(row.get("source_wallet_count", 0)),
            "" if last_trade is None else f"{_to_float(last_trade):.3f}",
            "" if edge_bps is None else f"{_to_float(edge_bps):.1f}",
            "" if spread is None else f"{_to_float(spread):.1f}",
            "" if age_sec is None else f"{_to_float(age_sec):.1f}",
        )
    return table


def build_signal_observation_table(report: dict[str, object]) -> Table:
    """Render observation-only candidates with last-trade context."""
    return _build_signal_candidate_table(
        title="Signal Observation Candidates",
        rows=_extract_dict_rows(report, "observation_candidates"),
    )


def build_signal_promotion_queue_table(report: dict[str, object]) -> Table:
    """Render persistent observation candidates that entered the promotion queue."""
    return _build_signal_candidate_table(
        title="Signal Promotion Queue",
        rows=_extract_dict_rows(report, "promotion_queue_candidates"),
    )


def _extract_dict_rows(report: dict[str, object], key: str) -> list[dict[str, object]]:
    current = report.get("current", {})
    rows: object = getattr(current, "get", lambda *_: [])(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _build_signal_candidate_table(*, title: str, rows: list[dict[str, object]]) -> Table:
    """Render observation-derived candidate rows with streak context."""
    table = Table(title=title)
    table.add_column("Token")
    table.add_column("Reason")
    table.add_column("Wallets")
    table.add_column("Streak")
    table.add_column("Score")
    table.add_column("Ready")
    table.add_column("Direction")
    table.add_column("Last trade")
    table.add_column("Age sec")
    for row in rows:
        wallet_direction_score = _to_float(row.get("wallet_direction_score"))
        observation_score = row.get("observation_score")
        age_sec = row.get("snapshot_age_sec")
        last_trade = row.get("last_trade_price")
        table.add_row(
            str(row.get("token_id", "")),
            str(row.get("reason", "")),
            str(row.get("source_wallet_count", 0)),
            str(row.get("observation_streak", 0)),
            "" if observation_score is None else f"{_to_float(observation_score):.3f}",
            "yes" if bool(row.get("promotion_ready", False)) else "no",
            f"{wallet_direction_score:.3f}",
            "" if last_trade is None else f"{_to_float(last_trade):.3f}",
            "" if age_sec is None else f"{_to_float(age_sec):.1f}",
        )
    return table


def build_signal_diagnostics_sensitivity_table(report: dict[str, object]) -> Table:
    """Render signal-generation sensitivity across thresholds and consensus."""
    table = Table(title="Signal Diagnostics Sensitivity")
    table.add_column("Min Score")
    table.add_column("Consensus")
    table.add_column("Eligible")
    table.add_column("Tokens")
    table.add_column("Generated")
    table.add_column("Top Reason")
    rows = report.get("sensitivity", [])
    for row in rows if isinstance(rows, list) else []:
        reason_counts = row.get("reason_counts", {})
        top_reason = ""
        if isinstance(reason_counts, dict) and reason_counts:
            top_reason = next(iter(reason_counts))
        table.add_row(
            f"{_to_float(row.get('score_threshold')):.2f}",
            str(row.get("consensus", 0)),
            str(row.get("eligible_wallets", 0)),
            str(row.get("tokens_seen", 0)),
            str(row.get("generated", 0)),
            top_reason,
        )
    return table


def build_backtest_table(report: BacktestReport) -> Table:
    """Render a compact replay backtest report."""
    table = Table(title=f"Backtest Replay ({report.fill_model})")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("fill_model", report.fill_model)
    table.add_row("total_trades", str(report.total_trades))
    table.add_row("win_rate", f"{report.win_rate:.3f}")
    table.add_row("average_edge_bps", f"{report.average_edge_bps:.1f}")
    table.add_row("realized_pnl", f"{report.realized_pnl:.2f}")
    table.add_row("fees_total", f"{report.fees_total:.2f}")
    table.add_row("unrealized_pnl", f"{report.unrealized_pnl:.2f}")
    table.add_row("max_drawdown", f"{report.max_drawdown:.3f}")
    table.add_row("exposure_by_category", str(report.exposure_by_category))
    table.add_row("top_winning_markets", str(report.top_winning_markets))
    table.add_row("top_losing_markets", str(report.top_losing_markets))
    table.add_row("source_wallet_attribution", str(report.source_wallet_attribution))
    return table

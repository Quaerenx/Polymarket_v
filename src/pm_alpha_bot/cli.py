from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import typer
from alembic import command
from alembic.config import Config
from rich.console import Console

from pm_alpha_bot.backtest.replay import ReplayFillModel, replay_backtest
from pm_alpha_bot.config import get_settings
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.db.session import create_all_tables, session_scope
from pm_alpha_bot.execution.live import (
    cancel_live_order,
    execute_live_once,
    reset_live_kill_switch,
    run_live_supervisor,
    sync_live_orders,
)
from pm_alpha_bot.execution.paper import PaperBroker
from pm_alpha_bot.ingest.leaderboard import ingest_leaderboard
from pm_alpha_bot.ingest.markets import ingest_markets
from pm_alpha_bot.ingest.orderbook import ingest_orderbook
from pm_alpha_bot.ingest.stream_market import stream_market
from pm_alpha_bot.ingest.wallets import ingest_wallets
from pm_alpha_bot.logging import configure_logging
from pm_alpha_bot.ops.healthcheck import evaluate_healthcheck
from pm_alpha_bot.ops.refresh import run_refresh_cycle
from pm_alpha_bot.reporting.report import (
    build_backtest_table,
    build_live_events_table,
    build_live_health_table,
    build_live_table,
    build_paper_table,
    build_signal_diagnostics_candidates_table,
    build_signal_diagnostics_reasons_table,
    build_signal_diagnostics_sensitivity_table,
    build_signal_diagnostics_summary_table,
    build_signal_observation_table,
    build_signal_promotion_queue_table,
    build_signal_table,
    build_wallet_table,
)
from pm_alpha_bot.scoring.wallet_score import calculate_wallet_scores
from pm_alpha_bot.strategy.diagnostics import (
    analyze_signal_generation,
    analyze_signal_observations,
)
from pm_alpha_bot.strategy.signal import generate_signals
from pm_alpha_bot.time import parse_cli_datetime
from pm_alpha_bot.web.console import serve_console

app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(no_args_is_help=True)
ingest_app = typer.Typer(no_args_is_help=True)
score_app = typer.Typer(no_args_is_help=True)
scan_app = typer.Typer(no_args_is_help=True)
trade_app = typer.Typer(no_args_is_help=True)
report_app = typer.Typer(no_args_is_help=True)
stream_app = typer.Typer(no_args_is_help=True)
backtest_app = typer.Typer(no_args_is_help=True)
ops_app = typer.Typer(no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(ingest_app, name="ingest")
app.add_typer(score_app, name="score")
app.add_typer(scan_app, name="scan")
app.add_typer(trade_app, name="trade")
app.add_typer(report_app, name="report")
app.add_typer(stream_app, name="stream")
app.add_typer(backtest_app, name="backtest")
app.add_typer(ops_app, name="ops")
console = Console()


@contextmanager
def _repo_scope() -> Iterator[Repository]:
    with session_scope() as session:
        yield Repository(session)


@app.callback()
def main() -> None:
    """Polymarket wallet-weighted EV bot."""
    configure_logging(get_settings().log_level)


@db_app.command("init")
def db_init() -> None:
    """Create all tables directly from metadata."""
    create_all_tables()
    console.print("Database tables created.")


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply Alembic migrations."""
    config = Config(str(Path("alembic.ini").resolve()))
    command.upgrade(config, "head")
    console.print("Database migrated to head.")


@db_app.command("prune-runtime-events")
def db_prune_runtime_events_cmd(
    older_than_days: int | None = typer.Option(
        None,
        "--older-than-days",
        min=1,
        help="Delete events older than this many days. Defaults to RUNTIME_EVENT_RETENTION_DAYS.",
    ),
    category: str | None = typer.Option(
        "live",
        "--category",
        help="Filter by runtime event category. Pass an empty string to target all categories.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply/--dry-run",
        help="Actually delete rows. Dry-run is the default.",
    ),
) -> None:
    """Prune old runtime events with a dry-run default."""
    settings = get_settings()
    retention_days = older_than_days or settings.runtime_event_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    resolved_category = category or None
    with session_scope(settings) as session:
        repo = Repository(session)
        count = repo.prune_runtime_events(
            older_than=cutoff,
            category=resolved_category,
            dry_run=not apply,
        )
    verb = "Pruned" if apply else "Would prune"
    category_label = resolved_category or "all"
    console.print(
        f"{verb} {count} runtime events older than {cutoff.isoformat()} "
        f"(category={category_label})."
    )


@ops_app.command("healthcheck")
def ops_healthcheck_cmd(
    require_live_supervisor: bool = typer.Option(
        False,
        "--require-live-supervisor/--db-only",
        help="Require a fresh persisted live supervisor state instead of checking DB only.",
    ),
    max_supervisor_age_sec: float | None = typer.Option(
        None,
        "--max-supervisor-age-sec",
        min=1.0,
        help="Maximum allowed supervisor age before the healthcheck fails.",
    ),
    fail_on_kill_switch: bool = typer.Option(
        False,
        "--fail-on-kill-switch/--allow-kill-switch",
        help="Fail when the persisted live kill switch is active.",
    ),
    fail_on_supervisor_error: bool = typer.Option(
        False,
        "--fail-on-supervisor-error/--allow-supervisor-error",
        help="Fail when the persisted live supervisor status is not 'ok'.",
    ),
) -> None:
    """Run an operational healthcheck for DB and optional live supervisor state."""
    settings = get_settings()
    try:
        with session_scope(settings) as session:
            report = evaluate_healthcheck(
                Repository(session),
                settings=settings,
                require_live_supervisor=require_live_supervisor,
                max_supervisor_age_sec=max_supervisor_age_sec,
                fail_on_kill_switch=fail_on_kill_switch,
                fail_on_supervisor_error=fail_on_supervisor_error,
            )
    except Exception as exc:
        console.print(
            {
                "ok": False,
                "db_ok": False,
                "error": str(exc),
                "require_live_supervisor": require_live_supervisor,
            }
        )
        raise typer.Exit(code=1) from exc
    console.print(report)
    if not bool(report.get("ok", False)):
        raise typer.Exit(code=1)


@ops_app.command("serve-console")
def ops_serve_console_cmd(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. Loopback is the safe default.",
    ),
    port: int = typer.Option(
        8787,
        "--port",
        min=1,
        max=65535,
        help="Bind port for the web console.",
    ),
    enable_actions: bool = typer.Option(
        False,
        "--enable-actions/--read-only",
        help="Allow limited management actions. Read-only is the default.",
    ),
    allow_remote_actions: bool = typer.Option(
        False,
        "--allow-remote-actions",
        help="Permit actions on a non-loopback bind host after explicit acknowledgement.",
    ),
) -> None:
    """Serve a harness-style local monitoring and management console."""
    settings = get_settings()
    console.print(
        {
            "host": host,
            "port": port,
            "enable_actions": enable_actions,
            "live_trading_enabled": settings.enable_live_trading,
        }
    )
    try:
        serve_console(
            settings=settings,
            host=host,
            port=port,
            enable_actions=enable_actions,
            allow_remote_actions=allow_remote_actions,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@ops_app.command("refresh-data")
def ops_refresh_data_cmd(
    leaderboard_category: str = typer.Option(
        "OVERALL",
        "--leaderboard-category",
        help="Leaderboard category used for wallet refresh, scoring, and signals.",
    ),
    leaderboard_time_period: str = typer.Option(
        "MONTH",
        "--leaderboard-time-period",
        help="Leaderboard time period used during refresh.",
    ),
    market_limit: int = typer.Option(
        100,
        "--market-limit",
        min=1,
        help="Number of active markets to refresh.",
    ),
    wallet_limit: int | None = typer.Option(
        None,
        "--wallet-limit",
        min=1,
        help="Wallet refresh limit. Defaults to TRACKED_WALLET_LIMIT.",
    ),
    orderbook_limit: int | None = typer.Option(
        None,
        "--orderbook-limit",
        min=1,
        help="Orderbook snapshot refresh limit. Defaults to market limit.",
    ),
    wallet_context_limit: int | None = typer.Option(
        None,
        "--wallet-context-limit",
        min=1,
        help=(
            "Recent-wallet market/orderbook enrichment limit. "
            "Defaults to WALLET_MARKET_ENRICHMENT_LIMIT."
        ),
    ),
    active_only: bool = typer.Option(
        True,
        "--active-only/--all-markets",
        help="Refresh only active markets by default.",
    ),
    market_category: str | None = typer.Option(
        None,
        "--market-category",
        help="Restrict refresh, snapshots, and signals to a specific market category.",
    ),
) -> None:
    """Run one end-to-end data refresh, scoring, and signal generation cycle."""
    summary = run_refresh_cycle(
        settings=get_settings(),
        leaderboard_category=leaderboard_category,
        leaderboard_time_period=leaderboard_time_period,
        market_limit=market_limit,
        wallet_limit=wallet_limit,
        orderbook_limit=orderbook_limit,
        wallet_context_limit=wallet_context_limit,
        active_only=active_only,
        market_category=market_category,
    )
    console.print(summary)


@ingest_app.command("markets")
def ingest_markets_cmd(limit: int = 100, active_only: bool = True) -> None:
    """Ingest active market metadata."""
    with session_scope() as session:
        count = asyncio.run(
            ingest_markets(Repository(session), limit=limit, active_only=active_only)
        )
        console.print(f"Stored {count} markets.")


@ingest_app.command("leaderboard")
def ingest_leaderboard_cmd(
    category: str = "OVERALL",
    time_period: str = "MONTH",
    limit: int = 50,
) -> None:
    """Ingest leaderboard wallet data."""
    with session_scope() as session:
        count = asyncio.run(
            ingest_leaderboard(
                Repository(session),
                category=category,
                time_period=time_period,
                limit=limit,
            )
        )
        console.print(f"Stored {count} leaderboard wallets.")


@ingest_app.command("wallets")
def ingest_wallets_cmd(limit: int = 100) -> None:
    """Refresh wallet positions and activity."""
    with session_scope() as session:
        summary = asyncio.run(ingest_wallets(Repository(session), limit=limit))
        console.print(summary)


@ingest_app.command("orderbook")
def ingest_orderbook_cmd(
    limit: int = 100,
    market_category: str | None = typer.Option(
        None,
        "--market-category",
        help="Restrict orderbook refresh to a specific market category.",
    ),
) -> None:
    """Refresh orderbook snapshots."""
    with session_scope() as session:
        count = asyncio.run(
            ingest_orderbook(
                Repository(session),
                limit=limit,
                market_category=market_category,
            )
        )
        console.print(f"Stored {count} orderbook snapshots.")


@score_app.command("wallets")
def score_wallets_cmd(
    category: str = "OVERALL",
    leaderboard_time_period: str = typer.Option(
        "MONTH",
        "--leaderboard-time-period",
        help="Leaderboard time period used for point-in-time wallet universe selection.",
    ),
) -> None:
    """Calculate and persist wallet scores."""
    with session_scope() as session:
        repo = Repository(session)
        rows = calculate_wallet_scores(
            repo,
            category=category,
            leaderboard_time_period=leaderboard_time_period,
        )
        count = repo.save_wallet_scores(rows)
        console.print(f"Stored {count} wallet scores.")
        console.print(build_wallet_table(rows[:20]))


@scan_app.command("signals")
def scan_signals_cmd(
    category: str = "OVERALL",
    market_category: str | None = typer.Option(
        None,
        "--market-category",
        help="Restrict signal generation to a specific market category.",
    ),
) -> None:
    """Generate and persist market signals."""
    with session_scope() as session:
        repo = Repository(session)
        rows = generate_signals(repo, category=category, market_category=market_category)
        count = repo.save_signals(rows)
        console.print(f"Stored {count} signals.")
        console.print(build_signal_table(rows[:20]))


@trade_app.command("paper")
def trade_paper_cmd(once: bool = False, loop: bool = False, interval: int = 30) -> None:
    """Execute paper trading."""
    if not once and not loop:
        once = True
    with session_scope() as session:
        broker = PaperBroker(Repository(session))
        if once:
            console.print(broker.run_once())
        elif loop:
            broker.run_loop(interval=interval)


@trade_app.command("live")
def trade_live_cmd(
    live: bool = typer.Option(False, "--live", help="Explicit live trading acknowledgement."),
) -> None:
    """Submit one live order after all safety checks pass."""
    with session_scope() as session:
        result = execute_live_once(Repository(session), explicit_live=live)
        console.print(result.model_dump(mode="json"))


@trade_app.command("sync-live")
def trade_sync_live_cmd(limit: int = 50) -> None:
    """Sync remote live-order state and fills into the local database."""
    with session_scope() as session:
        summary = sync_live_orders(Repository(session), limit=limit)
        console.print(summary)


@trade_app.command("live-loop")
def trade_live_loop_cmd(
    live: bool = typer.Option(False, "--live", help="Explicit live trading acknowledgement."),
    interval: float | None = typer.Option(
        None,
        "--interval",
        help="Heartbeat and sync interval in seconds.",
    ),
    iterations: int | None = typer.Option(
        None,
        "--iterations",
        help="Stop after this many supervisor iterations.",
    ),
    sync_limit: int | None = typer.Option(
        None,
        "--sync-limit",
        help="Maximum local live orders to inspect per iteration when open-order data is absent.",
    ),
    submit_new_orders: bool = typer.Option(
        False,
        "--submit-new-orders/--no-submit-new-orders",
        help="When idle, attempt to place a new live order from the freshest signal.",
    ),
) -> None:
    """Run the live heartbeat and reconciliation supervisor."""
    summaries = run_live_supervisor(
        _repo_scope,
        explicit_live=live,
        interval_sec=interval,
        iterations=iterations,
        submit_new_orders=submit_new_orders,
        sync_limit=sync_limit,
    )
    for summary in summaries:
        console.print(summary)


@trade_app.command("cancel-live")
def trade_cancel_live_cmd(
    order_id: str = typer.Option(..., "--order-id", help="Exchange order ID."),
    live: bool = typer.Option(False, "--live", help="Explicit live trading acknowledgement."),
) -> None:
    """Cancel a live order on Polymarket and mirror the result locally."""
    with session_scope() as session:
        summary = cancel_live_order(
            Repository(session),
            external_order_id=order_id,
            explicit_live=live,
        )
        console.print(summary)


@trade_app.command("reset-live-kill-switch")
def trade_reset_live_kill_switch_cmd(
    acknowledge: bool = typer.Option(
        False,
        "--acknowledge",
        help="Confirm that an operator reviewed the kill-switch cause before reset.",
    ),
    reason: str = typer.Option(
        "operator_acknowledged_reset",
        "--reason",
        help="Short audit reason stored with the reset event.",
    ),
    allow_active_orders: bool = typer.Option(
        False,
        "--allow-active-orders",
        help="Allow reset even if local active live orders still exist.",
    ),
) -> None:
    """Reset the persisted live kill switch state."""
    with session_scope() as session:
        summary = reset_live_kill_switch(
            Repository(session),
            acknowledge=acknowledge,
            reset_reason=reason,
            allow_active_orders=allow_active_orders,
        )
        console.print(summary)


@report_app.command("wallets")
def report_wallets_cmd(top: int = 20) -> None:
    """Show top wallet scores."""
    with session_scope() as session:
        rows = Repository(session).wallet_summary_rows(top)
        console.print(build_wallet_table(rows))


@report_app.command("signals")
def report_signals_cmd(top: int = 20) -> None:
    """Show top signal rows."""
    with session_scope() as session:
        rows = Repository(session).signal_summary_rows(top)
        console.print(build_signal_table(rows))


@report_app.command("signal-diagnostics")
def report_signal_diagnostics_cmd(
    category: str = "OVERALL",
    token_id: str | None = typer.Option(
        None,
        "--token-id",
        help="Restrict diagnostics to a single token ID.",
    ),
    market_category: str | None = typer.Option(
        None,
        "--market-category",
        help="Restrict diagnostics to a specific market category.",
    ),
    top_candidates: int = typer.Option(
        10,
        "--top-candidates",
        min=1,
        help="Number of blocked/generated candidate tokens to show.",
    ),
) -> None:
    """Show why the current signal pipeline is or is not producing signals."""
    with session_scope() as session:
        report = analyze_signal_generation(
            Repository(session),
            category=category,
            token_id=token_id,
            market_category=market_category,
            settings=get_settings(),
            top_candidates=top_candidates,
        )
        console.print(build_signal_diagnostics_summary_table(report))
        console.print(build_signal_diagnostics_reasons_table(report))
        console.print(build_signal_diagnostics_candidates_table(report))
        console.print(build_signal_observation_table(report))
        console.print(build_signal_promotion_queue_table(report))
        console.print(build_signal_diagnostics_sensitivity_table(report))


@report_app.command("signal-observations")
def report_signal_observations_cmd(
    category: str = "OVERALL",
    token_id: str | None = typer.Option(
        None,
        "--token-id",
        help="Restrict observation analysis to a single token ID.",
    ),
    market_category: str | None = typer.Option(
        None,
        "--market-category",
        help="Restrict observation analysis to a specific market category.",
    ),
    score_threshold: float = typer.Option(
        0.30,
        "--score-threshold",
        min=0.0,
        max=1.0,
        help="Wallet score threshold used to discover observation candidates.",
    ),
    consensus: int = typer.Option(
        1,
        "--consensus",
        min=1,
        help="Minimum positive-wallet consensus required for observation candidates.",
    ),
    top_candidates: int = typer.Option(
        20,
        "--top-candidates",
        min=1,
        help="Number of observation candidates to show.",
    ),
) -> None:
    """Show observation-only empty-book candidates with last-trade context."""
    with session_scope() as session:
        report = analyze_signal_observations(
            Repository(session),
            category=category,
            token_id=token_id,
            market_category=market_category,
            score_threshold=score_threshold,
            consensus=consensus,
            settings=get_settings(),
            top_candidates=top_candidates,
        )
        console.print(build_signal_diagnostics_summary_table(report))
        console.print(build_signal_diagnostics_reasons_table(report))
        console.print(build_signal_observation_table(report))
        console.print(build_signal_promotion_queue_table(report))


@report_app.command("paper")
def report_paper_cmd() -> None:
    """Show a paper trading report."""
    with session_scope() as session:
        report = Repository(session).paper_report()
        console.print(build_paper_table(report))


@report_app.command("live")
def report_live_cmd() -> None:
    """Show a live trading report reconstructed from synced fills."""
    settings = get_settings()
    with session_scope(settings) as session:
        report = Repository(session).live_report(settings.live_initial_capital)
        console.print(build_live_table(report))


@report_app.command("live-health")
def report_live_health_cmd() -> None:
    """Show persisted live supervisor and kill-switch state."""
    with session_scope() as session:
        report = Repository(session).live_operations_state()
        console.print(build_live_health_table(report))


@report_app.command("live-events")
def report_live_events_cmd(
    top: int = 50,
    category: str = typer.Option(
        "live",
        "--category",
        help="Runtime event category to display, for example live or signal.",
    ),
    event_type: str | None = typer.Option(None, "--event-type"),
) -> None:
    """Show recent persisted live/runtime events."""
    with session_scope() as session:
        rows = Repository(session).list_runtime_events(
            category=category or None,
            event_type=event_type,
            limit=top,
        )
        console.print(build_live_events_table(rows))


@stream_app.command("market")
def stream_market_cmd(
    tokens_from_db: bool = True,
    limit: int = 50,
    token_ids: str | None = typer.Option(None, "--token-ids", help="Comma-separated token IDs."),
    max_messages: int | None = typer.Option(
        None,
        "--max-messages",
        help="Stop after this many raw WebSocket messages.",
    ),
    timeout_sec: float | None = typer.Option(
        None,
        "--timeout-sec",
        help="Stop after this many seconds.",
    ),
) -> None:
    """Stream market-channel events and persist normalized snapshots."""
    with session_scope() as session:
        repo = Repository(session)
        resolved_token_ids: list[str]
        if token_ids:
            resolved_token_ids = [token.strip() for token in token_ids.split(",") if token.strip()]
        elif tokens_from_db:
            resolved_token_ids = [token.token_id for token in repo.list_active_tokens(limit=limit)]
        else:
            raise typer.BadParameter("Provide --token-ids or keep --tokens-from-db enabled.")
        if not resolved_token_ids:
            raise typer.BadParameter("No token IDs are available for streaming.")
        summary = asyncio.run(
            stream_market(
                repo,
                token_ids=resolved_token_ids[:limit],
                max_messages=max_messages,
                timeout_sec=timeout_sec,
            )
        )
        console.print(summary)


@backtest_app.command("replay")
def backtest_replay_cmd(
    from_: str = typer.Option(..., "--from"),
    to: str = typer.Option(..., "--to"),
    category: str = "OVERALL",
    fill_model: str = typer.Option(
        "both",
        "--fill-model",
        help="Replay fill model: optimistic, pessimistic, or both.",
        case_sensitive=False,
    ),
) -> None:
    """Replay stored snapshots and wallet history without lookahead."""
    from_dt = parse_cli_datetime(from_)
    to_dt = parse_cli_datetime(to, end_of_day=True)
    requested_fill_model = fill_model.lower().strip()
    if requested_fill_model not in {"optimistic", "pessimistic", "both"}:
        raise typer.BadParameter("--fill-model must be optimistic, pessimistic, or both.")
    fill_models: list[ReplayFillModel] = (
        ["optimistic", "pessimistic"]
        if requested_fill_model == "both"
        else [cast(ReplayFillModel, requested_fill_model)]
    )
    with session_scope() as session:
        repo = Repository(session)
        for model in fill_models:
            report = replay_backtest(
                repo,
                from_dt=from_dt,
                to_dt=to_dt,
                category=category,
                fill_model=model,
            )
            console.print(build_backtest_table(report))

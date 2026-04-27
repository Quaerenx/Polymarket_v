from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from math import ceil, floor
from time import sleep
from typing import Any

from sqlalchemy import text

from pm_alpha_bot.clients.clob_v2 import (
    ClobV2TradingClient,
    LiveTradingClientError,
    LiveTradingDisabledError,
)
from pm_alpha_bot.clients.geoblock import GeoblockClient
from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.models import Order
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.domain import (
    LiveAccountHealth,
    LiveExecutionResult,
    LiveOpenOrder,
    LiveTradeFill,
    OrderBookSnapshot,
    PreparedLiveOrder,
    SignalRecord,
)
from pm_alpha_bot.logging import get_logger
from pm_alpha_bot.notifications import send_alert
from pm_alpha_bot.risk.limits import evaluate_signal_risk
from pm_alpha_bot.time import ensure_utc

_SYNCABLE_LIVE_STATUSES = {
    "created",
    "submitted",
    "resting",
    "live",
    "delayed",
    "unmatched",
    "matched",
    "partially_filled",
}
_TERMINAL_LIVE_STATUSES = {"filled", "cancelled", "rejected", "expired", "failed"}
_LIVE_SUPERVISOR_STATE_KEY = "live_supervisor"
_LIVE_KILL_SWITCH_STATE_KEY = "live_kill_switch"
_LIVE_SUBMISSION_LOCK_PREFIX = "live_submission"
_LIVE_PRE_SUBMIT_ORDERBOOK_MAX_AGE_SEC = 10.0
logger = get_logger(__name__)


class LiveKillSwitchTriggeredError(LiveTradingDisabledError):
    """Raised when the live supervisor trips its kill switch."""


def execute_live_once(
    repo: Repository,
    *,
    settings: Settings | None = None,
    explicit_live: bool = False,
    client: ClobV2TradingClient | None = None,
) -> LiveExecutionResult:
    """Run live-trading preflight, submit one order, and sync its initial state."""
    resolved = settings or get_settings()
    if not explicit_live:
        raise LiveTradingDisabledError("The --live flag is required for live trading.")
    if not resolved.enable_live_trading:
        raise LiveTradingDisabledError("ENABLE_LIVE_TRADING must be true for live trading.")
    if not resolved.live_credentials_present:
        raise LiveTradingDisabledError("Live trading credentials are missing.")
    _assert_db_healthy(repo)
    _assert_live_submission_allowed(repo)
    if asyncio.run(_check_geoblock(resolved)):
        raise LiveTradingDisabledError("Geoblock check failed. Live trading blocked.")

    prepared = prepare_live_order(repo, settings=resolved)
    trading_client = client or ClobV2TradingClient(resolved)
    try:
        account_health = check_live_account_health(
            repo,
            settings=resolved,
            client=trading_client,
            prepared_order=prepared,
        )
    except LiveTradingClientError as exc:
        _mark_live_signal_status(
            repo,
            prepared.signal_id,
            status="live_failed",
            reason=str(exc),
            commit=True,
        )
        raise
    if not account_health.ok:
        _mark_live_signal_status(
            repo,
            prepared.signal_id,
            status="live_rejected",
            reason=f"Account health check failed: {account_health.reason}",
            commit=True,
        )
        raise LiveTradingDisabledError(f"Account health check failed: {account_health.reason}")
    idempotency_key = _live_submission_idempotency_key(prepared)
    lock_key = _live_submission_lock_key(idempotency_key)
    if not repo.insert_runtime_state_once(
        lock_key,
        _live_submission_lock_payload(
            prepared,
            idempotency_key=idempotency_key,
            state="reserved",
        ),
    ):
        reason = f"Live submission lock already exists for idempotency_key={idempotency_key}."
        _mark_live_signal_status(
            repo,
            prepared.signal_id,
            status="live_rejected",
            reason=reason,
            commit=True,
        )
        raise LiveTradingDisabledError(reason)
    try:
        local_order = repo.create_live_order(
            prepared,
            status="created",
            reason_json={
                "live_submission_idempotency_key": idempotency_key,
                "live_submission_lock_key": lock_key,
                "live_submission_state": "reserved",
                "session_requires_heartbeat": True,
            },
        )
    except ValueError as exc:
        _mark_live_signal_status(
            repo,
            prepared.signal_id,
            status="live_rejected",
            reason=str(exc),
            commit=True,
        )
        raise LiveTradingDisabledError(str(exc)) from exc
    repo.upsert_runtime_state(
        lock_key,
        _live_submission_lock_payload(
            prepared,
            idempotency_key=idempotency_key,
            state="local_order_reserved",
            local_order_id=local_order.id,
        ),
    )
    _commit_live_state(repo)
    try:
        pre_submit_snapshot = trading_client.get_orderbook_snapshot(prepared.token_id)
        _validate_pre_submit_orderbook(
            prepared,
            snapshot=pre_submit_snapshot,
            settings=resolved,
            as_of=datetime.now(UTC),
        )
        repo.save_snapshots([pre_submit_snapshot])
        repo.upsert_runtime_state(
            lock_key,
            _live_submission_lock_payload(
                prepared,
                idempotency_key=idempotency_key,
                state="pre_submit_verified",
                local_order_id=local_order.id,
                pre_submit_snapshot=pre_submit_snapshot,
            ),
        )
        repo.update_order_status(
            local_order,
            status="created",
            reason_json={
                "live_submission_state": "pre_submit_verified",
                "pre_submit_orderbook": _pre_submit_snapshot_payload(pre_submit_snapshot),
            },
        )
        _commit_live_state(repo)
    except LiveTradingClientError as exc:
        repo.update_order_status(
            local_order,
            status="failed",
            reason_json={
                "pre_submit_orderbook_error": str(exc),
                "live_submission_state": "pre_submit_failed",
            },
        )
        repo.upsert_runtime_state(
            lock_key,
            _live_submission_lock_payload(
                prepared,
                idempotency_key=idempotency_key,
                state="pre_submit_failed",
                local_order_id=local_order.id,
                error=str(exc),
            ),
        )
        _mark_live_signal_status(
            repo,
            prepared.signal_id,
            status="live_failed",
            reason=str(exc),
            local_order_id=local_order.id,
            commit=True,
        )
        raise
    except LiveTradingDisabledError as exc:
        repo.update_order_status(
            local_order,
            status="failed",
            reason_json={
                "pre_submit_reject_reason": str(exc),
                "live_submission_state": "pre_submit_rejected",
            },
        )
        repo.upsert_runtime_state(
            lock_key,
            _live_submission_lock_payload(
                prepared,
                idempotency_key=idempotency_key,
                state="pre_submit_rejected",
                local_order_id=local_order.id,
                error=str(exc),
            ),
        )
        _mark_live_signal_status(
            repo,
            prepared.signal_id,
            status="live_rejected",
            reason=str(exc),
            local_order_id=local_order.id,
            commit=True,
        )
        raise
    try:
        submission = trading_client.create_limit_order(
            token_id=prepared.token_id,
            price=prepared.price,
            size=prepared.size,
            side=prepared.side,
            tick_size=prepared.tick_size,
            neg_risk=prepared.neg_risk,
            expiration=prepared.expiration,
            post_only=prepared.post_only,
            order_type=prepared.order_type,
        )
    except LiveTradingClientError as exc:
        repo.update_order_status(
            local_order,
            status="failed",
            reason_json={"submit_error": str(exc), "live_submission_state": "failed"},
        )
        repo.upsert_runtime_state(
            lock_key,
            _live_submission_lock_payload(
                prepared,
                idempotency_key=idempotency_key,
                state="failed",
                local_order_id=local_order.id,
                error=str(exc),
            ),
        )
        _mark_live_signal_status(
            repo,
            prepared.signal_id,
            status="live_failed",
            reason=str(exc),
            local_order_id=local_order.id,
            commit=True,
        )
        raise
    local_status = _submission_status(submission.success, submission.status)
    repo.update_order_status(
        local_order,
        status=local_status,
        external_order_id=submission.external_order_id,
        reason_json={
            "live_submission_idempotency_key": idempotency_key,
            "live_submission_lock_key": lock_key,
            "submit_response": submission.raw_json,
            "live_submission_state": "submitted",
            "session_requires_heartbeat": True,
        },
    )
    repo.upsert_runtime_state(
        lock_key,
        _live_submission_lock_payload(
            prepared,
            idempotency_key=idempotency_key,
            state="submitted",
            local_order_id=local_order.id,
            external_order_id=submission.external_order_id,
            pre_submit_snapshot=pre_submit_snapshot,
        ),
    )
    _mark_live_signal_status(
        repo,
        prepared.signal_id,
        status="processed",
        reason="live_order_created",
        local_order_id=local_order.id,
        external_order_id=submission.external_order_id,
        commit=True,
    )
    synced_status = local_status
    fills_synced = 0
    if submission.external_order_id:
        sync_summary = sync_live_orders(
            repo,
            settings=resolved,
            client=trading_client,
            external_order_ids=[submission.external_order_id],
            limit=1,
        )
        synced_status = str(
            sync_summary.get("order_statuses", {}).get(submission.external_order_id, local_status)
        )
        fills_synced = int(sync_summary.get("fills_synced", 0))
    _record_live_event(
        repo,
        event_type="live_order_submitted",
        message=f"Submitted live order {submission.external_order_id or 'pending-id'}.",
        event_json={
            "local_order_id": local_order.id,
            "external_order_id": submission.external_order_id,
            "submitted_status": local_status,
            "synced_status": synced_status,
            "fills_synced": fills_synced,
            "token_id": prepared.token_id,
            "side": prepared.side,
            "price": prepared.price,
            "size": prepared.size,
        },
    )

    return LiveExecutionResult(
        prepared=prepared,
        local_order_id=local_order.id,
        external_order_id=submission.external_order_id,
        submitted_status=local_status,
        synced_status=synced_status,
        fills_synced=fills_synced,
        raw_response=submission.raw_json,
    )


def cancel_live_order(
    repo: Repository,
    *,
    external_order_id: str,
    settings: Settings | None = None,
    explicit_live: bool = False,
    client: ClobV2TradingClient | None = None,
) -> dict[str, Any]:
    """Cancel a live order and mirror the result locally."""
    if not explicit_live:
        raise LiveTradingDisabledError("The --live flag is required to cancel live orders.")
    resolved = settings or get_settings()
    trading_client = client or ClobV2TradingClient(resolved)
    response = trading_client.cancel_order(external_order_id)
    local_order = repo.get_order_by_external_order_id(external_order_id)
    if local_order is not None:
        repo.update_order_status(
            local_order,
            status="cancelled",
            reason_json={"cancel_response": response},
        )
    _record_live_event(
        repo,
        event_type="live_order_cancelled",
        message=f"Cancelled live order {external_order_id}.",
        event_json={"external_order_id": external_order_id, "response": response},
    )
    return response


def run_live_supervisor_iteration(
    repo: Repository,
    *,
    settings: Settings | None = None,
    client: ClobV2TradingClient | None = None,
    heartbeat_id: str = "",
    submit_new_orders: bool = False,
    sync_limit: int | None = None,
) -> dict[str, Any]:
    """Send one heartbeat, sync remote state, and optionally submit a new live order."""
    resolved = settings or get_settings()
    _assert_db_healthy(repo)
    trading_client = client or ClobV2TradingClient(resolved)
    kill_switch_state = _current_kill_switch_state(repo)
    kill_switch_active = bool(kill_switch_state.get("tripped", False))
    heartbeat = trading_client.post_heartbeat(heartbeat_id)
    next_heartbeat_id = str(heartbeat.get("heartbeat_id") or heartbeat_id)
    sync_summary = sync_live_orders(
        repo,
        settings=resolved,
        client=trading_client,
        limit=sync_limit or resolved.live_order_sync_limit,
    )
    account_health = check_live_account_health(
        repo,
        settings=resolved,
        client=trading_client,
    )
    if not account_health.ok:
        raise LiveTradingDisabledError(f"Account health check failed: {account_health.reason}")
    active_statuses = [
        str(status)
        for status in sync_summary.get("order_statuses", {}).values()
        if str(status) in _SYNCABLE_LIVE_STATUSES
    ]
    submitted_order: LiveExecutionResult | None = None
    skipped_submission_reason: str | None = None
    if submit_new_orders and kill_switch_active:
        skipped_submission_reason = "persistent_kill_switch_active"
    elif submit_new_orders and not active_statuses:
        try:
            submitted_order = execute_live_once(
                repo,
                settings=resolved,
                explicit_live=True,
                client=trading_client,
            )
        except LiveTradingDisabledError as exc:
            skipped_submission_reason = str(exc)
    summary: dict[str, Any] = {
        "heartbeat_id": next_heartbeat_id,
        "orders_synced": int(sync_summary.get("orders_synced", 0)),
        "fills_synced": int(sync_summary.get("fills_synced", 0)),
        "active_orders": len(active_statuses),
        "account_ok": account_health.ok,
        "account_reason": account_health.reason,
        "collateral_available": (
            account_health.collateral.available if account_health.collateral is not None else None
        ),
        "outstanding_buy_notional": account_health.outstanding_buy_notional,
        "outstanding_sell_tokens": account_health.outstanding_sell_size_by_token,
        "kill_switch_active": kill_switch_active,
        "submitted": submitted_order is not None,
        "submitted_order_id": submitted_order.external_order_id if submitted_order else None,
        "submitted_status": submitted_order.synced_status if submitted_order else None,
        "skipped_submission_reason": skipped_submission_reason,
    }
    _persist_supervisor_state(repo, summary, status="ok")
    _record_live_event(
        repo,
        event_type="live_supervisor_iteration",
        message="Completed live supervisor iteration.",
        event_json=summary,
    )
    return summary


def run_live_supervisor(
    repo_factory: Any,
    *,
    settings: Settings | None = None,
    explicit_live: bool = False,
    client: ClobV2TradingClient | None = None,
    interval_sec: float | None = None,
    iterations: int | None = None,
    submit_new_orders: bool = False,
    sync_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Run a long-lived supervisor loop for heartbeats and order reconciliation."""
    resolved = settings or get_settings()
    if not explicit_live:
        raise LiveTradingDisabledError("The --live flag is required for live trading.")
    if not resolved.enable_live_trading:
        raise LiveTradingDisabledError("ENABLE_LIVE_TRADING must be true for live trading.")
    if not resolved.live_credentials_present:
        raise LiveTradingDisabledError("Live trading credentials are missing.")

    heartbeat_id = ""
    loop_interval = interval_sec or resolved.live_heartbeat_interval_sec
    trading_client = client or ClobV2TradingClient(resolved)
    summaries: list[dict[str, Any]] = []
    completed = 0
    consecutive_errors = 0
    while iterations is None or completed < iterations:
        try:
            with repo_factory() as repo:
                if completed == 0 and asyncio.run(_check_geoblock(resolved)):
                    raise LiveTradingDisabledError("Geoblock check failed. Live trading blocked.")
                summary = run_live_supervisor_iteration(
                    repo,
                    settings=resolved,
                    client=trading_client,
                    heartbeat_id=heartbeat_id,
                    submit_new_orders=submit_new_orders,
                    sync_limit=sync_limit,
                )
                submission_failed = bool(summary.get("submitted")) and str(
                    summary.get("submitted_status")
                ) in {"rejected", "failed"}
                consecutive_errors = consecutive_errors + 1 if submission_failed else 0
                summary["consecutive_errors"] = consecutive_errors
                summary["kill_switch_triggered"] = False
                _persist_supervisor_state(repo, summary, status="ok")
            summaries.append(summary)
            heartbeat_id = str(summary["heartbeat_id"])
            logger.info("live_supervisor_iteration", extra=summary)
        except (LiveTradingClientError, LiveTradingDisabledError) as exc:
            consecutive_errors += 1
            summary = {
                "heartbeat_id": heartbeat_id,
                "error": str(exc),
                "consecutive_errors": consecutive_errors,
                "kill_switch_triggered": False,
                "submitted": False,
            }
            with repo_factory() as repo:
                _persist_supervisor_state(repo, summary, status="error")
                _record_live_event(
                    repo,
                    event_type="live_supervisor_error",
                    level="error",
                    message=f"Live supervisor iteration failed: {exc}",
                    event_json=summary,
                )
            logger.error("live_supervisor_iteration_failed", extra=summary)
            if consecutive_errors >= resolved.live_max_consecutive_errors:
                cancel_summary: dict[str, Any] | None = None
                with repo_factory() as repo:
                    if resolved.live_cancel_all_on_kill_switch:
                        cancel_summary = _cancel_all_live_orders(repo, trading_client)
                    kill_state = _trip_live_kill_switch(
                        repo,
                        reason=str(exc),
                        consecutive_errors=consecutive_errors,
                        cancel_summary=cancel_summary,
                    )
                    _persist_supervisor_state(
                        repo,
                        {
                            **summary,
                            "kill_switch_triggered": True,
                            "cancel_summary": cancel_summary,
                        },
                        status="killed",
                    )
                    _record_live_event(
                        repo,
                        event_type="live_kill_switch_triggered",
                        level="critical",
                        message="Live kill switch triggered.",
                        event_json={
                            **summary,
                            "cancel_summary": cancel_summary,
                            "kill_switch_state": kill_state,
                        },
                    )
                summary["kill_switch_triggered"] = True
                summary["cancel_summary"] = cancel_summary
                summary["kill_switch_state"] = kill_state
                summaries.append(summary)
                message = (
                    "Live kill switch triggered after "
                    f"{consecutive_errors} consecutive errors: {exc}"
                )
                send_alert(
                    "live_kill_switch_triggered",
                    message,
                    payload=summary,
                    settings=resolved,
                )
                raise LiveKillSwitchTriggeredError(
                    message
                ) from exc
            summaries.append(summary)
            completed += 1
            if iterations is not None and completed >= iterations:
                break
            sleep(loop_interval)
            continue
        completed += 1
        if iterations is not None and completed >= iterations:
            break
        sleep(loop_interval)
    return summaries


def sync_live_orders(
    repo: Repository,
    *,
    settings: Settings | None = None,
    client: ClobV2TradingClient | None = None,
    external_order_ids: Sequence[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Sync remote live-order state and trades back into the local database."""
    resolved = settings or get_settings()
    trading_client = client or ClobV2TradingClient(resolved)
    remote_open_orders = trading_client.get_open_orders(only_first_page=True)
    order_map: dict[str, Order] = {}
    for remote_order in remote_open_orders:
        local_order = repo.get_order_by_external_order_id(remote_order.external_order_id)
        if local_order is None:
            local_order = repo.create_live_order_from_remote(remote_order)
        _apply_remote_order_state(repo, local_order, remote_order, source="open_orders")
        order_map[remote_order.external_order_id] = local_order

    candidate_ids = list(dict.fromkeys(str(order_id) for order_id in external_order_ids or []))
    if not candidate_ids:
        for order in repo.list_orders(
            mode="live",
            statuses=sorted(_SYNCABLE_LIVE_STATUSES),
            limit=limit,
        ):
            if order.external_order_id:
                candidate_ids.append(order.external_order_id)
    for external_order_id in candidate_ids:
        if external_order_id in order_map:
            continue
        local_order = repo.get_order_by_external_order_id(external_order_id)
        if local_order is None:
            continue
        try:
            remote_order = trading_client.get_order(external_order_id)
        except LiveTradingClientError:
            continue
        _apply_remote_order_state(repo, local_order, remote_order, source="order_detail")
        order_map[external_order_id] = local_order

    fills_synced = 0
    for external_order_id, local_order in order_map.items():
        fills_synced += _sync_trade_fills_for_order(repo, trading_client, local_order)
        repo.update_order_status(
            local_order,
            status=_finalize_live_order_status(repo, local_order),
            reason_json={"last_sync_at": datetime.now(UTC).isoformat()},
        )
        order_map[external_order_id] = local_order

    summary = {
        "orders_synced": len(order_map),
        "fills_synced": fills_synced,
        "order_statuses": {
            external_order_id: order.status for external_order_id, order in order_map.items()
        },
    }
    _record_live_event(
        repo,
        event_type="live_sync_completed",
        message="Synced live orders and fills.",
        event_json=summary,
    )
    return summary


def check_live_account_health(
    repo: Repository,
    *,
    settings: Settings | None = None,
    client: ClobV2TradingClient | None = None,
    prepared_order: PreparedLiveOrder | None = None,
) -> LiveAccountHealth:
    """Check remote balances and allowances against local live-order requirements."""
    resolved = settings or get_settings()
    trading_client = client or ClobV2TradingClient(resolved)
    open_orders = repo.list_orders(mode="live", statuses=sorted(_SYNCABLE_LIVE_STATUSES))
    outstanding_buy_notional = 0.0
    outstanding_sell_size_by_token: dict[str, float] = {}
    for order in open_orders:
        remaining = max(0.0, (order.size or 0.0) - repo.filled_size_for_order(order.id))
        if remaining <= 0:
            continue
        if order.side == "BUY":
            outstanding_buy_notional += remaining * float(order.price or 0.0)
        elif order.token_id:
            outstanding_sell_size_by_token[order.token_id] = (
                outstanding_sell_size_by_token.get(order.token_id, 0.0) + remaining
            )

    collateral = trading_client.get_balance_allowance(asset_type="COLLATERAL")
    conditional_snapshots: list[Any] = []
    required_available = outstanding_buy_notional
    required_token_id: str | None = None
    if prepared_order is not None and prepared_order.side == "BUY":
        required_available += prepared_order.price * prepared_order.size
    if collateral.available is None or collateral.available < required_available - 1e-9:
        return LiveAccountHealth(
            ok=False,
            reason="insufficient_collateral_balance_allowance",
            collateral=collateral,
            conditional=conditional_snapshots,
            outstanding_buy_notional=outstanding_buy_notional,
            outstanding_sell_size_by_token=outstanding_sell_size_by_token,
            required_available=required_available,
            required_token_id=required_token_id,
        )

    required_sell_size_by_token = dict(outstanding_sell_size_by_token)
    if prepared_order is not None and prepared_order.side == "SELL":
        required_token_id = prepared_order.token_id
        required_sell_size_by_token[prepared_order.token_id] = (
            required_sell_size_by_token.get(prepared_order.token_id, 0.0) + prepared_order.size
        )
    for token_id, required_size in required_sell_size_by_token.items():
        snapshot = trading_client.get_balance_allowance(
            asset_type="CONDITIONAL",
            token_id=token_id,
        )
        conditional_snapshots.append(snapshot)
        if snapshot.available is None or snapshot.available < required_size - 1e-9:
            return LiveAccountHealth(
                ok=False,
                reason=f"insufficient_conditional_balance_allowance:{token_id}",
                collateral=collateral,
                conditional=conditional_snapshots,
                outstanding_buy_notional=outstanding_buy_notional,
                outstanding_sell_size_by_token=outstanding_sell_size_by_token,
                required_available=required_size,
                required_token_id=token_id,
            )

    return LiveAccountHealth(
        ok=True,
        reason="ok",
        collateral=collateral,
        conditional=conditional_snapshots,
        outstanding_buy_notional=outstanding_buy_notional,
        outstanding_sell_size_by_token=outstanding_sell_size_by_token,
        required_available=required_available,
        required_token_id=required_token_id,
    )


def prepare_live_order(
    repo: Repository,
    *,
    settings: Settings | None = None,
    as_of: datetime | None = None,
) -> PreparedLiveOrder:
    """Prepare and validate the next live order candidate without placing it."""
    resolved = settings or get_settings()
    ts = as_of or datetime.now(UTC)
    signal = _next_live_signal(repo, as_of=ts)
    if signal is None:
        raise LiveTradingDisabledError("No fresh signal is available for live trading.")
    signal_id = int(signal.reason_json.get("signal_id", 0))
    try:
        if repo.live_order_exists_for_signal(signal_id):
            raise LiveTradingDisabledError(f"Live order already exists for signal_id={signal_id}.")
        if repo.active_live_order_exists_for_token(signal.token_id):
            raise LiveTradingDisabledError(
                f"Active live order already exists for token_id={signal.token_id}."
            )
        market = repo.get_market_by_condition_id(signal.condition_id)
        snapshot = repo.latest_snapshot_for_token(signal.token_id, as_of=ts)
        if market is None or snapshot is None:
            raise LiveTradingDisabledError("No current market or orderbook snapshot exists.")
        if (ts - ensure_utc(snapshot.ts)).total_seconds() > resolved.snapshot_stale_after_sec:
            raise LiveTradingDisabledError("Latest orderbook snapshot is stale.")
        if (ts - signal.ts).total_seconds() > resolved.snapshot_stale_after_sec:
            raise LiveTradingDisabledError("Signal is too old for live trading.")

        risk_state = repo.live_risk_state(resolved.live_initial_capital)
        decision = evaluate_signal_risk(
            signal=signal,
            market=market,
            snapshot=snapshot,
            risk_state=risk_state,
            settings=resolved,
            as_of=ts,
        )
        if not decision.passed or signal.effective_entry_price is None:
            raise LiveTradingDisabledError(f"Risk engine rejected live order: {decision.reason}")

        tick_size = _tick_size_string(market.min_tick_size)
        live_price = _quantize_price(signal.effective_entry_price, tick_size, signal.direction)
        _assert_maker_safe_price(signal.direction, live_price, snapshot)
    except LiveTradingDisabledError as exc:
        _mark_live_signal_status(
            repo,
            signal_id,
            status="live_rejected",
            reason=str(exc),
            commit=True,
        )
        raise

    return PreparedLiveOrder(
        signal_id=signal_id,
        condition_id=signal.condition_id,
        token_id=signal.token_id,
        side=signal.direction,
        price=live_price,
        size=decision.size_units,
        tick_size=tick_size,
        neg_risk=bool(market.neg_risk),
        order_type="GTC",
        post_only=True,
        expiration=0,
        reason_json={
            **signal.reason_json,
            "risk": {"reason": decision.reason, "size_pct": decision.size_pct},
            "live_price": {
                "requested": signal.effective_entry_price,
                "quantized": live_price,
                "tick_size": tick_size,
            },
        },
    )


def reset_live_kill_switch(
    repo: Repository,
    *,
    settings: Settings | None = None,
    acknowledge: bool = False,
    reset_reason: str | None = None,
    allow_active_orders: bool = False,
) -> dict[str, Any]:
    """Reset the persisted live kill switch after operator acknowledgement."""
    resolved = settings or get_settings()
    if not acknowledge:
        raise LiveTradingDisabledError(
            "Resetting the live kill switch requires explicit operator acknowledgement."
        )
    active_orders = repo.list_orders(
        mode="live",
        statuses=sorted(_SYNCABLE_LIVE_STATUSES),
    )
    active_order_count = len(active_orders)
    if active_order_count > 0 and not allow_active_orders:
        raise LiveTradingDisabledError(
            "Cannot reset the live kill switch while active live orders remain. "
            "Run live sync/cancel-all first or explicitly allow active orders."
        )
    previous_state = _current_kill_switch_state(repo)
    reason = " ".join(str(reset_reason or "").strip().split())
    if not reason:
        reason = "operator_acknowledged_reset"
    payload = {
        "tripped": False,
        "reason": "",
        "tripped_at": previous_state.get("tripped_at"),
        "reset_at": datetime.now(UTC).isoformat(),
        "reset_reason": reason,
        "reset_acknowledged": True,
        "previous_tripped": bool(previous_state.get("tripped", False)),
        "previous_reason": previous_state.get("reason"),
        "previous_consecutive_errors": previous_state.get("consecutive_errors", 0),
        "previous_cancel_summary": previous_state.get("cancel_summary"),
        "active_live_orders_at_reset": active_order_count,
        "allow_active_orders": allow_active_orders,
    }
    repo.upsert_runtime_state(_LIVE_KILL_SWITCH_STATE_KEY, payload)
    _record_live_event(
        repo,
        event_type="live_kill_switch_reset",
        level="warning",
        message="Reset persisted live kill switch.",
        event_json=payload,
    )
    send_alert(
        "live_kill_switch_reset",
        "Live kill switch reset by operator.",
        payload=payload,
        settings=resolved,
    )
    return payload


def _assert_db_healthy(repo: Repository) -> None:
    repo.session.execute(text("SELECT 1"))


def _assert_live_submission_allowed(repo: Repository) -> None:
    kill_switch_state = _current_kill_switch_state(repo)
    if bool(kill_switch_state.get("tripped", False)):
        reason = str(kill_switch_state.get("reason") or "persistent_kill_switch_active")
        raise LiveTradingDisabledError(
            "Persistent live kill switch is active. "
            f"Reset it before submitting live orders. reason={reason}"
        )


def _next_live_signal(repo: Repository, *, as_of: datetime) -> SignalRecord | None:
    signal = repo.claim_next_live_signal(as_of=as_of, limit=20)
    if signal is None:
        return None
    reason_json = dict(signal.reason_json or {})
    reason_json["signal_id"] = signal.id
    return SignalRecord(
        ts=ensure_utc(signal.ts),
        condition_id=signal.condition_id,
        token_id=signal.token_id,
        direction=signal.direction,
        market_midpoint=signal.market_midpoint,
        effective_entry_price=signal.effective_entry_price,
        fair_prob=signal.fair_prob,
        edge_bps=signal.edge_bps,
        confidence=signal.confidence,
        source_wallet_count=signal.source_wallet_count,
        source_wallets_json=signal.source_wallets_json or [],
        reason_json=reason_json,
        status=signal.status,
    )


def _mark_live_signal_status(
    repo: Repository,
    signal_id: int,
    *,
    status: str,
    reason: str,
    local_order_id: int | None = None,
    external_order_id: str | None = None,
    commit: bool = False,
) -> None:
    if signal_id <= 0:
        return
    payload: dict[str, Any] = {
        "live_status_reason": reason,
        "live_status_updated_at": datetime.now(UTC).isoformat(),
    }
    if local_order_id is not None:
        payload["live_order_id"] = local_order_id
    if external_order_id is not None:
        payload["external_order_id"] = external_order_id
    repo.update_signal_status(signal_id, status=status, reason_json=payload)
    if commit:
        _commit_live_state(repo)


def _commit_live_state(repo: Repository) -> None:
    """Persist live execution guard state before external side effects can diverge."""
    repo.session.commit()


def _live_submission_idempotency_key(prepared: PreparedLiveOrder) -> str:
    raw_key = "|".join(
        [
            str(prepared.signal_id),
            prepared.condition_id,
            prepared.token_id,
            prepared.side.upper(),
            _stable_float(prepared.price),
            _stable_float(prepared.size),
            prepared.order_type.upper(),
            "post_only" if prepared.post_only else "not_post_only",
        ]
    )
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _live_submission_lock_key(idempotency_key: str) -> str:
    return f"{_LIVE_SUBMISSION_LOCK_PREFIX}:{idempotency_key[:48]}"


def _live_submission_lock_payload(
    prepared: PreparedLiveOrder,
    *,
    idempotency_key: str,
    state: str,
    local_order_id: int | None = None,
    external_order_id: str | None = None,
    error: str | None = None,
    pre_submit_snapshot: OrderBookSnapshot | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": state,
        "idempotency_key": idempotency_key,
        "signal_id": prepared.signal_id,
        "condition_id": prepared.condition_id,
        "token_id": prepared.token_id,
        "side": prepared.side,
        "price": prepared.price,
        "size": prepared.size,
        "order_type": prepared.order_type,
        "post_only": prepared.post_only,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if local_order_id is not None:
        payload["local_order_id"] = local_order_id
    if external_order_id is not None:
        payload["external_order_id"] = external_order_id
    if error is not None:
        payload["error"] = error
    if pre_submit_snapshot is not None:
        payload["pre_submit_orderbook"] = _pre_submit_snapshot_payload(pre_submit_snapshot)
    return payload


def _stable_float(value: float) -> str:
    return f"{value:.12g}"


def _validate_pre_submit_orderbook(
    prepared: PreparedLiveOrder,
    *,
    snapshot: OrderBookSnapshot,
    settings: Settings,
    as_of: datetime,
) -> None:
    if snapshot.token_id != prepared.token_id:
        raise LiveTradingDisabledError(
            f"Pre-submit orderbook token mismatch: {snapshot.token_id} != {prepared.token_id}."
        )
    age_sec = max(0.0, (ensure_utc(as_of) - ensure_utc(snapshot.ts)).total_seconds())
    if age_sec > _LIVE_PRE_SUBMIT_ORDERBOOK_MAX_AGE_SEC:
        raise LiveTradingDisabledError(
            "Pre-submit CLOB orderbook is stale: "
            f"age={age_sec:.1f}s max={_LIVE_PRE_SUBMIT_ORDERBOOK_MAX_AGE_SEC:.0f}s."
        )
    if snapshot.best_bid is None or snapshot.best_ask is None:
        raise LiveTradingDisabledError("Pre-submit CLOB orderbook is missing bid/ask.")
    if snapshot.best_bid <= 0 or snapshot.best_ask >= 1 or snapshot.best_bid >= snapshot.best_ask:
        raise LiveTradingDisabledError("Pre-submit CLOB orderbook bid/ask is invalid.")
    live_spread_bps = (snapshot.best_ask - snapshot.best_bid) * 10_000
    if live_spread_bps > settings.max_spread_bps:
        raise LiveTradingDisabledError(
            "Pre-submit CLOB spread exceeds limit: "
            f"{live_spread_bps:.1f}bps > {settings.max_spread_bps}bps."
        )
    _assert_maker_safe_price(prepared.side, prepared.price, snapshot)


def _pre_submit_snapshot_payload(snapshot: OrderBookSnapshot) -> dict[str, Any]:
    return {
        "ts": ensure_utc(snapshot.ts).isoformat(),
        "condition_id": snapshot.condition_id,
        "token_id": snapshot.token_id,
        "best_bid": snapshot.best_bid,
        "best_ask": snapshot.best_ask,
        "midpoint": snapshot.midpoint,
        "spread": snapshot.spread,
    }


def _tick_size_string(value: float | None) -> str | None:
    if value is None:
        return None
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or None


def _quantize_price(price: float, tick_size: str | None, side: str) -> float:
    if tick_size is None:
        return round(price, 6)
    tick = float(tick_size)
    bounded = max(tick, min(1.0 - tick, price))
    precision = len(tick_size.partition(".")[2])
    if side.upper() == "SELL":
        quantized = ceil((bounded / tick) - 1e-12) * tick
    else:
        quantized = floor((bounded / tick) + 1e-12) * tick
    quantized = max(tick, min(1.0 - tick, quantized))
    return round(quantized, precision)


def _assert_maker_safe_price(
    side: str,
    live_price: float,
    snapshot: Any,
) -> None:
    resolved_side = side.upper()
    if (
        resolved_side == "BUY"
        and snapshot.best_ask is not None
        and live_price >= snapshot.best_ask
    ):
        raise LiveTradingDisabledError(
            "Quantized live order would cross the ask and violate post-only."
        )
    if (
        resolved_side == "SELL"
        and snapshot.best_bid is not None
        and live_price <= snapshot.best_bid
    ):
        raise LiveTradingDisabledError(
            "Quantized live order would cross the bid and violate post-only."
        )


def _submission_status(success: bool, status: str | None) -> str:
    if not success:
        return "rejected"
    if status:
        return status
    return "submitted"


def _apply_remote_order_state(
    repo: Repository,
    local_order: Order,
    remote_order: LiveOpenOrder,
    *,
    source: str,
) -> None:
    local_order.condition_id = remote_order.condition_id or local_order.condition_id
    local_order.token_id = remote_order.token_id or local_order.token_id
    local_order.side = remote_order.side or local_order.side
    local_order.price = remote_order.price if remote_order.price is not None else local_order.price
    local_order.size = (
        remote_order.original_size
        if remote_order.original_size is not None
        else local_order.size
    )
    local_order.order_type = remote_order.order_type or local_order.order_type
    repo.update_order_status(
        local_order,
        status=_remote_order_status(local_order, remote_order),
        external_order_id=remote_order.external_order_id,
        reason_json={
            "remote_order": remote_order.raw_json,
            "remote_sync_source": source,
        },
    )


def _remote_order_status(local_order: Order, remote_order: LiveOpenOrder) -> str:
    size_target = remote_order.original_size or local_order.size or 0.0
    size_matched = remote_order.size_matched or 0.0
    if size_target > 0 and size_matched >= size_target - 1e-9:
        return "filled"
    if size_matched > 0:
        return "partially_filled"
    return remote_order.status or local_order.status or "submitted"


def _persist_supervisor_state(
    repo: Repository,
    summary: dict[str, Any],
    *,
    status: str,
) -> None:
    previous = repo.get_runtime_state(_LIVE_SUPERVISOR_STATE_KEY)
    previous_state = dict(previous.state_json or {}) if previous is not None else {}
    now = datetime.now(UTC).isoformat()
    payload = dict(summary)
    payload["status"] = status
    payload["last_iteration_at"] = now
    if status == "ok":
        payload["last_success_at"] = now
    elif "last_success_at" not in payload and previous_state.get("last_success_at"):
        payload["last_success_at"] = previous_state["last_success_at"]
    if "error" in payload:
        payload["last_error"] = payload["error"]
        payload["last_error_at"] = now
    elif previous_state.get("last_error"):
        payload["last_error"] = previous_state["last_error"]
        payload["last_error_at"] = previous_state.get("last_error_at")
    repo.upsert_runtime_state(_LIVE_SUPERVISOR_STATE_KEY, payload)


def _current_kill_switch_state(repo: Repository) -> dict[str, Any]:
    state = repo.get_runtime_state(_LIVE_KILL_SWITCH_STATE_KEY)
    return dict(state.state_json or {}) if state is not None else {}


def _trip_live_kill_switch(
    repo: Repository,
    *,
    reason: str,
    consecutive_errors: int,
    cancel_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "tripped": True,
        "reason": reason,
        "consecutive_errors": consecutive_errors,
        "tripped_at": datetime.now(UTC).isoformat(),
        "cancel_summary": cancel_summary,
    }
    repo.upsert_runtime_state(_LIVE_KILL_SWITCH_STATE_KEY, payload)
    return payload


def _record_live_event(
    repo: Repository,
    *,
    event_type: str,
    message: str,
    level: str = "info",
    event_json: dict[str, Any] | None = None,
) -> None:
    repo.add_runtime_event(
        category="live",
        event_type=event_type,
        level=level,
        message=message,
        event_json=event_json,
    )


def _sync_trade_fills_for_order(
    repo: Repository,
    client: ClobV2TradingClient,
    local_order: Order,
) -> int:
    if not local_order.external_order_id:
        return 0
    fills = repo.fills_for_order(local_order.id)
    after_ts = ensure_utc(local_order.created_at)
    if fills:
        after_ts = ensure_utc(fills[-1].ts)
    after = max(0, int(after_ts.timestamp()) - 5)
    trades = client.get_trades(
        market=local_order.condition_id or None,
        asset_id=local_order.token_id or None,
        after=after,
        only_first_page=True,
    )
    synced = 0
    for trade in trades:
        fill_payload = _fill_payload_for_order(trade, local_order.external_order_id)
        if fill_payload is None:
            continue
        fill, created = repo.upsert_fill(
            local_order,
            ts=trade.ts,
            price=fill_payload["price"],
            size=fill_payload["size"],
            fee=fill_payload["fee"],
            tx_hash=trade.tx_hash,
            raw_json=fill_payload["raw_json"],
        )
        _ = fill
        if created:
            synced += 1
    return synced


def _fill_payload_for_order(
    trade: LiveTradeFill,
    external_order_id: str,
) -> dict[str, Any] | None:
    role: str | None = None
    fill_size = trade.size
    if trade.taker_order_id == external_order_id:
        role = "taker"
    elif external_order_id in trade.maker_order_ids:
        role = "maker"
        fill_size = _maker_fill_size(trade, external_order_id)
    if role is None or trade.price is None or fill_size is None or fill_size <= 0:
        return None
    raw_json = dict(trade.raw_json or {})
    raw_json["trade_id"] = trade.trade_id
    raw_json["order_role"] = role
    return {
        "price": trade.price,
        "size": fill_size,
        "fee": trade.fee or 0.0,
        "raw_json": raw_json,
    }


def _maker_fill_size(trade: LiveTradeFill, external_order_id: str) -> float | None:
    raw_json = trade.raw_json or {}
    maker_orders = raw_json.get("maker_orders")
    if not isinstance(maker_orders, list):
        return trade.size
    for maker_order in maker_orders:
        if not isinstance(maker_order, dict):
            continue
        if str(maker_order.get("order_id") or "") != external_order_id:
            continue
        matched_amount = maker_order.get("matched_amount")
        if matched_amount is None:
            break
        try:
            return float(matched_amount)
        except (TypeError, ValueError):
            break
    return trade.size


def _finalize_live_order_status(repo: Repository, local_order: Order) -> str:
    filled_size = repo.filled_size_for_order(local_order.id)
    target_size = local_order.size or 0.0
    if target_size > 0 and filled_size >= target_size - 1e-9:
        return "filled"
    if filled_size > 0 and local_order.status not in _TERMINAL_LIVE_STATUSES:
        return "partially_filled"
    return local_order.status


def _cancel_all_live_orders(
    repo: Repository,
    client: ClobV2TradingClient,
) -> dict[str, Any]:
    """Cancel all remote live orders and mirror the result locally."""
    summary = client.cancel_all()
    canceled_ids = {str(order_id) for order_id in summary.get("canceled", []) if order_id}
    for external_order_id in canceled_ids:
        local_order = repo.get_order_by_external_order_id(external_order_id)
        if local_order is None:
            continue
        repo.update_order_status(
            local_order,
            status="cancelled",
            reason_json={"cancel_all_response": summary},
        )
    return summary


async def _check_geoblock(settings: Settings) -> bool:
    async with GeoblockClient(settings) as client:
        status = await client.check()
        return status.blocked

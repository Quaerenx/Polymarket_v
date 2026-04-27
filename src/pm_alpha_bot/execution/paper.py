from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import sleep

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.domain import PaperOrderRequest, SignalRecord
from pm_alpha_bot.logging import get_logger
from pm_alpha_bot.risk.costs import adverse_fill_price, fill_fee, maker_queue_fill_price
from pm_alpha_bot.risk.limits import evaluate_signal_risk
from pm_alpha_bot.time import ensure_utc

logger = get_logger(__name__)


class PaperBroker:
    """Conservative maker-first paper trading broker."""

    def __init__(self, repo: Repository, settings: Settings | None = None) -> None:
        self.repo = repo
        self.settings = settings or get_settings()

    def process_open_orders(self, as_of: datetime | None = None) -> int:
        """Try to fill resting paper orders using future snapshots."""
        resolved_as_of = as_of or datetime.now(UTC)
        updated = 0
        for order in self.repo.list_open_orders(mode="paper"):
            filled_size = self.repo.filled_size_for_order(order.id)
            remaining = max(0.0, (order.size or 0.0) - filled_size)
            if remaining <= 0:
                order.status = "filled"
                updated += 1
                continue
            future_snapshots = self.repo.snapshots_between(
                order.token_id,
                order.created_at + timedelta(microseconds=1),
                resolved_as_of,
            )
            fill_candidate = None
            for snapshot in future_snapshots:
                dwell_sec = (
                    ensure_utc(snapshot.ts) - ensure_utc(order.created_at)
                ).total_seconds()
                if dwell_sec < self.settings.paper_fill_min_dwell_sec:
                    continue
                fill_price = maker_queue_fill_price(
                    side=order.side,
                    limit_price=order.price,
                    best_bid=snapshot.best_bid,
                    best_ask=snapshot.best_ask,
                    queue_miss_bps=self.settings.paper_queue_miss_bps,
                )
                if fill_price is not None:
                    fill_candidate = (snapshot, fill_price)
                    break
            if fill_candidate is None:
                continue
            snapshot, fill_price = fill_candidate
            conservative_fill = remaining if remaining <= 1 else max(remaining * 0.5, 1.0)
            if conservative_fill >= remaining:
                order.status = "filled"
            else:
                order.status = "partially_filled"
            self._apply_fill(
                order_id=order.id,
                fill_price=fill_price,
                fill_size=conservative_fill,
                ts=snapshot.ts,
            )
            updated += 1
        return updated

    def create_orders_from_signals(
        self, signal_rows: list[SignalRecord], as_of: datetime | None = None
    ) -> int:
        """Create paper orders for eligible signals."""
        resolved_as_of = as_of or datetime.now(UTC)
        created = 0
        open_order_tokens = {order.token_id for order in self.repo.list_open_orders(mode="paper")}
        paper_position_tokens = {
            position.token_id
            for position in self.repo.list_paper_positions()
            if position.size is not None and position.size > 0
        }
        for signal in signal_rows:
            market = self.repo.get_market_by_condition_id(signal.condition_id)
            snapshot = self.repo.latest_snapshot_for_token(signal.token_id, as_of=resolved_as_of)
            if market is None or snapshot is None:
                continue
            if signal.token_id in open_order_tokens or signal.token_id in paper_position_tokens:
                reason = dict(signal.reason_json)
                reason["risk"] = {
                    "passed": False,
                    "reason": "duplicate_open_order_or_position",
                }
                self.repo.create_order(
                    PaperOrderRequest(
                        condition_id=signal.condition_id,
                        token_id=signal.token_id,
                        side=signal.direction,
                        price=signal.effective_entry_price or 0.0,
                        size=0.0,
                        signal_id=signal.reason_json.get("signal_id"),
                        reason_json=reason,
                    ),
                    mode="paper",
                    status="rejected",
                )
                created += 1
                continue
            risk_state = self.repo.paper_risk_state(self.settings.paper_initial_capital)
            decision = evaluate_signal_risk(
                signal=signal,
                market=market,
                snapshot=snapshot,
                risk_state=risk_state,
                settings=self.settings,
                as_of=resolved_as_of,
            )
            reason = dict(signal.reason_json)
            reason["risk"] = {"passed": decision.passed, "reason": decision.reason}
            if not decision.passed:
                self.repo.create_order(
                    PaperOrderRequest(
                        condition_id=signal.condition_id,
                        token_id=signal.token_id,
                        side=signal.direction,
                        price=signal.effective_entry_price or 0.0,
                        size=0.0,
                        signal_id=signal.reason_json.get("signal_id"),
                        reason_json=reason,
                    ),
                    mode="paper",
                    status="rejected",
                )
                created += 1
                continue
            status = "resting"
            if (
                snapshot.best_ask is not None
                and signal.effective_entry_price is not None
                and signal.effective_entry_price >= snapshot.best_ask
            ):
                status = "would_take"
            order = self.repo.create_order(
                PaperOrderRequest(
                    condition_id=signal.condition_id,
                    token_id=signal.token_id,
                    side=signal.direction,
                    price=signal.effective_entry_price or 0.0,
                    size=decision.size_units,
                    signal_id=signal.reason_json.get("signal_id"),
                    reason_json=reason,
                ),
                mode="paper",
                status=status,
            )
            created += 1
            open_order_tokens.add(signal.token_id)
            if status == "would_take":
                logger.info(
                    "paper_order_would_take",
                    extra={"order_id": order.id, "token_id": order.token_id},
                )
        return created

    def run_once(self) -> dict[str, int]:
        """Process existing orders and place new orders from recent signals."""
        processed = self.process_open_orders()
        orm_signals = self.repo.list_recent_signals(status="new", limit=20)
        signal_rows = []
        for signal in orm_signals:
            reason_json = dict(signal.reason_json or {})
            reason_json["signal_id"] = signal.id
            signal_rows.append(
                SignalRecord(
                    ts=signal.ts,
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
            )
        created = self.create_orders_from_signals(signal_rows)
        for signal in orm_signals:
            signal.status = "processed"
        return {"processed_orders": processed, "created_orders": created}

    def run_loop(self, interval: int) -> None:
        """Continuously execute paper trading iterations."""
        while True:
            summary = self.run_once()
            logger.info("paper_loop_iteration", extra=summary)
            self.repo.session.commit()
            sleep(interval)

    def _apply_fill(self, order_id: int, fill_price: float, fill_size: float, ts: datetime) -> None:
        """Apply a conservative fill to the paper portfolio."""
        order = self.repo.get_order(order_id)
        if order is None:
            return
        position = self.repo.get_paper_position(order.token_id)
        adjusted_fill_price = adverse_fill_price(
            fill_price,
            order.side,
            self.settings.paper_slippage_bps,
        )
        fee = fill_fee(adjusted_fill_price, fill_size, self.settings.paper_fee_bps)
        gross_realized_pnl_delta = 0.0
        realized_pnl_delta = -fee
        if order.side == "BUY":
            if position is None:
                new_size = fill_size
                new_avg = adjusted_fill_price
                realized_total = -fee
            else:
                new_size = position.size + fill_size
                new_avg = (
                    (
                        (position.avg_price * position.size)
                        + (adjusted_fill_price * fill_size)
                    )
                    / new_size
                    if new_size > 0
                    else adjusted_fill_price
                )
                realized_total = position.realized_pnl - fee
            latest_snapshot = self.repo.latest_snapshot_for_token(order.token_id, as_of=ts)
            unrealized = 0.0
            if latest_snapshot is not None and latest_snapshot.midpoint is not None:
                unrealized = (latest_snapshot.midpoint - new_avg) * new_size
            self.repo.upsert_paper_position(
                condition_id=order.condition_id,
                token_id=order.token_id,
                side="LONG",
                size=new_size,
                avg_price=new_avg,
                realized_pnl=realized_total,
                unrealized_pnl=unrealized,
            )
        elif order.side == "SELL" and position is not None:
            close_size = min(position.size, fill_size)
            remaining = max(0.0, position.size - close_size)
            gross_realized_pnl_delta = (adjusted_fill_price - position.avg_price) * close_size
            realized_pnl_delta = gross_realized_pnl_delta - fee
            latest_snapshot = self.repo.latest_snapshot_for_token(order.token_id, as_of=ts)
            unrealized = 0.0
            if (
                latest_snapshot is not None
                and latest_snapshot.midpoint is not None
                and remaining > 0
            ):
                unrealized = (latest_snapshot.midpoint - position.avg_price) * remaining
            self.repo.upsert_paper_position(
                condition_id=order.condition_id,
                token_id=order.token_id,
                side="LONG",
                size=remaining,
                avg_price=position.avg_price,
                realized_pnl=position.realized_pnl + realized_pnl_delta,
                unrealized_pnl=unrealized,
            )
        self.repo.add_fill(
            order=order,
            ts=ts,
            price=adjusted_fill_price,
            size=fill_size,
            fee=fee,
            raw_json={
                "gross_realized_pnl_delta": gross_realized_pnl_delta,
                "realized_pnl_delta": realized_pnl_delta,
                "applied_fee_bps": self.settings.paper_fee_bps,
                "applied_slippage_bps": self.settings.paper_slippage_bps,
            },
        )

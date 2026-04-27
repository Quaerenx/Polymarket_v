from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.models import MarketSnapshot
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.domain import BacktestReport, RiskState, SignalRecord
from pm_alpha_bot.risk.costs import adverse_fill_price, fill_fee, maker_queue_fill_price
from pm_alpha_bot.risk.limits import evaluate_signal_risk
from pm_alpha_bot.strategy.signal import generate_signals
from pm_alpha_bot.time import ensure_utc

ReplayFillModel = Literal["optimistic", "pessimistic"]


@dataclass(slots=True)
class ReplayPosition:
    condition_id: str
    token_id: str
    category: str
    size: float
    avg_price: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    event_id: str | None = None


@dataclass(slots=True)
class ReplayOrder:
    order_id: int
    created_at: datetime
    condition_id: str
    token_id: str
    category: str
    side: str
    price: float
    size: float
    edge_bps: float
    event_id: str | None = None
    source_wallets: list[str] = field(default_factory=list)
    filled_size: float = 0.0
    favorable_hits: int = 0
    queue_misses: int = 0
    status: str = "resting"


@dataclass(slots=True)
class ReplayState:
    initial_capital: float
    positions: dict[str, ReplayPosition] = field(default_factory=dict)
    open_orders: list[ReplayOrder] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    edge_samples: list[float] = field(default_factory=list)
    source_wallet_attribution: Counter[str] = field(default_factory=Counter)
    total_trades: int = 0
    next_order_id: int = 1
    fees_total: float = 0.0
    daily_realized_by_date: dict[str, float] = field(default_factory=dict)


def replay_backtest(
    repo: Repository,
    *,
    from_dt: datetime,
    to_dt: datetime,
    category: str = "OVERALL",
    settings: Settings | None = None,
    fill_model: ReplayFillModel = "optimistic",
) -> BacktestReport:
    """Replay stored snapshots and wallet data without lookahead bias."""
    resolved_settings = settings or get_settings()
    snapshots = repo.list_snapshots(start=from_dt, end=to_dt)
    if not snapshots:
        return BacktestReport(
            fill_model=fill_model,
            total_trades=0,
            win_rate=0.0,
            average_edge_bps=0.0,
            realized_pnl=0.0,
            fees_total=0.0,
            unrealized_pnl=0.0,
            max_drawdown=0.0,
            exposure_by_category={},
            top_winning_markets=[],
            top_losing_markets=[],
            source_wallet_attribution={},
        )
    timeline: dict[datetime, list[MarketSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        timeline[ensure_utc(snapshot.ts)].append(snapshot)
    state = ReplayState(initial_capital=resolved_settings.paper_initial_capital)
    current_snapshots: dict[str, MarketSnapshot] = {}
    for ts in sorted(timeline):
        for snapshot in timeline[ts]:
            current_snapshots[snapshot.token_id] = snapshot
        _process_open_orders(
            state,
            current_snapshots,
            ts,
            resolved_settings,
            fill_model=fill_model,
        )
        _mark_to_market(state, current_snapshots)
        for signal in generate_signals(
            repo,
            category=category,
            settings=resolved_settings,
            as_of=ts,
        ):
            _attempt_order(state, signal, repo, current_snapshots, ts, resolved_settings)
        _mark_to_market(state, current_snapshots)
        state.equity_curve.append((ts, _equity(state)))
    market_pnl = {
        token_id: position.realized_pnl + position.unrealized_pnl
        for token_id, position in state.positions.items()
    }
    wins = [pnl for pnl in market_pnl.values() if pnl > 0]
    average_edge = sum(state.edge_samples) / len(state.edge_samples) if state.edge_samples else 0.0
    exposure_by_category = _exposure_by_category(
        state=state,
        snapshots=current_snapshots,
        capital=resolved_settings.paper_initial_capital,
    )
    sorted_pnl = sorted(market_pnl.items(), key=lambda item: item[1], reverse=True)
    return BacktestReport(
        fill_model=fill_model,
        total_trades=state.total_trades,
        win_rate=(len(wins) / len(market_pnl)) if market_pnl else 0.0,
        average_edge_bps=average_edge,
        realized_pnl=sum(position.realized_pnl for position in state.positions.values()),
        fees_total=state.fees_total,
        unrealized_pnl=sum(position.unrealized_pnl for position in state.positions.values()),
        max_drawdown=_max_drawdown(state.equity_curve),
        exposure_by_category=exposure_by_category,
        top_winning_markets=sorted_pnl[:5],
        top_losing_markets=sorted(sorted_pnl[-5:], key=lambda item: item[1]),
        source_wallet_attribution=dict(state.source_wallet_attribution),
    )


def _attempt_order(
    state: ReplayState,
    signal: SignalRecord,
    repo: Repository,
    current_snapshots: dict[str, MarketSnapshot],
    ts: datetime,
    settings: Settings,
) -> None:
    market = repo.get_market_by_condition_id(signal.condition_id)
    snapshot = current_snapshots.get(signal.token_id) or repo.latest_snapshot_for_token(
        signal.token_id,
        as_of=ts,
    )
    if market is None or snapshot is None:
        return
    if signal.token_id in state.positions:
        return
    if any(order.token_id == signal.token_id for order in state.open_orders):
        return
    risk_state = _build_risk_state(state, current_snapshots, settings.paper_initial_capital, ts)
    decision = evaluate_signal_risk(
        signal=signal,
        market=market,
        snapshot=snapshot,
        risk_state=risk_state,
        settings=settings,
        as_of=ts,
    )
    if not decision.passed or signal.effective_entry_price is None or signal.edge_bps is None:
        return
    if snapshot.best_ask is not None and signal.effective_entry_price >= snapshot.best_ask:
        return
    state.edge_samples.append(signal.edge_bps)
    state.source_wallet_attribution.update(signal.source_wallets_json)
    state.open_orders.append(
        ReplayOrder(
            order_id=state.next_order_id,
            created_at=ts,
            condition_id=signal.condition_id,
            token_id=signal.token_id,
            category=market.category or "",
            side=signal.direction,
            price=signal.effective_entry_price,
            size=decision.size_units,
            edge_bps=signal.edge_bps,
            event_id=market.event_id,
            source_wallets=list(signal.source_wallets_json),
        )
    )
    state.next_order_id += 1


def _process_open_orders(
    state: ReplayState,
    current_snapshots: dict[str, MarketSnapshot],
    ts: datetime,
    settings: Settings,
    *,
    fill_model: ReplayFillModel,
) -> None:
    remaining_orders: list[ReplayOrder] = []
    for order in state.open_orders:
        snapshot = current_snapshots.get(order.token_id)
        if snapshot is None:
            remaining_orders.append(order)
            continue
        fill_price = _favorable_fill_price(
            order,
            snapshot,
            settings=settings,
            fill_model=fill_model,
            ts=ts,
        )
        if fill_price is None:
            order.favorable_hits = 0
            remaining_orders.append(order)
            continue
        fill_size = _resolve_fill_size(order, fill_model=fill_model)
        if fill_size is None:
            remaining_orders.append(order)
            continue
        _apply_fill(
            state,
            order,
            fill_price=fill_price,
            fill_size=fill_size,
            ts=ts,
            settings=settings,
        )
        remaining = max(0.0, order.size - order.filled_size)
        if remaining <= 0:
            order.status = "filled"
            continue
        order.status = "partially_filled"
        if fill_model == "pessimistic":
            order.favorable_hits = 0
        remaining_orders.append(order)
        continue
    state.open_orders = remaining_orders


def _favorable_fill_price(
    order: ReplayOrder,
    snapshot: MarketSnapshot,
    *,
    settings: Settings,
    fill_model: ReplayFillModel,
    ts: datetime,
) -> float | None:
    if fill_model == "pessimistic":
        dwell_sec = (ensure_utc(ts) - ensure_utc(order.created_at)).total_seconds()
        if dwell_sec < settings.replay_fill_min_dwell_sec:
            return None
        fill_price = maker_queue_fill_price(
            side=order.side,
            limit_price=order.price,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            queue_miss_bps=settings.replay_queue_miss_bps,
        )
        if fill_price is None:
            order.queue_misses += 1
        return fill_price
    if order.side == "BUY" and snapshot.best_ask is not None and snapshot.best_ask <= order.price:
        return snapshot.best_ask
    if order.side == "SELL" and snapshot.best_bid is not None and snapshot.best_bid >= order.price:
        return snapshot.best_bid
    return None


def _resolve_fill_size(order: ReplayOrder, *, fill_model: ReplayFillModel) -> float | None:
    remaining = max(0.0, order.size - order.filled_size)
    if remaining <= 0:
        return None
    if fill_model == "optimistic":
        return remaining
    order.favorable_hits += 1
    if order.favorable_hits < 2:
        return None
    return remaining if remaining <= 1 else max(remaining * 0.5, 1.0)


def _apply_fill(
    state: ReplayState,
    order: ReplayOrder,
    *,
    fill_price: float,
    fill_size: float,
    ts: datetime,
    settings: Settings,
) -> None:
    position = state.positions.get(order.token_id)
    adjusted_fill_price = adverse_fill_price(
        fill_price,
        order.side,
        settings.replay_slippage_bps,
    )
    fee = fill_fee(adjusted_fill_price, fill_size, settings.replay_fee_bps)
    order.filled_size += fill_size
    state.total_trades += 1
    realized_delta = -fee
    state.fees_total += fee
    if order.side == "BUY":
        if position is None:
            state.positions[order.token_id] = ReplayPosition(
                condition_id=order.condition_id,
                token_id=order.token_id,
                category=order.category,
                size=fill_size,
                avg_price=adjusted_fill_price,
                realized_pnl=realized_delta,
                event_id=order.event_id,
            )
        else:
            new_size = position.size + fill_size
            position.avg_price = (
                (position.avg_price * position.size + adjusted_fill_price * fill_size) / new_size
                if new_size > 0
                else adjusted_fill_price
            )
            position.size = new_size
            position.realized_pnl += realized_delta
    elif order.side == "SELL" and position is not None:
        close_size = min(position.size, fill_size)
        realized_delta = ((adjusted_fill_price - position.avg_price) * close_size) - fee
        position.size -= close_size
        position.realized_pnl += realized_delta
        if position.size <= 0:
            position.unrealized_pnl = 0.0
    day_key = ensure_utc(ts).date().isoformat()
    state.daily_realized_by_date[day_key] = (
        state.daily_realized_by_date.get(day_key, 0.0) + realized_delta
    )


def _mark_to_market(
    state: ReplayState,
    current_snapshots: dict[str, MarketSnapshot],
) -> None:
    for token_id, position in state.positions.items():
        snapshot = current_snapshots.get(token_id)
        if snapshot is None or snapshot.midpoint is None or position.size <= 0:
            position.unrealized_pnl = 0.0
            continue
        position.unrealized_pnl = (snapshot.midpoint - position.avg_price) * position.size


def _build_risk_state(
    state: ReplayState,
    current_snapshots: dict[str, MarketSnapshot],
    capital: float,
    ts: datetime,
) -> RiskState:
    market_exposure_pct: dict[str, float] = {}
    condition_exposure_pct: dict[str, float] = defaultdict(float)
    event_exposure_pct: dict[str, float] = defaultdict(float)
    category_exposure_pct: dict[str, float] = defaultdict(float)
    for token_id, position in state.positions.items():
        snapshot = current_snapshots.get(token_id)
        if snapshot is None or snapshot.midpoint is None or capital == 0:
            continue
        exposure = (snapshot.midpoint * position.size) / capital
        market_exposure_pct[token_id] = exposure
        condition_exposure_pct[position.condition_id] += exposure
        if position.event_id:
            event_exposure_pct[position.event_id] += exposure
        category_exposure_pct[position.category] += exposure
    daily_loss_pct = 0.0
    day_key = ensure_utc(ts).date().isoformat()
    daily_realized = state.daily_realized_by_date.get(day_key, 0.0)
    daily_mark_to_market = daily_realized + sum(
        position.unrealized_pnl for position in state.positions.values()
    )
    if capital > 0 and daily_mark_to_market < 0:
        daily_loss_pct = abs(daily_mark_to_market) / capital
    equity = _equity(state)
    total_drawdown_pct = ((capital - equity) / capital) if capital > 0 and equity < capital else 0.0
    return RiskState(
        capital=capital,
        cash=capital + sum(position.realized_pnl for position in state.positions.values()),
        equity=equity,
        daily_loss_pct=daily_loss_pct,
        total_drawdown_pct=total_drawdown_pct,
        market_exposure_pct=market_exposure_pct,
        condition_exposure_pct=dict(condition_exposure_pct),
        event_exposure_pct=dict(event_exposure_pct),
        category_exposure_pct=dict(category_exposure_pct),
    )


def _equity(state: ReplayState) -> float:
    return state.initial_capital + sum(
        position.realized_pnl + position.unrealized_pnl for position in state.positions.values()
    )


def _max_drawdown(curve: list[tuple[datetime, float]]) -> float:
    peak = 0.0
    max_drawdown = 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        if peak == 0:
            continue
        drawdown = (peak - equity) / peak
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def _exposure_by_category(
    *,
    state: ReplayState,
    snapshots: dict[str, MarketSnapshot],
    capital: float,
) -> dict[str, float]:
    exposure: dict[str, float] = defaultdict(float)
    if capital <= 0:
        return {}
    for token_id, position in state.positions.items():
        snapshot = snapshots.get(token_id)
        if snapshot is None or snapshot.midpoint is None:
            continue
        exposure[position.category] += (snapshot.midpoint * position.size) / capital
    return dict(exposure)

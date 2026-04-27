from __future__ import annotations

from pm_alpha_bot.scoring.metrics import clamp


def adverse_fill_price(price: float, side: str, slippage_bps: float) -> float:
    """Apply a simple adverse slippage adjustment to a fill price."""
    if price <= 0:
        return 0.0
    if slippage_bps <= 0:
        return clamp(price, 0.0, 1.0)
    adjustment = price * (slippage_bps / 10_000)
    resolved_side = side.upper()
    slipped = price + adjustment if resolved_side == "BUY" else price - adjustment
    return clamp(slipped, 0.0, 1.0)


def fill_fee(price: float, size: float, fee_bps: float) -> float:
    """Return a simple notional-based fee estimate."""
    if price <= 0 or size <= 0 or fee_bps <= 0:
        return 0.0
    return price * size * (fee_bps / 10_000)


def maker_queue_fill_price(
    *,
    side: str,
    limit_price: float | None,
    best_bid: float | None,
    best_ask: float | None,
    queue_miss_bps: float,
) -> float | None:
    """Return a maker fill price only after the book moves through the limit."""
    if limit_price is None:
        return None
    buffer = max(queue_miss_bps, 0.0) / 10_000
    resolved_side = side.upper()
    if resolved_side == "BUY" and best_ask is not None:
        return best_ask if best_ask <= limit_price - buffer else None
    if resolved_side == "SELL" and best_bid is not None:
        return best_bid if best_bid >= limit_price + buffer else None
    return None

from __future__ import annotations

from pm_alpha_bot.scoring.metrics import clamp


def kelly_fraction_binary(fair_prob: float, entry_price: float) -> float:
    """Return the Kelly fraction approximation for a binary contract."""
    if entry_price >= 1:
        return 0.0
    return max(0.0, (fair_prob - entry_price) / (1 - entry_price))


def position_size_pct(
    fair_prob: float,
    entry_price: float,
    kelly_fraction_scale: float,
    max_market_exposure_pct: float,
    confidence: float | None = None,
) -> float:
    """Return the portfolio allocation percentage for an order."""
    raw = kelly_fraction_binary(fair_prob, entry_price) * kelly_fraction_scale
    if confidence is not None:
        raw *= clamp(confidence, 0.0, 1.0)
    return clamp(raw, 0.0, max_market_exposure_pct)


def order_size_units(capital: float, size_pct: float, entry_price: float) -> float:
    """Convert allocation percentage to token units."""
    if entry_price <= 0:
        return 0.0
    notional = capital * size_pct
    return notional / entry_price

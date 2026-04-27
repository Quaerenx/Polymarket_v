from __future__ import annotations

from collections.abc import Iterable


def clamp(value: float, lower: float, upper: float) -> float:
    """Clamp a numeric value to a range."""
    return max(lower, min(upper, value))


def safe_ratio(numerator: float | None, denominator: float | None, default: float = 0.0) -> float:
    """Return a safe division result."""
    if numerator is None or denominator is None or denominator == 0:
        return default
    return numerator / denominator


def neutral_if_none(value: float | None, neutral: float = 0.5) -> float:
    """Replace missing values with a neutral score."""
    return neutral if value is None else value


def min_max_normalize(value: float | None, values: list[float], inverse: bool = False) -> float:
    """Min-max normalize a metric into `[0, 1]` or return neutral if not possible."""
    if value is None or not values:
        return 0.5
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return 0.5
    score = (value - lo) / (hi - lo)
    if inverse:
        score = 1 - score
    return clamp(score, 0.0, 1.0)


def mean(values: Iterable[float]) -> float | None:
    """Return the arithmetic mean or `None` for empty sequences."""
    seq = list(values)
    if not seq:
        return None
    return sum(seq) / len(seq)


def profit_concentration(values: list[float]) -> float | None:
    """Estimate how concentrated profits are in the largest winning trade."""
    positives = [value for value in values if value > 0]
    if not positives:
        return None
    total = sum(positives)
    if total == 0:
        return None
    return max(positives) / total


def max_drawdown_from_pnl_series(values: list[float]) -> float | None:
    """Compute max drawdown from a cumulative PnL series."""
    if not values:
        return None
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown

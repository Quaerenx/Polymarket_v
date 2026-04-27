from __future__ import annotations

from pm_alpha_bot.db.models import MarketSnapshot
from pm_alpha_bot.scoring.metrics import clamp


def maker_limit_price(snapshot: MarketSnapshot, tick_size: float) -> float:
    """Return a maker-first limit price between the spread."""
    midpoint = snapshot.midpoint if snapshot.midpoint is not None else 0.5
    if snapshot.best_ask is None:
        return clamp(midpoint, 0.01, 0.99)
    return clamp(min(snapshot.best_ask - tick_size, midpoint), 0.01, 0.99)

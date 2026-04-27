from __future__ import annotations


def normalize_market_category(value: str | None) -> str | None:
    """Normalize a market category for case-insensitive comparisons."""
    normalized = (value or "").strip()
    if not normalized:
        return None
    return normalized.casefold()


def market_category_matches(value: str | None, expected: str | None) -> bool:
    """Return true when a market category matches the configured filter."""
    normalized_expected = normalize_market_category(expected)
    if normalized_expected is None:
        return True
    return normalize_market_category(value) == normalized_expected

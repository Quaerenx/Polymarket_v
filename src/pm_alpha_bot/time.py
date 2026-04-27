from __future__ import annotations

from datetime import UTC, datetime, timedelta


def ensure_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_cli_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse a CLI date or datetime string into a UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if "T" not in value and " " not in value:
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
        if end_of_day:
            parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
    return ensure_utc(parsed)

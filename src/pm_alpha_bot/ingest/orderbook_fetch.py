from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from pm_alpha_bot.clients.base import ApiError
from pm_alpha_bot.clients.clob_public import ClobPublicClient
from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.domain import OrderBookSnapshot
from pm_alpha_bot.time import ensure_utc

MISSING_ORDERBOOK_CACHE_KEY = "missing_orderbooks"


def _dedupe_preserve_order(token_ids: list[str]) -> list[str]:
    return list(dict.fromkeys(token_id for token_id in token_ids if token_id))


def _load_missing_orderbook_cache(repo: Repository) -> dict[str, str]:
    state = repo.get_runtime_state(MISSING_ORDERBOOK_CACHE_KEY)
    payload = state.state_json if state is not None else None
    if not isinstance(payload, dict):
        return {}
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return {}
    return {
        str(token_id): str(ts)
        for token_id, ts in tokens.items()
        if isinstance(token_id, str) and isinstance(ts, str)
    }


def _prune_missing_orderbook_cache(
    cache: dict[str, str],
    *,
    as_of: datetime,
    ttl_hours: int,
) -> dict[str, str]:
    cutoff = as_of - timedelta(hours=ttl_hours)
    pruned: dict[str, str] = {}
    for token_id, raw_ts in cache.items():
        try:
            ts = ensure_utc(datetime.fromisoformat(raw_ts.replace("Z", "+00:00")))
        except ValueError:
            continue
        if ts >= cutoff:
            pruned[token_id] = ts.isoformat()
    return pruned


def _save_missing_orderbook_cache(
    repo: Repository,
    cache: dict[str, str],
    *,
    as_of: datetime,
) -> None:
    repo.upsert_runtime_state(
        MISSING_ORDERBOOK_CACHE_KEY,
        {
            "updated_at": as_of.isoformat(),
            "tokens": cache,
        },
    )


def current_missing_orderbook_cache(
    repo: Repository,
    *,
    settings: Settings | None = None,
    as_of: datetime | None = None,
) -> set[str]:
    """Return token IDs currently cached as missing orderbooks."""
    resolved_settings = settings or get_settings()
    resolved_as_of = as_of or datetime.now(UTC)
    cache = _prune_missing_orderbook_cache(
        _load_missing_orderbook_cache(repo),
        as_of=resolved_as_of,
        ttl_hours=resolved_settings.missing_orderbook_cache_ttl_hours,
    )
    return set(cache)


def is_missing_orderbook_cached(
    repo: Repository,
    *,
    token_id: str,
    settings: Settings | None = None,
    as_of: datetime | None = None,
) -> bool:
    """Return true when the token is currently cached as a missing orderbook."""
    return token_id in current_missing_orderbook_cache(
        repo,
        settings=settings,
        as_of=as_of,
    )


async def fetch_latest_orderbook_snapshots(
    repo: Repository,
    *,
    token_ids: list[str],
    settings: Settings | None = None,
    as_of: datetime | None = None,
) -> tuple[list[OrderBookSnapshot], dict[str, int]]:
    """Fetch orderbooks while skipping recently missing tokens via runtime-state cache."""
    resolved_settings = settings or get_settings()
    resolved_as_of = as_of or datetime.now(UTC)
    unique_token_ids = _dedupe_preserve_order(token_ids)
    cache = _prune_missing_orderbook_cache(
        _load_missing_orderbook_cache(repo),
        as_of=resolved_as_of,
        ttl_hours=resolved_settings.missing_orderbook_cache_ttl_hours,
    )

    attempted_token_ids: list[str] = []
    cached_missing = 0
    for token_id in unique_token_ids:
        if token_id in cache:
            cached_missing += 1
            continue
        attempted_token_ids.append(token_id)

    snapshots: list[OrderBookSnapshot] = []
    missing_404: list[str] = []
    if attempted_token_ids:
        async with ClobPublicClient(settings=resolved_settings) as client:
            for token_id in attempted_token_ids:
                try:
                    snapshots.append(await client.get_orderbook(token_id))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        missing_404.append(token_id)
                        continue
                    raise
                except (httpx.HTTPError, ApiError):
                    continue

    for token_id in missing_404:
        cache[token_id] = resolved_as_of.isoformat()
    for snapshot in snapshots:
        cache.pop(snapshot.token_id, None)
    _save_missing_orderbook_cache(repo, cache, as_of=resolved_as_of)
    return snapshots, {
        "requested": len(unique_token_ids),
        "attempted": len(attempted_token_ids),
        "cached_missing": cached_missing,
        "missing_404": len(missing_404),
    }

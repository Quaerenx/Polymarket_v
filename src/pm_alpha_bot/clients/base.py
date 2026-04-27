from __future__ import annotations

import asyncio
from typing import Any, Self

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.logging import get_logger

logger = get_logger(__name__)


class ApiError(RuntimeError):
    """Raised when a remote API returns an invalid or error response."""


def _is_retryable_exception(exc: BaseException) -> bool:
    """Return true when a request failure is worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
        return False
    return isinstance(exc, (httpx.HTTPError, ApiError))


class BaseApiClient:
    """Async JSON HTTP client with retry and structured logging."""

    def __init__(self, base_url: str, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base_url, timeout=self.settings.http_timeout_sec
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.client.aclose()

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        """Perform a GET request and return parsed JSON."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            retry=retry_if_exception(_is_retryable_exception),
            reraise=True,
        ):
            with attempt:
                if self.settings.http_rate_limit_delay_sec > 0:
                    await asyncio.sleep(self.settings.http_rate_limit_delay_sec)
                response = await self.client.get(path, params=params)
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "http_status_error",
                        extra={"path": path, "status_code": exc.response.status_code},
                    )
                    raise
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ApiError(f"Failed to parse JSON from {path}") from exc
                if raw:
                    return {"payload": payload, "status_code": response.status_code}
                return payload
        raise ApiError(f"Request failed after retries: {path}")

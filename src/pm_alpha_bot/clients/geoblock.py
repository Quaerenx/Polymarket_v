from __future__ import annotations

from pm_alpha_bot.clients.base import BaseApiClient
from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.domain import GeoblockStatus


class GeoblockClient(BaseApiClient):
    """Client for Polymarket geoblock checks."""

    def __init__(self, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        super().__init__("https://polymarket.com", settings=resolved)

    async def check(self) -> GeoblockStatus:
        """Return the current geoblock status."""
        payload = await self.get_json("/api/geoblock")
        return GeoblockStatus(blocked=bool(payload.get("blocked", False)), raw_json=payload)

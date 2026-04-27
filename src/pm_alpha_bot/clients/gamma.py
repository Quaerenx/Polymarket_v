from __future__ import annotations

from datetime import datetime
from typing import Any

from pm_alpha_bot.clients.base import BaseApiClient
from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.domain import NormalizedMarket, NormalizedMarketToken


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GammaClient(BaseApiClient):
    """Public Gamma API client."""

    def __init__(self, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        super().__init__(resolved.poly_gamma_host, settings=resolved)

    async def list_markets(
        self, limit: int = 100, active_only: bool = True
    ) -> list[NormalizedMarket]:
        """Fetch and normalize market metadata."""
        params: dict[str, Any] = {"limit": limit}
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"
        payload = await self.get_json("/markets", params=params)
        return [self._normalize_market(item) for item in payload]

    def _normalize_market(self, payload: dict[str, Any]) -> NormalizedMarket:
        tokens: list[NormalizedMarketToken] = []
        token_ids = payload.get("clobTokenIds")
        outcomes = payload.get("outcomes")
        token_list: list[str] = []
        outcome_list: list[str] = []
        if isinstance(token_ids, str):
            token_list = [
                token.strip()
                for token in token_ids.strip("[]").replace('"', "").split(",")
                if token.strip()
            ]
        elif isinstance(token_ids, list):
            token_list = [str(token) for token in token_ids]
        if isinstance(outcomes, str):
            outcome_list = [
                item.strip()
                for item in outcomes.strip("[]").replace('"', "").split(",")
                if item.strip()
            ]
        elif isinstance(outcomes, list):
            outcome_list = [str(item) for item in outcomes]
        for index, token_id in enumerate(token_list):
            outcome = outcome_list[index] if index < len(outcome_list) else None
            tokens.append(
                NormalizedMarketToken(
                    condition_id=str(
                        payload.get("conditionId") or payload.get("questionID") or payload["id"]
                    ),
                    token_id=token_id,
                    outcome=outcome,
                    side_label=outcome,
                )
            )
        return NormalizedMarket(
            condition_id=str(
                payload.get("conditionId") or payload.get("questionID") or payload["id"]
            ),
            question=str(payload.get("question") or payload.get("title") or payload["id"]),
            market_id=str(payload.get("id")) if payload.get("id") is not None else None,
            event_id=str(payload.get("events", [{}])[0].get("id"))
            if payload.get("events")
            else None,
            category=payload.get("category"),
            slug=payload.get("slug"),
            end_date=_parse_datetime(payload.get("endDate") or payload.get("endDateIso")),
            active=bool(payload.get("active", True)),
            closed=bool(payload.get("closed", False)),
            neg_risk=bool(payload.get("negRisk") or payload.get("negativeRisk") or False),
            min_tick_size=_to_float(payload.get("orderPriceMinTickSize")),
            min_order_size=_to_float(payload.get("orderMinSize")),
            tokens=tokens,
            raw_json=payload,
        )


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

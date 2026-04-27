from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.logging import get_logger

logger = get_logger(__name__)


def send_alert(
    event_type: str,
    message: str,
    *,
    payload: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> bool:
    """Send a best-effort JSON alert to the configured webhook."""
    resolved = settings or get_settings()
    webhook_url = resolved.alert_webhook_url.strip()
    if not webhook_url:
        return False
    body = {
        "event_type": event_type,
        "message": message,
        "payload": payload or {},
        "ts": datetime.now(UTC).isoformat(),
        "app_env": resolved.app_env,
    }
    try:
        response = httpx.post(
            webhook_url,
            json=body,
            timeout=resolved.alert_webhook_timeout_sec,
        )
        response.raise_for_status()
    except Exception as exc:  # pragma: no cover - network failure path
        logger.warning(
            "alert_delivery_failed",
            extra={"event_type": event_type, "error": str(exc)},
        )
        return False
    logger.info(
        "alert_delivered",
        extra={"event_type": event_type, "status_code": response.status_code},
    )
    return True

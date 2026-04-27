from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.time import ensure_utc


def evaluate_healthcheck(
    repo: Repository,
    *,
    settings: Settings | None = None,
    require_live_supervisor: bool = False,
    max_supervisor_age_sec: float | None = None,
    fail_on_kill_switch: bool = False,
    fail_on_supervisor_error: bool = False,
) -> dict[str, Any]:
    """Evaluate runtime health and return a machine-friendly summary."""
    resolved = settings or get_settings()
    now = datetime.now(UTC)
    repo.session.execute(text("SELECT 1"))

    needs_live_state = (
        require_live_supervisor
        or fail_on_kill_switch
        or fail_on_supervisor_error
    )
    state = repo.live_operations_state() if needs_live_state else {}
    supervisor = state.get("supervisor")
    kill_switch = state.get("kill_switch")
    default_max_age = max(15.0, resolved.live_heartbeat_interval_sec * 3)
    resolved_max_age = max_supervisor_age_sec or default_max_age

    failures: list[str] = []
    supervisor_age_sec: float | None = None
    supervisor_state = supervisor if isinstance(supervisor, dict) else None
    kill_switch_state = kill_switch if isinstance(kill_switch, dict) else None

    if fail_on_kill_switch and kill_switch_state and bool(kill_switch_state.get("tripped", False)):
        failures.append("kill_switch_tripped")

    if require_live_supervisor:
        if supervisor_state is None:
            failures.append("missing_supervisor_state")
        else:
            last_iteration_at = _parse_iso_datetime(supervisor_state.get("last_iteration_at"))
            if last_iteration_at is None:
                failures.append("missing_last_iteration_at")
            else:
                supervisor_age_sec = (now - last_iteration_at).total_seconds()
                if supervisor_age_sec > resolved_max_age:
                    failures.append("stale_supervisor_state")
            status = str(supervisor_state.get("status") or "unknown")
            if fail_on_supervisor_error and status != "ok":
                failures.append(f"supervisor_status:{status}")

    return {
        "ok": not failures,
        "ts": now.isoformat(),
        "db_ok": True,
        "failures": failures,
        "require_live_supervisor": require_live_supervisor,
        "max_supervisor_age_sec": resolved_max_age,
        "supervisor_age_sec": supervisor_age_sec,
        "supervisor": supervisor_state,
        "kill_switch": kill_switch_state,
    }


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None

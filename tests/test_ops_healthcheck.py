from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pm_alpha_bot.ops.healthcheck import evaluate_healthcheck


def test_healthcheck_passes_db_only(repo, sqlite_settings) -> None:
    report = evaluate_healthcheck(repo, settings=sqlite_settings)

    assert report["ok"] is True
    assert report["db_ok"] is True
    assert report["failures"] == []


def test_healthcheck_fails_when_live_supervisor_missing(repo, sqlite_settings) -> None:
    report = evaluate_healthcheck(
        repo,
        settings=sqlite_settings,
        require_live_supervisor=True,
    )

    assert report["ok"] is False
    assert "missing_supervisor_state" in report["failures"]


def test_healthcheck_fails_for_stale_supervisor_and_kill_switch(repo, sqlite_settings) -> None:
    now = datetime.now(UTC)
    repo.upsert_runtime_state(
        "live_supervisor",
        {
            "status": "error",
            "last_iteration_at": (now - timedelta(seconds=120)).isoformat(),
        },
    )
    repo.upsert_runtime_state(
        "live_kill_switch",
        {
            "tripped": True,
            "reason": "operator stop",
            "tripped_at": now.isoformat(),
        },
    )
    repo.session.commit()

    report = evaluate_healthcheck(
        repo,
        settings=sqlite_settings,
        require_live_supervisor=True,
        max_supervisor_age_sec=30,
        fail_on_kill_switch=True,
        fail_on_supervisor_error=True,
    )

    assert report["ok"] is False
    assert "kill_switch_tripped" in report["failures"]
    assert "stale_supervisor_state" in report["failures"]
    assert "supervisor_status:error" in report["failures"]

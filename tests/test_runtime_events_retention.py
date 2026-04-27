from __future__ import annotations

from datetime import UTC, datetime, timedelta


def test_prune_runtime_events_dry_run_keeps_rows(repo) -> None:
    now = datetime.now(UTC)
    repo.add_runtime_event(
        category="live",
        event_type="old_event",
        ts=now - timedelta(days=40),
    )
    repo.add_runtime_event(
        category="live",
        event_type="new_event",
        ts=now - timedelta(days=5),
    )
    repo.session.commit()

    count = repo.prune_runtime_events(
        older_than=now - timedelta(days=30),
        category="live",
        dry_run=True,
    )

    remaining = repo.list_runtime_events(category="live", limit=10)
    assert count == 1
    assert {event.event_type for event in remaining} == {"old_event", "new_event"}


def test_prune_runtime_events_apply_respects_category_filter(repo) -> None:
    now = datetime.now(UTC)
    repo.add_runtime_event(
        category="live",
        event_type="old_live_event",
        ts=now - timedelta(days=45),
    )
    repo.add_runtime_event(
        category="paper",
        event_type="old_paper_event",
        ts=now - timedelta(days=45),
    )
    repo.add_runtime_event(
        category="live",
        event_type="new_live_event",
        ts=now - timedelta(days=2),
    )
    repo.session.commit()

    count = repo.prune_runtime_events(
        older_than=now - timedelta(days=30),
        category="live",
        dry_run=False,
    )

    live_events = repo.list_runtime_events(category="live", limit=10)
    paper_events = repo.list_runtime_events(category="paper", limit=10)
    assert count == 1
    assert {event.event_type for event in live_events} == {"new_live_event"}
    assert {event.event_type for event in paper_events} == {"old_paper_event"}

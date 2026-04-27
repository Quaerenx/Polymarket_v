from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from pm_alpha_bot.db.models import (
    Market,
    MarketSnapshot,
    MarketToken,
    Order,
    PaperPosition,
    Signal,
    WalletActivity,
    WalletScore,
)
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.web import console as console_web
from pm_alpha_bot.web.console import OpsConsoleServer, create_console_server


def _seed_console_rows(repo: Repository) -> None:
    now = datetime.now(UTC)
    repo.upsert_runtime_state(
        "live_supervisor",
        {
            "status": "ok",
            "heartbeat_id": "hb-1",
            "last_iteration_at": now.isoformat(),
            "last_success_at": now.isoformat(),
            "consecutive_errors": 0,
        },
    )
    repo.upsert_runtime_state(
        "live_kill_switch",
        {
            "tripped": True,
            "reason": "test reason",
            "tripped_at": now.isoformat(),
            "consecutive_errors": 3,
        },
    )
    repo.upsert_runtime_state(
        "signal_observation_watch:OVERALL",
        {
            "category": "OVERALL",
            "updated_at": now.isoformat(),
            "tokens": {
                "token-obs": {
                    "token_id": "token-obs",
                    "observation_streak": 3,
                    "first_seen_at": (now - timedelta(minutes=15)).isoformat(),
                    "last_seen_at": now.isoformat(),
                }
            },
        },
    )
    repo.add_runtime_event(
        category="live",
        event_type="live_supervisor_iteration",
        message="Supervisor iteration ok.",
        ts=now,
        event_json={
            "submitted": False,
            "skipped_submission_reason": "persistent_kill_switch_active",
            "active_orders": 0,
        },
    )
    repo.add_runtime_event(
        category="live",
        event_type="old_event",
        message="Old event for prune testing.",
        ts=now - timedelta(days=40),
    )
    repo.add_runtime_event(
        category="signal",
        event_type="signal_observation_queued",
        level="warning",
        message="Observation candidate token-obs entered the promotion queue.",
        ts=now - timedelta(seconds=30),
        event_json={"token_id": "token-obs", "observation_streak": 3},
    )
    repo.session.add(
        WalletScore(
            proxy_wallet="0xwallet1",
            category="OVERALL",
            as_of=now,
            score=0.81,
            roi=0.12,
            pnl=42.0,
            closed_market_count=45,
            trade_count=88,
            profit_concentration=0.22,
            raw_metrics_json={"recent_30d_pnl": 42.0},
        )
    )
    repo.session.add(
        Market(
            condition_id="condition-obs",
            question="Observation market",
            category="OVERALL",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
        )
    )
    repo.session.add(
        MarketToken(
            condition_id="condition-obs",
            token_id="token-obs",
            outcome="Yes",
            side_label="Yes",
        )
    )
    repo.session.add(
        Market(
            condition_id="condition-2",
            question="Paper position market",
            category="OVERALL",
            active=True,
            closed=False,
            min_tick_size=0.01,
            min_order_size=1.0,
        )
    )
    repo.session.add(
        MarketToken(
            condition_id="condition-2",
            token_id="token-2",
            outcome="Yes",
            side_label="Yes",
        )
    )
    repo.session.add(
        WalletActivity(
            proxy_wallet="0xwallet1",
            condition_id="condition-obs",
            token_id="token-obs",
            side="buy",
            price=0.4,
            size=5.0,
            ts=now - timedelta(minutes=1),
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=now - timedelta(seconds=10),
            condition_id="condition-obs",
            token_id="token-obs",
            best_bid=None,
            best_ask=None,
            midpoint=None,
            spread=None,
            last_trade_price=0.47,
            liquidity_score=0.0,
            raw_json={"bids": [], "asks": []},
        )
    )
    repo.session.add(
        MarketSnapshot(
            ts=now - timedelta(seconds=5),
            condition_id="condition-2",
            token_id="token-2",
            best_bid=0.42,
            best_ask=0.44,
            midpoint=0.43,
            spread=0.02,
            last_trade_price=0.43,
            liquidity_score=5.0,
            raw_json={"bids": [["0.42", "10"]], "asks": [["0.44", "10"]]},
        )
    )
    repo.session.add(
        Signal(
            ts=now,
            condition_id="condition-1",
            token_id="token-1",
            direction="BUY",
            effective_entry_price=0.44,
            fair_prob=0.58,
            edge_bps=1400.0,
            confidence=0.66,
            source_wallet_count=3,
            status="new",
            reason_json={"source": "test"},
        )
    )
    repo.session.add(
        Order(
            mode="live",
            condition_id="condition-1",
            token_id="token-1",
            side="BUY",
            price=0.44,
            size=12.0,
            order_type="GTC",
            status="resting",
            external_order_id="ext-1",
            reason_json={"seed": True},
        )
    )
    repo.session.add(
        Order(
            created_at=now - timedelta(seconds=20),
            mode="live",
            condition_id="condition-1",
            token_id="token-1",
            side="BUY",
            price=0.45,
            size=10.0,
            order_type="GTC",
            status="rejected",
            external_order_id="ext-rejected",
            reason_json={"reject_reason": "post_only_would_cross_ask"},
        )
    )
    repo.session.add(
        PaperPosition(
            condition_id="condition-2",
            token_id="token-2",
            side="LONG",
            size=5.0,
            avg_price=0.41,
            realized_pnl=3.5,
            unrealized_pnl=1.2,
        )
    )
    repo.session.commit()


def _start_server(*, settings, enable_actions: bool) -> tuple[OpsConsoleServer, Thread, str]:
    server = create_console_server(
        settings=settings,
        host="127.0.0.1",
        port=0,
        enable_actions=enable_actions,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    return server, thread, base_url


def _stop_server(server: OpsConsoleServer, thread: Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _basic_auth_headers(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, object]:
    request = Request(url, headers=headers or {}, method="GET")
    with urlopen(request, timeout=5) as response:
        assert response.status == 200
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def _get_text(url: str, headers: dict[str, str] | None = None) -> str:
    request = Request(url, headers=headers or {}, method="GET")
    with urlopen(request, timeout=5) as response:
        assert response.status == 200
        return response.read().decode("utf-8")


def _get_sse_event(
    url: str,
    headers: dict[str, str] | None = None,
) -> tuple[str, dict[str, object]]:
    request = Request(url, headers=headers or {}, method="GET")
    with urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")
        event_name = ""
        data_lines: list[str] = []
        for _ in range(80):
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                if data_lines:
                    break
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
        assert event_name
        assert data_lines
        return event_name, json.loads("\n".join(data_lines))


def _post_json(
    url: str,
    body: dict[str, object],
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_console_index_and_api(repo: Repository, sqlite_settings) -> None:
    _seed_console_rows(repo)
    server, thread, base_url = _start_server(settings=sqlite_settings, enable_actions=False)
    try:
        html = _get_text(f"{base_url}/")
        payload = _get_json(f"{base_url}/api/console")
    finally:
        _stop_server(server, thread)

    assert "PM Alpha Bot Console" in html
    assert "IBM Plex Mono" in html
    assert 'id="theme-toggle"' in html
    assert "다크 모드" in html
    assert 'id="stream-status"' in html
    assert 'id="operator-summary"' in html
    assert 'id="position-risk-list"' in html
    assert 'id="service-heartbeats-table"' in html
    assert 'id="execution-reject-summary-table"' in html
    assert 'new EventSource("/events")' in html
    assert payload["context"]["actions_enabled"] is False
    assert payload["context"]["auth_enabled"] is False
    assert payload["summary"]["run_mode"] == "KILL SWITCH"
    assert payload["summary"]["primary_status"] == "PERSISTENT LIVE KILL SWITCH ACTIVE"
    operator_summary = payload["summary"]["operator_summary"]
    assert operator_summary[0]["label"] == "MODE"
    assert operator_summary[1]["label"] == "총자산"
    assert operator_summary[2]["label"] == "현금"
    assert operator_summary[3]["label"] == "포지션 슬롯"
    assert operator_summary[4]["label"] == "다음 진입"
    assert operator_summary[5]["value"] == "1 READY"
    assert operator_summary[6]["value"] == "LIVE OFF"
    assert payload["position_risks"][0]["mode"] == "paper"
    assert payload["position_risks"][0]["token_id"] == "token-2"
    assert payload["position_risks"][0]["entry_price"] == pytest.approx(0.41)
    assert payload["position_risks"][0]["current_price"] == pytest.approx(0.43)
    assert payload["position_risks"][0]["target_price"] > 0.41
    assert payload["position_risks"][0]["stop_price"] < 0.41
    assert payload["position_risks"][0]["stop_distance_pct"] > 0
    heartbeats = payload["service_heartbeats"]
    heartbeat_by_component = {row["component"]: row for row in heartbeats}
    assert {
        "scanner",
        "wallet-tracker",
        "wallet-scoring",
        "stream",
        "signal",
        "exec",
        "live-supervisor",
    } <= set(heartbeat_by_component)
    assert heartbeat_by_component["scanner"]["status"] == "ok"
    assert heartbeat_by_component["wallet-tracker"]["status"] == "ok"
    assert heartbeat_by_component["wallet-scoring"]["status"] == "ok"
    assert heartbeat_by_component["stream"]["status"] == "ok"
    assert heartbeat_by_component["signal"]["status"] == "ok"
    assert heartbeat_by_component["exec"]["status"] == "ok"
    assert heartbeat_by_component["live-supervisor"]["status"] == "ok"
    execution_rejects = payload["execution_rejects"]
    blocker_reasons = {row["reason"] for row in execution_rejects["current_blockers"]}
    assert "live_trading_disabled" in blocker_reasons
    assert "persistent_kill_switch_active" in blocker_reasons
    summary_reasons = {row["reason"] for row in execution_rejects["summary"]}
    assert "persistent_kill_switch_active" in summary_reasons
    assert "post_only_would_cross_ask" in summary_reasons
    recent_reasons = {row["reason"] for row in execution_rejects["recent"]}
    assert "persistent_kill_switch_active" in recent_reasons
    assert "post_only_would_cross_ask" in recent_reasons
    assert payload["signals"][0]["token_id"] == "token-1"
    assert payload["signal_observations"][0]["token_id"] == "token-obs"
    assert payload["signal_observations"][0]["promotion_ready"] is True
    assert payload["signal_observations"][0]["observation_streak"] == 3
    assert payload["signal_promotion_queue"][0]["token_id"] == "token-obs"
    assert payload["signal_events"][0]["event_type"] == "signal_observation_queued"
    assert payload["wallets"][0]["proxy_wallet"] == "0xwallet1"
    assert payload["paper_report"]["positions"][0]["token_id"] == "token-2"


def test_console_events_stream_returns_console_payload(repo: Repository, sqlite_settings) -> None:
    _seed_console_rows(repo)
    server, thread, base_url = _start_server(settings=sqlite_settings, enable_actions=False)
    try:
        event_name, payload = _get_sse_event(f"{base_url}/events")
    finally:
        _stop_server(server, thread)

    assert event_name == "console"
    assert payload["context"]["actions_enabled"] is False
    assert payload["summary"]["run_mode"] == "KILL SWITCH"
    assert payload["signals"][0]["token_id"] == "token-1"


def test_console_actions_are_disabled_by_default(repo: Repository, sqlite_settings) -> None:
    _seed_console_rows(repo)
    server, thread, base_url = _start_server(settings=sqlite_settings, enable_actions=False)
    try:
        status, payload = _post_json(f"{base_url}/api/actions/reset-live-kill-switch", {})
    finally:
        _stop_server(server, thread)

    assert status == 403
    assert payload["ok"] is False


def test_console_actions_execute_when_enabled(
    repo: Repository,
    sqlite_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_console_rows(repo)

    def _fake_sync_live_orders(
        repo_arg: Repository,
        *,
        settings,
        limit: int,
    ) -> dict[str, object]:
        assert repo_arg is not None
        assert settings is sqlite_settings
        return {"orders_synced": limit, "fills_synced": 0, "order_statuses": {}}

    monkeypatch.setattr(console_web, "sync_live_orders", _fake_sync_live_orders)
    server, thread, base_url = _start_server(settings=sqlite_settings, enable_actions=True)
    try:
        sync_status, sync_payload = _post_json(f"{base_url}/api/actions/sync-live", {"limit": 7})
        preview_status, preview_payload = _post_json(
            f"{base_url}/api/actions/prune-runtime-events",
            {"older_than_days": 30, "apply": False},
        )
        apply_status, apply_payload = _post_json(
            f"{base_url}/api/actions/prune-runtime-events",
            {"older_than_days": 30, "apply": True},
        )
        reset_status, reset_payload = _post_json(
            f"{base_url}/api/actions/reset-live-kill-switch",
            {
                "acknowledge": True,
                "reason": "test_operator_confirmed_reset",
                "allow_active_orders": True,
            },
        )
    finally:
        _stop_server(server, thread)

    assert sync_status == 200
    assert sync_payload["result"]["orders_synced"] == 7
    assert preview_status == 200
    assert preview_payload["result"]["count"] == 1
    assert apply_status == 200
    assert apply_payload["result"]["count"] == 1
    assert reset_status == 200
    assert reset_payload["result"]["tripped"] is False
    assert reset_payload["result"]["reset_acknowledged"] is True
    assert reset_payload["result"]["reset_reason"] == "test_operator_confirmed_reset"

    repo.session.expire_all()
    kill_switch = repo.get_runtime_state("live_kill_switch")
    assert kill_switch is not None
    assert kill_switch.state_json["tripped"] is False
    remaining_old_events = repo.list_runtime_events(
        category="live",
        event_type="old_event",
        limit=10,
    )
    assert remaining_old_events == []


def test_console_rejects_non_loopback_actions(sqlite_settings) -> None:
    with pytest.raises(ValueError, match="Refusing to enable remote actions"):
        create_console_server(
            settings=sqlite_settings,
            host="0.0.0.0",
            port=0,
            enable_actions=True,
        )


def test_console_requires_basic_auth_when_configured(
    repo: Repository,
    sqlite_settings,
) -> None:
    _seed_console_rows(repo)
    settings = sqlite_settings.model_copy(
        update={
            "console_auth_username": "operator",
            "console_auth_password": "secret-pass",
        }
    )
    server, thread, base_url = _start_server(settings=settings, enable_actions=False)
    try:
        with pytest.raises(HTTPError) as exc_info:
            _get_json(f"{base_url}/api/console")
        with pytest.raises(HTTPError) as events_exc_info:
            _get_sse_event(f"{base_url}/events")
        payload = _get_json(
            f"{base_url}/api/console",
            headers=_basic_auth_headers("operator", "secret-pass"),
        )
        event_name, event_payload = _get_sse_event(
            f"{base_url}/events",
            headers=_basic_auth_headers("operator", "secret-pass"),
        )
    finally:
        _stop_server(server, thread)

    assert exc_info.value.code == 401
    assert events_exc_info.value.code == 401
    assert exc_info.value.headers["WWW-Authenticate"].startswith("Basic ")
    assert json.loads(exc_info.value.read().decode("utf-8"))["error"] == "Authentication required."
    assert payload["context"]["auth_enabled"] is True
    assert payload["summary"]["run_mode"] == "KILL SWITCH"
    assert event_name == "console"
    assert event_payload["context"]["auth_enabled"] is True


def test_console_rejects_incomplete_auth_config(sqlite_settings) -> None:
    settings = sqlite_settings.model_copy(update={"console_auth_username": "operator"})
    with pytest.raises(ValueError, match="Console auth requires both"):
        create_console_server(
            settings=settings,
            host="127.0.0.1",
            port=0,
            enable_actions=False,
        )


def test_console_rejects_remote_actions_without_auth(sqlite_settings) -> None:
    with pytest.raises(ValueError, match="without console authentication"):
        create_console_server(
            settings=sqlite_settings,
            host="0.0.0.0",
            port=0,
            enable_actions=True,
            allow_remote_actions=True,
        )

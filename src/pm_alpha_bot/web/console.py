from __future__ import annotations

import base64
import hmac
import json
import time
from binascii import Error as BinasciiError
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Any, cast
from urllib.parse import urlparse

from sqlalchemy import func, select

from pm_alpha_bot.clients.clob_v2 import LiveTradingDisabledError
from pm_alpha_bot.config import Settings, get_settings
from pm_alpha_bot.db.models import (
    Fill,
    Market,
    MarketSnapshot,
    Order,
    RuntimeEvent,
    Signal,
    WalletActivity,
    WalletDetailSnapshot,
    WalletScore,
)
from pm_alpha_bot.db.repository import Repository
from pm_alpha_bot.db.session import session_scope
from pm_alpha_bot.execution.live import reset_live_kill_switch, sync_live_orders
from pm_alpha_bot.logging import get_logger
from pm_alpha_bot.ops.healthcheck import evaluate_healthcheck
from pm_alpha_bot.strategy.diagnostics import analyze_signal_observations
from pm_alpha_bot.time import ensure_utc

logger = get_logger(__name__)
_MAX_BODY_BYTES = 16_384
_DEFAULT_EVENT_CATEGORY = "live"
_SSE_EVENT_INTERVAL_SECONDS = 5.0
_SSE_RETRY_MS = 5_000
_REJECT_REASON_KEYS = (
    "skipped_submission_reason",
    "reject_reason",
    "rejection_reason",
    "failure_reason",
    "status_reason",
    "reason",
    "error",
    "message",
)
_REJECT_REASON_NESTED_KEYS = (
    "kill_switch_state",
    "risk",
    "remote_order",
    "error_response",
    "response",
)
_NON_ACTIONABLE_REJECT_REASONS = {"", "ok", "none", "null", "false", "true"}


@dataclass(frozen=True)
class ConsoleActionResult:
    """Structured action response payload."""

    action: str
    ok: bool
    result: dict[str, Any]


@dataclass(frozen=True)
class ConsoleAuthConfig:
    """HTTP Basic Auth configuration for the console."""

    realm: str = "PM Alpha Bot Console"
    username: str = ""
    password: str = ""

    @property
    def enabled(self) -> bool:
        """Return true when username and password are both configured."""
        return bool(self.username and self.password)


class OpsConsoleServer(ThreadingHTTPServer):
    """Threaded HTTP server for the local operations console."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        settings: Settings,
        enable_actions: bool,
        auth: ConsoleAuthConfig,
    ) -> None:
        self.settings = settings
        self.enable_actions = enable_actions
        self.auth = auth
        super().__init__(server_address, request_handler_class)


class OpsConsoleRequestHandler(BaseHTTPRequestHandler):
    """Serve the console HTML and small JSON/action endpoints."""

    server: OpsConsoleServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authenticate_request():
            return
        try:
            if parsed.path in {"/", "/index.html"}:
                self._write_html(_INDEX_HTML)
                return
            if parsed.path == "/api/console":
                payload = build_console_payload(
                    settings=self.server.settings,
                    actions_enabled=self.server.enable_actions,
                    auth_enabled=self.server.auth.enabled,
                )
                self._write_json(payload)
                return
            if parsed.path == "/events":
                self._stream_console_events()
                return
        except Exception as exc:  # pragma: no cover - exercised in live smoke
            logger.exception("ops_console_get_failed", extra={"path": parsed.path})
            self._write_json(
                {"ok": False, "error": str(exc), "path": parsed.path},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._write_json(
            {"ok": False, "error": f"Unknown path: {parsed.path}"},
            status=HTTPStatus.NOT_FOUND,
        )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._authenticate_request():
            return
        if not parsed.path.startswith("/api/actions/"):
            self._write_json(
                {"ok": False, "error": f"Unknown path: {parsed.path}"},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        if not self.server.enable_actions:
            self._write_json(
                {"ok": False, "error": "Actions are disabled. Restart with --enable-actions."},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        action = parsed.path.removeprefix("/api/actions/")
        try:
            body = self._read_json_body()
            result = execute_console_action(
                action=action,
                body=body,
                settings=self.server.settings,
            )
        except (ValueError, LiveTradingDisabledError) as exc:
            self._write_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        except Exception as exc:  # pragma: no cover - guarded by focused tests
            logger.exception("ops_console_action_failed", extra={"action": action})
            self._write_json(
                {"ok": False, "error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._write_json(
            {
                "ok": result.ok,
                "action": result.action,
                "result": result.result,
            }
        )

    def log_message(self, format: str, *args: object) -> None:
        message = format % args
        logger.info(
            "ops_console_http",
            extra={
                "client": self.address_string(),
                "request_line": self.requestline,
                "http_message": message,
            },
        )

    def _authenticate_request(self) -> bool:
        auth = self.server.auth
        if not auth.enabled:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded.strip():
            self._write_auth_required()
            return False
        try:
            decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
        except (BinasciiError, UnicodeDecodeError, ValueError):
            self._write_auth_required()
            return False
        username, separator, password = decoded.partition(":")
        if not separator:
            self._write_auth_required()
            return False
        if hmac.compare_digest(username, auth.username) and hmac.compare_digest(
            password, auth.password
        ):
            return True
        logger.warning(
            "ops_console_auth_failed",
            extra={"client": self.address_string(), "path": self.path},
        )
        self._write_auth_required()
        return False

    def _write_auth_required(self) -> None:
        auth = self.server.auth
        encoded = json.dumps({"ok": False, "error": "Authentication required."}).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header(
            "WWW-Authenticate",
            f'Basic realm="{_escape_http_auth_realm(auth.realm)}", charset="UTF-8"',
        )
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0").strip() or "0"
        try:
            content_length = int(raw_length)
        except ValueError as exc:  # pragma: no cover - parser guard
            raise ValueError("Invalid Content-Length header.") from exc
        if content_length > _MAX_BODY_BYTES:
            raise ValueError("Request body is too large.")
        if content_length == 0:
            return {}
        payload = self.rfile.read(content_length)
        if not payload:
            return {}
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Request body must be a JSON object.")
        return cast(dict[str, Any], parsed)

    def _write_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        encoded = json.dumps(_json_safe(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _stream_console_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(f"retry: {_SSE_RETRY_MS}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        while True:
            try:
                payload = build_console_payload(
                    settings=self.server.settings,
                    actions_enabled=self.server.enable_actions,
                    auth_enabled=self.server.auth.enabled,
                )
                self.wfile.write(_encode_sse_event("console", payload))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:  # pragma: no cover - exercised in live smoke
                logger.exception("ops_console_sse_payload_failed")
                try:
                    self.wfile.write(
                        _encode_sse_event(
                            "console-error",
                            {"ok": False, "error": "Failed to build console payload."},
                        )
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            time.sleep(_SSE_EVENT_INTERVAL_SECONDS)


def create_console_server(
    *,
    settings: Settings | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    enable_actions: bool = False,
    allow_remote_actions: bool = False,
) -> OpsConsoleServer:
    """Create a configured operations console server."""
    resolved = settings or get_settings()
    auth = _resolve_console_auth(resolved)
    if enable_actions and not allow_remote_actions and not _is_loopback_host(host):
        raise ValueError(
            "Refusing to enable remote actions on a non-loopback host. "
            "Use --allow-remote-actions only after reviewing the risk."
        )
    if enable_actions and not _is_loopback_host(host) and not auth.enabled:
        raise ValueError(
            "Refusing to enable remote actions without console authentication. "
            "Set PM_ALPHA_CONSOLE_AUTH_USERNAME and PM_ALPHA_CONSOLE_AUTH_PASSWORD."
        )
    return OpsConsoleServer(
        (host, port),
        OpsConsoleRequestHandler,
        settings=resolved,
        enable_actions=enable_actions,
        auth=auth,
    )


def serve_console(
    *,
    settings: Settings | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    enable_actions: bool = False,
    allow_remote_actions: bool = False,
) -> None:
    """Start the operations console server and block forever."""
    server = create_console_server(
        settings=settings,
        host=host,
        port=port,
        enable_actions=enable_actions,
        allow_remote_actions=allow_remote_actions,
    )
    logger.info(
        "ops_console_started",
        extra={
            "host": host,
            "port": port,
            "enable_actions": enable_actions,
        },
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_console_payload(
    *,
    settings: Settings | None = None,
    actions_enabled: bool = False,
    auth_enabled: bool = False,
) -> dict[str, Any]:
    """Build an aggregate JSON payload for the browser console."""
    resolved = settings or get_settings()
    with session_scope(resolved) as session:
        repo = Repository(session)
        db_health = evaluate_healthcheck(repo, settings=resolved)
        live_health = evaluate_healthcheck(
            repo,
            settings=resolved,
            require_live_supervisor=True,
        )
        live_state = repo.live_operations_state()
        live_report = repo.live_report(resolved.live_initial_capital)
        paper_report = repo.paper_report()
        wallets = repo.wallet_summary_rows(8)
        signals = repo.signal_summary_rows(8)
        observation_report = analyze_signal_observations(
            repo,
            category="OVERALL",
            score_threshold=min(resolved.signal_min_wallet_score, 0.30),
            consensus=1,
            settings=resolved,
            top_candidates=8,
        )
        live_orders = repo.list_orders(mode="live", limit=12)
        live_events = repo.list_runtime_events(category=_DEFAULT_EVENT_CATEGORY, limit=25)
        signal_events = repo.list_runtime_events(category="signal", limit=12)
        execution_rejects = _build_execution_reject_report(
            repo=repo,
            settings=resolved,
            db_health=db_health,
            live_health=live_health,
            live_state=live_state,
            live_report=live_report,
            observation_report=observation_report,
            signals_count=len(signals),
        )
        position_risks = _build_position_risk_rows(
            repo=repo,
            paper_positions=paper_report.get("positions", []),
            live_positions=live_report.get("positions", []),
            settings=resolved,
        )
        service_heartbeats = _build_service_heartbeat_rows(
            repo=repo,
            settings=resolved,
            as_of=datetime.now(UTC),
        )
    paper_positions = [
        _serialize_paper_position(row)
        for row in paper_report.get("positions", [])
        if row is not None
    ]
    paper_report_payload = dict(paper_report)
    paper_report_payload["positions"] = paper_positions

    summary = _build_console_summary(
        settings=resolved,
        db_health=db_health,
        live_health=live_health,
        live_state=live_state,
        live_report=live_report,
        paper_report=paper_report,
        signals_count=len(signals),
        observation_only_count=int(observation_report["current"].get("observation_only", 0)),
        promotion_ready_count=int(observation_report["current"].get("promotion_ready", 0)),
        promotion_queue_count=int(observation_report["current"].get("promotion_queue", 0)),
        live_events_count=len(live_events),
        actions_enabled=actions_enabled,
    )
    return _json_safe(
        {
            "generated_at": datetime.now(UTC),
            "context": {
                "app_env": resolved.app_env,
                "database_backend": "sqlite" if resolved.is_sqlite else "postgres",
                "actions_enabled": actions_enabled,
                "auth_enabled": auth_enabled,
                "live_trading_enabled": resolved.enable_live_trading,
                "runtime_event_retention_days": resolved.runtime_event_retention_days,
            },
            "summary": summary,
            "health": {"db": db_health, "live": live_health},
            "runtime_state": live_state,
            "live_report": live_report,
            "paper_report": paper_report_payload,
            "wallets": [_serialize_wallet_score(row) for row in wallets],
            "signals": [_serialize_signal(row) for row in signals],
            "signal_observations": [
                _serialize_observation_candidate(row)
                for row in observation_report["current"].get("observation_candidates", [])
                if isinstance(row, dict)
            ],
            "signal_promotion_queue": [
                _serialize_observation_candidate(row)
                for row in observation_report["current"].get("promotion_queue_candidates", [])
                if isinstance(row, dict)
            ],
            "signal_events": [_serialize_runtime_event(row) for row in signal_events],
            "live_orders": [_serialize_order(row) for row in live_orders],
            "live_events": [_serialize_runtime_event(row) for row in live_events],
            "execution_rejects": execution_rejects,
            "position_risks": position_risks,
            "service_heartbeats": service_heartbeats,
        }
    )


def execute_console_action(
    *,
    action: str,
    body: dict[str, Any],
    settings: Settings | None = None,
) -> ConsoleActionResult:
    """Execute a restricted management action for the web console."""
    resolved = settings or get_settings()
    with session_scope(resolved) as session:
        repo = Repository(session)
        if action == "reset-live-kill-switch":
            return ConsoleActionResult(
                action=action,
                ok=True,
                result=reset_live_kill_switch(
                    repo,
                    settings=resolved,
                    acknowledge=bool(body.get("acknowledge", False)),
                    reset_reason=str(body.get("reason") or "console_operator_reset"),
                    allow_active_orders=bool(body.get("allow_active_orders", False)),
                ),
            )
        if action == "sync-live":
            limit = _coerce_positive_int(body.get("limit", resolved.live_order_sync_limit), "limit")
            return ConsoleActionResult(
                action=action,
                ok=True,
                result=sync_live_orders(repo, settings=resolved, limit=limit),
            )
        if action == "prune-runtime-events":
            retention_days = _coerce_positive_int(
                body.get("older_than_days", resolved.runtime_event_retention_days),
                "older_than_days",
            )
            apply = bool(body.get("apply", False))
            category = body.get("category", _DEFAULT_EVENT_CATEGORY)
            if category == "":
                category = None
            cutoff = datetime.now(UTC) - timedelta(days=retention_days)
            count = repo.prune_runtime_events(
                older_than=cutoff,
                category=category if isinstance(category, str) or category is None else None,
                dry_run=not apply,
            )
            return ConsoleActionResult(
                action=action,
                ok=True,
                result={
                    "count": count,
                    "cutoff": cutoff,
                    "apply": apply,
                    "category": category,
                },
            )
    raise ValueError(f"Unsupported action: {action}")


def _build_console_summary(
    *,
    settings: Settings,
    db_health: dict[str, Any],
    live_health: dict[str, Any],
    live_state: dict[str, Any],
    live_report: dict[str, Any],
    paper_report: dict[str, Any],
    signals_count: int,
    observation_only_count: int,
    promotion_ready_count: int,
    promotion_queue_count: int,
    live_events_count: int,
    actions_enabled: bool,
) -> dict[str, Any]:
    supervisor = live_state.get("supervisor")
    kill_switch = live_state.get("kill_switch")
    supervisor_state = supervisor if isinstance(supervisor, dict) else {}
    kill_switch_state = kill_switch if isinstance(kill_switch, dict) else {}
    supervisor_status = str(supervisor_state.get("status") or "missing").upper()
    supervisor_age = live_health.get("supervisor_age_sec")
    kill_switch_tripped = bool(kill_switch_state.get("tripped", False))
    open_orders = int(live_report.get("open_orders_count", 0))
    equity = float(live_report.get("equity", settings.live_initial_capital))
    db_ok = bool(db_health.get("ok", False))
    live_ok = bool(live_health.get("ok", False))
    paper_positions = paper_report.get("positions", [])
    paper_position_rows = paper_positions if isinstance(paper_positions, list) else []
    paper_position_count = len(paper_position_rows)
    paper_realized_pnl = _as_float(paper_report.get("realized_pnl"))
    paper_unrealized_pnl = _as_float(paper_report.get("unrealized_pnl"))
    paper_invested = sum(_paper_position_notional(row) for row in paper_position_rows)
    paper_equity = settings.paper_initial_capital + paper_realized_pnl + paper_unrealized_pnl
    paper_cash = settings.paper_initial_capital + paper_realized_pnl - paper_invested
    target_entry_amount = max(
        0.0,
        settings.paper_initial_capital * settings.max_market_exposure_pct,
    )
    next_entry_amount = min(target_entry_amount, max(0.0, paper_cash))
    max_position_slots = (
        int(1.0 / settings.max_market_exposure_pct)
        if settings.max_market_exposure_pct > 0
        else 0
    )
    free_position_slots = max(max_position_slots - paper_position_count, 0)

    run_mode = "PAPER ONLY"
    live_indicator = "IDLE"
    tone = "warn"
    if kill_switch_tripped:
        run_mode = "KILL SWITCH"
        live_indicator = "ERROR"
        tone = "error"
    elif settings.enable_live_trading and live_ok:
        run_mode = "LIVE ACTIVE"
        live_indicator = "LIVE"
        tone = "ok"
    elif settings.enable_live_trading:
        run_mode = "LIVE ENABLED"
        live_indicator = "STALE"
        tone = "warn"

    primary_status = "SYSTEM HEALTHY"
    decision_summary = "READ-ONLY MONITORING MODE. REVIEW SIGNALS BEFORE ANY MANUAL ACTION."
    if not db_ok:
        primary_status = "DATABASE HEALTHCHECK FAILED"
        decision_summary = "DO NOT OPERATE. RESTORE DATABASE HEALTH FIRST."
        tone = "error"
    elif kill_switch_tripped:
        primary_status = "PERSISTENT LIVE KILL SWITCH ACTIVE"
        decision_summary = "REVIEW THE LAST ERROR, THEN RESET THE KILL SWITCH MANUALLY."
    elif settings.enable_live_trading and not live_ok:
        primary_status = "LIVE SUPERVISOR STATE IS NOT READY"
        decision_summary = "KEEP RECONCILIATION RUNNING UNTIL SUPERVISOR STATE IS FRESH."
    elif promotion_queue_count > 0:
        primary_status = "PROMOTION QUEUE HAS PERSISTENT CANDIDATES"
        decision_summary = "REVIEW STREAKED OBSERVATION CANDIDATES BEFORE MANUAL ACTION."
        tone = "warn"
    elif promotion_ready_count > 0:
        primary_status = "OBSERVATION CANDIDATES READY"
        decision_summary = "REVIEW PROMOTION-READY OBSERVATION CANDIDATES BEFORE MANUAL ACTION."
        tone = "warn"
    elif signals_count == 0:
        primary_status = "NO QUALIFIED SIGNALS"
        decision_summary = "SYSTEM IS HEALTHY BUT THERE IS NOTHING TO SUBMIT RIGHT NOW."
        tone = "warn" if tone == "ok" else tone

    signal_state = "IDLE"
    signal_detail = "qualified 0"
    signal_tone = "warn"
    if signals_count > 0:
        signal_state = f"{signals_count} READY"
        signal_detail = "qualified signal queue has candidates"
        signal_tone = "ok"
    elif promotion_queue_count > 0:
        signal_state = "WATCH QUEUE"
        signal_detail = f"persistent candidates {promotion_queue_count}"
    elif promotion_ready_count > 0:
        signal_state = "OBSERVE READY"
        signal_detail = f"promotion-ready {promotion_ready_count}"

    operator_summary = [
        _status_card(
            label="MODE",
            value="PAPER MODE" if run_mode == "PAPER ONLY" else run_mode,
            description="현재 운용 모드",
            detail="실계정 주문 없음" if not settings.enable_live_trading else "실거래 설정 켜짐",
            tone=tone,
        ),
        _status_card(
            label="총자산",
            value=_format_usd(paper_equity),
            description="모의투자 기준 현재 총자산",
            detail=(
                f"realized {_format_usd(paper_realized_pnl)} / "
                f"unreal {_format_usd(paper_unrealized_pnl)}"
            ),
            tone="ok" if paper_equity >= settings.paper_initial_capital else "warn",
        ),
        _status_card(
            label="현금",
            value=_format_usd(paper_cash),
            description="다음 진입에 쓸 수 있는 추정 현금",
            detail=f"invested {_format_usd(paper_invested)}",
            tone="ok" if paper_cash > 0 else "warn",
        ),
        _status_card(
            label="포지션 슬롯",
            value=(
                f"{paper_position_count}/{max_position_slots}"
                if max_position_slots
                else str(paper_position_count)
            ),
            description="현재 보유 중인 모의 포지션 수",
            detail=(
                f"free {free_position_slots} / "
                f"max market {settings.max_market_exposure_pct:.1%}"
            ),
            tone="ok" if free_position_slots > 0 else "warn",
        ),
        _status_card(
            label="다음 진입",
            value=_format_usd(next_entry_amount),
            description="위험 한도 기준 다음 주문 가능 금액",
            detail=f"target {_format_usd(target_entry_amount)} / cash {_format_usd(paper_cash)}",
            tone="ok" if next_entry_amount > 0 and free_position_slots > 0 else "warn",
        ),
        _status_card(
            label="신호 상태",
            value=signal_state,
            description="지금 바로 검토할 신호 여부",
            detail=signal_detail,
            tone=signal_tone,
        ),
        _status_card(
            label="LIVE SAFETY",
            value="LIVE ON" if settings.enable_live_trading else "LIVE OFF",
            description="실거래 주문 제출 안전 상태",
            detail=(
                "기본 차단 상태"
                if not settings.enable_live_trading
                else "실거래 전 안전조건 확인 필요"
            ),
            tone="error" if settings.enable_live_trading else "ok",
        ),
    ]

    hero_banner = "\n".join(
        [
            "PM ALPHA BOT // OPS CONSOLE",
            f"RUNMODE   {run_mode:<18} | INDICATOR  {live_indicator}",
            (
                f"DB        {'OK' if db_ok else 'FAIL':<18} | ACTIONS    "
                f"{'ENABLED' if actions_enabled else 'READ ONLY'}"
            ),
            (
                f"LIVE      {'ENABLED' if settings.enable_live_trading else 'DISABLED':<18} "
                f"| SIGNALS    {signals_count}"
            ),
            f"ORDERS    {open_orders:<18} | OBSERVE    {observation_only_count}",
            f"EVENTS    {live_events_count:<18} | READY      {promotion_ready_count}",
            f"QUEUE     {promotion_queue_count:<18} | STATUS     {primary_status}",
        ]
    )

    mode_cells = [
        _status_cell(
            label="live trading",
            value="enabled" if settings.enable_live_trading else "disabled",
            description="실거래 주문 제출 기능이 켜져 있는지 표시합니다.",
            tone="ok" if settings.enable_live_trading else "warn",
        ),
        _status_cell(
            label="actions",
            value="enabled" if actions_enabled else "read only",
            description="웹에서 관리 작업 버튼을 실행할 수 있는지 표시합니다.",
            tone="ok" if actions_enabled else "warn",
        ),
        _status_cell(
            label="supervisor",
            value=supervisor_status,
            description="실거래 감시 루프가 최근 정상 heartbeat를 보냈는지 표시합니다.",
            tone="ok" if live_ok else "warn",
        ),
        _status_cell(
            label="kill switch",
            value="tripped" if kill_switch_tripped else "clear",
            description="위험 차단 스위치가 실거래 제출을 막고 있는지 표시합니다.",
            tone="error" if kill_switch_tripped else "ok",
        ),
    ]
    status_cards = [
        _status_card(
            label="system",
            value=primary_status,
            description="DB, 감시 루프, kill switch를 합친 전체 운영 상태입니다.",
            detail=decision_summary,
            tone=tone,
            prominent=True,
        ),
        _status_card(
            label="db",
            value="healthy" if db_ok else "failing",
            description="봇이 데이터베이스에 정상 접속하고 조회할 수 있는지입니다.",
            detail=f"backend={ 'sqlite' if settings.is_sqlite else 'postgres' }",
            tone="ok" if db_ok else "error",
        ),
        _status_card(
            label="supervisor age",
            value=_format_age(supervisor_age),
            description="마지막 실거래 감시 heartbeat가 얼마나 오래됐는지입니다.",
            detail=f"status={supervisor_status.lower()}",
            tone="ok" if live_ok else "warn",
        ),
        _status_card(
            label="open live orders",
            value=str(open_orders),
            description="현재 거래소에 남아 있는 실거래 주문 수입니다.",
            detail=f"equity={equity:.2f}",
            tone="ok" if open_orders == 0 else "warn",
        ),
        _status_card(
            label="signal queue",
            value=str(signals_count),
            description="전략이 매수 후보로 인정해 저장한 신호 수입니다.",
            detail=f"recent events={live_events_count}",
            tone="ok" if signals_count > 0 else "warn",
        ),
        _status_card(
            label="observations",
            value=str(observation_only_count),
            description="주문장은 비었지만 계속 지켜볼 만한 후보 수입니다.",
            detail=f"promotion ready={promotion_ready_count} / queue={promotion_queue_count}",
            tone="warn" if observation_only_count > 0 else "ok",
        ),
    ]
    decision_cards = [
        _status_card(
            label="submission gate",
            value="blocked" if (kill_switch_tripped or not db_ok or not live_ok) else "clear",
            description="실거래 주문을 제출해도 되는 안전 조건이 통과됐는지입니다.",
            detail="kill switch, db, and supervisor state must be clear before new live orders.",
            tone="error" if (kill_switch_tripped or not db_ok or not live_ok) else "ok",
        ),
        _status_card(
            label="paper pnl",
            value="paper-first",
            description="기본 운영 모드가 모의투자 우선인지 확인하는 카드입니다.",
            detail="paper trading remains the default operating mode.",
            tone="ok",
        ),
        _status_card(
            label="live equity",
            value=f"{equity:.2f}",
            description="실거래 계정 기준으로 계산한 현재 자산 추정값입니다.",
            detail=f"open orders={open_orders}",
            tone="ok" if equity >= settings.live_initial_capital else "warn",
        ),
        _status_card(
            label="retention",
            value=f"{settings.runtime_event_retention_days}d",
            description="운영 이벤트 로그를 보관하는 기본 기간입니다.",
            detail="runtime event pruning defaults to dry-run.",
            tone="ok",
        ),
    ]
    return {
        "run_mode": run_mode,
        "live_indicator": live_indicator,
        "tone": tone,
        "primary_status": primary_status,
        "decision_summary": decision_summary,
        "hero_banner": hero_banner,
        "operator_summary": operator_summary,
        "top_meta_items": [
            f"env / {settings.app_env}",
            f"db / {'sqlite' if settings.is_sqlite else 'postgres'}",
            f"live / {'enabled' if settings.enable_live_trading else 'disabled'}",
            f"events / {settings.runtime_event_retention_days}d retention",
        ],
        "mode_cells": mode_cells,
        "status_cards": status_cards,
        "decision_cards": decision_cards,
    }


def _serialize_wallet_score(row: Any) -> dict[str, Any]:
    return {
        "proxy_wallet": getattr(row, "proxy_wallet", ""),
        "category": getattr(row, "category", ""),
        "as_of": getattr(row, "as_of", None),
        "score": getattr(row, "score", None),
        "roi": getattr(row, "roi", None),
        "pnl": getattr(row, "pnl", None),
        "trade_count": getattr(row, "trade_count", None),
        "profit_concentration": getattr(row, "profit_concentration", None),
    }


def _serialize_signal(row: Any) -> dict[str, Any]:
    return {
        "id": getattr(row, "id", None),
        "ts": getattr(row, "ts", None),
        "token_id": getattr(row, "token_id", ""),
        "condition_id": getattr(row, "condition_id", ""),
        "direction": getattr(row, "direction", ""),
        "edge_bps": getattr(row, "edge_bps", None),
        "fair_prob": getattr(row, "fair_prob", None),
        "effective_entry_price": getattr(row, "effective_entry_price", None),
        "status": getattr(row, "status", ""),
        "source_wallet_count": getattr(row, "source_wallet_count", 0),
    }


def _serialize_observation_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "token_id": row.get("token_id", ""),
        "reason": row.get("reason", ""),
        "source_wallet_count": row.get("source_wallet_count", 0),
        "observation_streak": row.get("observation_streak", 0),
        "observation_score": row.get("observation_score"),
        "promotion_ready": bool(row.get("promotion_ready", False)),
        "wallet_direction_score": row.get("wallet_direction_score"),
        "last_trade_price": row.get("last_trade_price"),
        "snapshot_age_sec": row.get("snapshot_age_sec"),
    }


def _serialize_order(row: Any) -> dict[str, Any]:
    return {
        "id": getattr(row, "id", None),
        "created_at": getattr(row, "created_at", None),
        "mode": getattr(row, "mode", ""),
        "token_id": getattr(row, "token_id", ""),
        "condition_id": getattr(row, "condition_id", ""),
        "side": getattr(row, "side", ""),
        "price": getattr(row, "price", None),
        "size": getattr(row, "size", None),
        "status": getattr(row, "status", ""),
        "order_type": getattr(row, "order_type", ""),
        "external_order_id": getattr(row, "external_order_id", None),
    }


def _serialize_runtime_event(row: Any) -> dict[str, Any]:
    return {
        "id": getattr(row, "id", None),
        "ts": getattr(row, "ts", None),
        "level": getattr(row, "level", ""),
        "event_type": getattr(row, "event_type", ""),
        "message": getattr(row, "message", ""),
        "event_json": getattr(row, "event_json", None),
    }


def _serialize_paper_position(row: Any) -> dict[str, Any]:
    return {
        "id": getattr(row, "id", None),
        "updated_at": getattr(row, "updated_at", None),
        "condition_id": getattr(row, "condition_id", ""),
        "token_id": getattr(row, "token_id", ""),
        "side": getattr(row, "side", ""),
        "size": getattr(row, "size", None),
        "avg_price": getattr(row, "avg_price", None),
        "realized_pnl": getattr(row, "realized_pnl", None),
        "unrealized_pnl": getattr(row, "unrealized_pnl", None),
    }


def _build_execution_reject_report(
    *,
    repo: Repository,
    settings: Settings,
    db_health: dict[str, Any],
    live_health: dict[str, Any],
    live_state: dict[str, Any],
    live_report: dict[str, Any],
    observation_report: dict[str, Any],
    signals_count: int,
) -> dict[str, Any]:
    summary_index: dict[tuple[str, str], dict[str, Any]] = {}
    recent_rows: list[dict[str, Any]] = []

    def add_summary(
        *,
        reason: str,
        source: str,
        count: int = 1,
        last_seen: Any = None,
        detail: str = "",
        tone: str = "warn",
    ) -> None:
        normalized_reason = _normalize_reject_reason(reason)
        if not normalized_reason:
            return
        key = (normalized_reason, source)
        existing = summary_index.get(key)
        parsed_last_seen = _parse_datetime_like(last_seen)
        if existing is None:
            summary_index[key] = {
                "reason": normalized_reason,
                "source": source,
                "count": count,
                "last_seen": parsed_last_seen,
                "detail": detail,
                "tone": tone,
            }
            return
        existing["count"] = int(existing["count"]) + count
        if parsed_last_seen is not None and (
            existing["last_seen"] is None or parsed_last_seen > existing["last_seen"]
        ):
            existing["last_seen"] = parsed_last_seen
            existing["detail"] = detail
        if tone == "error":
            existing["tone"] = "error"

    def add_recent(
        *,
        ts: Any,
        source: str,
        reason: str,
        detail: str = "",
        token_id: str = "",
        status: str = "",
        tone: str = "warn",
    ) -> None:
        normalized_reason = _normalize_reject_reason(reason)
        if not normalized_reason:
            return
        parsed_ts = _parse_datetime_like(ts)
        detail_text = _trim_reject_text(detail)
        row = {
            "ts": parsed_ts,
            "source": source,
            "reason": normalized_reason,
            "detail": detail_text,
            "token_id": token_id,
            "status": status,
            "tone": tone,
        }
        recent_rows.append(row)
        add_summary(
            reason=normalized_reason,
            source=source,
            last_seen=parsed_ts,
            detail=detail_text,
            tone=tone,
        )

    current_blockers = _build_current_execution_blockers(
        settings=settings,
        db_health=db_health,
        live_health=live_health,
        live_state=live_state,
        live_report=live_report,
        signals_count=signals_count,
    )

    for event in repo.list_runtime_events(category=_DEFAULT_EVENT_CATEGORY, limit=100):
        payload = event.event_json if isinstance(event.event_json, dict) else {}
        if event.event_type == "live_supervisor_iteration":
            skipped_reason = _extract_reject_reason(payload.get("skipped_submission_reason"))
            if skipped_reason:
                add_recent(
                    ts=event.ts,
                    source="supervisor skip",
                    reason=skipped_reason,
                    detail=str(event.message or "live supervisor skipped submission"),
                    status=event.event_type,
                )
        elif event.event_type in {"live_supervisor_error", "live_kill_switch_triggered"}:
            reason = _extract_reject_reason(payload) or str(event.message or event.event_type)
            add_recent(
                ts=event.ts,
                source=event.event_type,
                reason=reason,
                detail=str(event.message or ""),
                status=event.event_type,
                tone="error",
            )

    terminal_orders = repo.list_orders(
        mode="live",
        statuses=["rejected", "failed", "expired"],
        limit=50,
    )
    for order in terminal_orders:
        reason = _extract_reject_reason(order.reason_json) or f"live_order_{order.status}"
        add_recent(
            ts=order.created_at,
            source="live order",
            reason=reason,
            detail=f"{order.side} {order.size or 0:g}@{order.price or 0:g}",
            token_id=order.token_id,
            status=order.status,
            tone="error" if order.status in {"rejected", "failed"} else "warn",
        )

    current_report = observation_report.get("current", {})
    reason_counts = current_report.get("reason_counts") if isinstance(current_report, dict) else {}
    if isinstance(reason_counts, dict):
        for reason, count in reason_counts.items():
            normalized_reason = _normalize_reject_reason(reason)
            if not normalized_reason or normalized_reason == "generated":
                continue
            add_summary(
                reason=normalized_reason,
                source="signal diagnostics",
                count=_coerce_count(count),
                detail="현재 신호 생성 전 필터/관찰 단계에서 집계된 사유",
            )

    recent_rows.sort(
        key=lambda row: row["ts"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    summary_rows = sorted(
        summary_index.values(),
        key=lambda row: (
            int(row["count"]),
            row["last_seen"] or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )
    return {
        "current_blockers": current_blockers,
        "summary": summary_rows[:10],
        "recent": recent_rows[:12],
    }


def _build_current_execution_blockers(
    *,
    settings: Settings,
    db_health: dict[str, Any],
    live_health: dict[str, Any],
    live_state: dict[str, Any],
    live_report: dict[str, Any],
    signals_count: int,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []

    def add(reason: str, detail: str, tone: str = "warn") -> None:
        blockers.append({"reason": reason, "detail": detail, "tone": tone})

    if not settings.enable_live_trading:
        add("live_trading_disabled", "설정상 실거래 주문 제출이 꺼져 있습니다.")
    if not bool(db_health.get("ok", False)):
        add("database_unhealthy", "DB healthcheck가 통과하지 못했습니다.", tone="error")
    kill_switch = live_state.get("kill_switch")
    kill_switch_state = kill_switch if isinstance(kill_switch, dict) else {}
    if bool(kill_switch_state.get("tripped", False)):
        reason = _normalize_reject_reason(kill_switch_state.get("reason"))
        detail = f"kill switch reason={reason}" if reason else "persistent kill switch active"
        add("persistent_kill_switch_active", detail, tone="error")
    if settings.enable_live_trading and not bool(live_health.get("ok", False)):
        add("live_supervisor_not_ready", "실거래 supervisor heartbeat가 정상 상태가 아닙니다.")
    if signals_count == 0:
        add("no_qualified_signal", "현재 주문 후보로 승격된 신호가 없습니다.")
    open_orders = int(live_report.get("open_orders_count", 0) or 0)
    if open_orders > 0:
        add("active_live_order_present", f"이미 활성 live order가 {open_orders}개 있습니다.")
    return blockers


def _build_position_risk_rows(
    *,
    repo: Repository,
    paper_positions: Any,
    live_positions: Any,
    settings: Settings,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    paper_rows = paper_positions if isinstance(paper_positions, list) else []
    live_rows = live_positions if isinstance(live_positions, list) else []
    for position in paper_rows:
        size = _as_float(getattr(position, "size", None))
        if size <= 0:
            continue
        token_id = str(getattr(position, "token_id", ""))
        snapshot = repo.latest_snapshot_for_token(token_id)
        current_price = _snapshot_midpoint(snapshot)
        if current_price is None:
            current_price = _position_current_price_from_unrealized(
                entry_price=_as_float(getattr(position, "avg_price", None)),
                size=size,
                unrealized_pnl=_as_float(getattr(position, "unrealized_pnl", None)),
                side=str(getattr(position, "side", "")),
            )
        rows.append(
            _build_position_risk_row(
                mode="paper",
                token_id=token_id,
                condition_id=str(getattr(position, "condition_id", "")),
                side=str(getattr(position, "side", "")),
                size=size,
                entry_price=_as_float(getattr(position, "avg_price", None)),
                current_price=current_price,
                updated_at=getattr(position, "updated_at", None),
                snapshot_ts=getattr(snapshot, "ts", None) if snapshot is not None else None,
                market_label=_market_label(
                    repo,
                    token_id,
                    str(getattr(position, "condition_id", "")),
                ),
                settings=settings,
            )
        )

    for position in live_rows:
        if not isinstance(position, dict):
            continue
        size = _as_float(position.get("size"))
        if size <= 0:
            continue
        token_id = str(position.get("token_id", ""))
        current_price = position.get("current_price")
        if current_price is None:
            current_price = _position_current_price_from_unrealized(
                entry_price=_as_float(position.get("avg_price")),
                size=size,
                unrealized_pnl=_as_float(position.get("unrealized_pnl")),
                side=str(position.get("side", "")),
            )
        rows.append(
            _build_position_risk_row(
                mode="live",
                token_id=token_id,
                condition_id=str(position.get("condition_id", "")),
                side=str(position.get("side", "")),
                size=size,
                entry_price=_as_float(position.get("avg_price")),
                current_price=_as_float(current_price),
                updated_at=position.get("updated_at"),
                snapshot_ts=None,
                market_label=_market_label(repo, token_id, str(position.get("condition_id", ""))),
                settings=settings,
            )
        )

    rows.sort(
        key=lambda row: (
            row["stop_distance_pct"] is None,
            row["stop_distance_pct"] if row["stop_distance_pct"] is not None else 999.0,
            row["mode"],
            row["token_id"],
        )
    )
    return rows[:12]


def _build_service_heartbeat_rows(
    *,
    repo: Repository,
    settings: Settings,
    as_of: datetime,
) -> list[dict[str, Any]]:
    refresh_stale_after_sec = 6 * 60 * 60
    stream_stale_after_sec = max(
        float(settings.snapshot_stale_after_sec * 5),
        float(settings.orderbook_poll_interval_sec * 20),
        300.0,
    )
    live_supervisor_stale_after_sec = max(15.0, settings.live_heartbeat_interval_sec * 3)
    signal_last_seen = _max_datetime(
        repo.session.scalar(select(func.max(Signal.ts))),
        _max_runtime_event_ts(repo, category="signal"),
    )
    execution_last_seen = _max_datetime(
        repo.session.scalar(select(func.max(Order.created_at))),
        repo.session.scalar(select(func.max(Fill.ts))),
        _max_runtime_event_ts(repo, category="live", event_type="live_sync_completed"),
        _max_runtime_event_ts(repo, category="live", event_type="live_order_submitted"),
    )
    wallet_tracker_last_seen = _max_datetime(
        repo.session.scalar(select(func.max(WalletActivity.ts))),
        repo.session.scalar(select(func.max(WalletDetailSnapshot.observed_at))),
    )
    live_supervisor_state = repo.get_runtime_state("live_supervisor")
    live_supervisor_json = (
        live_supervisor_state.state_json
        if live_supervisor_state is not None and isinstance(live_supervisor_state.state_json, dict)
        else {}
    )
    live_supervisor_last_seen = _max_datetime(
        _parse_datetime_like(live_supervisor_json.get("last_iteration_at")),
        live_supervisor_state.updated_at if live_supervisor_state is not None else None,
    )
    rows = [
        _service_heartbeat_row(
            component="scanner",
            label="scanner",
            source="markets.updated_at",
            last_seen=repo.session.scalar(select(func.max(Market.updated_at))),
            stale_after_sec=refresh_stale_after_sec,
            as_of=as_of,
            detail="시장 메타데이터 수집/갱신 상태",
        ),
        _service_heartbeat_row(
            component="wallet-tracker",
            label="wallet-tracker",
            source="wallet_activity/detail_snapshot",
            last_seen=wallet_tracker_last_seen,
            stale_after_sec=refresh_stale_after_sec,
            as_of=as_of,
            detail="추적 지갑 포지션/활동 수집 상태",
        ),
        _service_heartbeat_row(
            component="wallet-scoring",
            label="wallet-scoring",
            source="wallet_scores.as_of",
            last_seen=repo.session.scalar(select(func.max(WalletScore.as_of))),
            stale_after_sec=refresh_stale_after_sec,
            as_of=as_of,
            detail="지갑 점수 계산 상태",
        ),
        _service_heartbeat_row(
            component="stream",
            label="stream/orderbook",
            source="market_snapshots.ts",
            last_seen=repo.session.scalar(select(func.max(MarketSnapshot.ts))),
            stale_after_sec=stream_stale_after_sec,
            as_of=as_of,
            detail="주문장/시장 스냅샷 최신성",
        ),
        _service_heartbeat_row(
            component="signal",
            label="signal",
            source="signals.ts + signal events",
            last_seen=signal_last_seen,
            stale_after_sec=refresh_stale_after_sec,
            as_of=as_of,
            detail="신호 생성 및 관찰 이벤트 상태",
            idle_ok=True,
        ),
        _service_heartbeat_row(
            component="exec",
            label="exec",
            source="orders/fills/live events",
            last_seen=execution_last_seen,
            stale_after_sec=refresh_stale_after_sec,
            as_of=as_of,
            detail="paper/live 주문·체결 처리 상태",
            idle_ok=True,
        ),
        _service_heartbeat_row(
            component="live-supervisor",
            label="live-supervisor",
            source="runtime_state.live_supervisor",
            last_seen=live_supervisor_last_seen,
            stale_after_sec=live_supervisor_stale_after_sec,
            as_of=as_of,
            detail=(
                f"status={live_supervisor_json.get('status', 'missing')} "
                f"errors={live_supervisor_json.get('consecutive_errors', 0)}"
            ),
            idle_ok=not settings.enable_live_trading,
            idle_status="standby",
        ),
    ]
    return rows


def _service_heartbeat_row(
    *,
    component: str,
    label: str,
    source: str,
    last_seen: Any,
    stale_after_sec: float,
    as_of: datetime,
    detail: str,
    idle_ok: bool = False,
    idle_status: str = "idle",
) -> dict[str, Any]:
    resolved_last_seen = _parse_datetime_like(last_seen)
    status = "missing"
    tone = "warn"
    age_sec: float | None = None
    if resolved_last_seen is None:
        if idle_ok:
            status = idle_status
            tone = "ok"
    else:
        age_sec = max(0.0, (ensure_utc(as_of) - resolved_last_seen).total_seconds())
        if age_sec <= stale_after_sec:
            status = "ok"
            tone = "ok"
        elif age_sec <= stale_after_sec * 3:
            status = "stale"
            tone = "warn"
        else:
            status = "silent"
            tone = "error"
    return {
        "component": component,
        "label": label,
        "status": status,
        "tone": tone,
        "last_seen": resolved_last_seen,
        "age_sec": age_sec,
        "age_label": _format_age(age_sec),
        "stale_after_sec": stale_after_sec,
        "source": source,
        "detail": detail,
    }


def _build_position_risk_row(
    *,
    mode: str,
    token_id: str,
    condition_id: str,
    side: str,
    size: float,
    entry_price: float,
    current_price: float | None,
    updated_at: Any,
    snapshot_ts: Any,
    market_label: str,
    settings: Settings,
) -> dict[str, Any]:
    current = current_price if current_price is not None else entry_price
    normalized_side = side.upper() or "LONG"
    is_short = normalized_side == "SHORT"
    target_pct = max(settings.min_edge_bps / 10_000.0, 0.0)
    stop_pct = max(settings.max_total_drawdown_pct, 0.0)
    if is_short:
        target_price = _clamp_price(entry_price * (1.0 - target_pct))
        stop_price = _clamp_price(entry_price * (1.0 + stop_pct))
        pnl_per_unit = entry_price - current
        target_distance_pct = _safe_ratio(current - target_price, current)
        stop_distance_pct = _safe_ratio(stop_price - current, current)
    else:
        target_price = _clamp_price(entry_price * (1.0 + target_pct))
        stop_price = _clamp_price(entry_price * (1.0 - stop_pct))
        pnl_per_unit = current - entry_price
        target_distance_pct = _safe_ratio(target_price - current, current)
        stop_distance_pct = _safe_ratio(current - stop_price, current)
    pnl_cash = pnl_per_unit * size
    pnl_pct = _safe_ratio(pnl_per_unit, entry_price)
    tone = "ok"
    if stop_distance_pct is not None and stop_distance_pct <= 0:
        tone = "error"
    elif (
        (stop_distance_pct is not None and stop_distance_pct < settings.max_daily_loss_pct)
        or (pnl_pct is not None and pnl_pct < 0)
    ):
        tone = "warn"
    return {
        "mode": mode,
        "token_id": token_id,
        "condition_id": condition_id,
        "market_label": market_label,
        "side": normalized_side,
        "size": size,
        "entry_price": entry_price,
        "current_price": current,
        "pnl_cash": pnl_cash,
        "pnl_pct": pnl_pct,
        "target_price": target_price,
        "target_distance_pct": target_distance_pct,
        "stop_price": stop_price,
        "stop_distance_pct": stop_distance_pct,
        "updated_at": updated_at,
        "snapshot_ts": snapshot_ts,
        "tone": tone,
        "rule_note": (
            f"target=min_edge {settings.min_edge_bps}bps; "
            f"stop=max_drawdown {settings.max_total_drawdown_pct:.1%}"
        ),
    }


def _status_cell(*, label: str, value: str, description: str, tone: str) -> dict[str, str]:
    return {"label": label, "value": value, "description": description, "tone": tone}


def _status_card(
    *,
    label: str,
    value: str,
    description: str,
    detail: str,
    tone: str,
    prominent: bool = False,
) -> dict[str, Any]:
    return {
        "label": label,
        "value": value,
        "description": description,
        "detail": detail,
        "tone": tone,
        "prominent": prominent,
    }


def _coerce_positive_int(value: Any, field_name: str) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc
    if resolved <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    return resolved


def _format_age(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    if value < 1:
        return "<1s"
    if value < 60:
        return f"{value:.0f}s"
    return f"{value / 60.0:.1f}m"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _paper_position_notional(row: Any) -> float:
    size = abs(_as_float(getattr(row, "size", None)))
    avg_price = _as_float(getattr(row, "avg_price", None))
    return max(0.0, size * avg_price)


def _format_usd(value: float) -> str:
    return f"${value:,.2f}"


def _normalize_reject_reason(value: Any) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).strip().split())
    if text.lower() in _NON_ACTIONABLE_REJECT_REASONS:
        return ""
    return _trim_reject_text(text)


def _trim_reject_text(value: Any, *, max_len: int = 180) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def _extract_reject_reason(value: Any) -> str:
    if isinstance(value, str):
        return _normalize_reject_reason(value)
    if not isinstance(value, dict):
        return ""
    for key in _REJECT_REASON_KEYS:
        reason = _extract_reject_reason(value.get(key))
        if reason:
            return reason
    for key in _REJECT_REASON_NESTED_KEYS:
        reason = _extract_reject_reason(value.get(key))
        if reason:
            return reason
    return ""


def _coerce_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _snapshot_midpoint(snapshot: Any) -> float | None:
    if snapshot is None:
        return None
    midpoint = getattr(snapshot, "midpoint", None)
    if midpoint is None:
        return None
    return _as_float(midpoint)


def _max_runtime_event_ts(
    repo: Repository,
    *,
    category: str,
    event_type: str | None = None,
) -> datetime | None:
    stmt = select(func.max(RuntimeEvent.ts)).where(RuntimeEvent.category == category)
    if event_type is not None:
        stmt = stmt.where(RuntimeEvent.event_type == event_type)
    return _parse_datetime_like(repo.session.scalar(stmt))


def _max_datetime(*values: Any) -> datetime | None:
    parsed = [_parse_datetime_like(value) for value in values]
    datetimes = [value for value in parsed if value is not None]
    if not datetimes:
        return None
    return max(datetimes)


def _parse_datetime_like(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return ensure_utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _position_current_price_from_unrealized(
    *,
    entry_price: float,
    size: float,
    unrealized_pnl: float,
    side: str,
) -> float:
    if size <= 0:
        return entry_price
    if side.upper() == "SHORT":
        return _clamp_price(entry_price - (unrealized_pnl / size))
    return _clamp_price(entry_price + (unrealized_pnl / size))


def _market_label(repo: Repository, token_id: str, condition_id: str) -> str:
    market = repo.get_market_for_token(token_id)
    if market is None and condition_id:
        market = repo.get_market_by_condition_id(condition_id)
    question = getattr(market, "question", None) if market is not None else None
    return str(question or condition_id or token_id or "unknown position")


def _clamp_price(value: float) -> float:
    if value <= 0:
        return 0.01
    return min(max(value, 0.01), 0.99)


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _resolve_console_auth(settings: Settings) -> ConsoleAuthConfig:
    username = settings.console_auth_username.strip()
    password = settings.console_auth_password
    if bool(username) != bool(password):
        raise ValueError(
            "Console auth requires both PM_ALPHA_CONSOLE_AUTH_USERNAME "
            "and PM_ALPHA_CONSOLE_AUTH_PASSWORD."
        )
    realm = settings.console_auth_realm.strip() or "PM Alpha Bot Console"
    return ConsoleAuthConfig(realm=realm, username=username, password=password)


def _escape_http_auth_realm(realm: str) -> str:
    return realm.replace("\\", "\\\\").replace('"', '\\"')


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if hasattr(value, "__dict__") and not isinstance(value, (str, bytes)):
        raw = {key: item for key, item in vars(value).items() if not key.startswith("_")}
        return _json_safe(raw)
    return value


def _encode_sse_event(event_name: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps(_json_safe(payload), ensure_ascii=False, separators=(",", ":"))
    lines = [f"event: {event_name}"]
    lines.extend(f"data: {line}" for line in data.splitlines() or [""])
    return ("\n".join(lines) + "\n\n").encode("utf-8")


_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PM Alpha Bot Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link
    href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap"
    rel="stylesheet"
  >
  <style>
    :root {
      color-scheme: light;
      --bg: #f3eedf;
      --panel: #fffaf0;
      --panel-2: #f8f0df;
      --terminal-bg: #fffdf7;
      --ink: #172018;
      --muted: #697260;
      --line: #ded3bd;
      --line-strong: #9f9276;
      --green: #166f3f;
      --amber: #9b6512;
      --red: #b64037;
      --grid-line-color: rgba(73, 60, 39, 0.055);
      --body-overlay-top: rgba(255, 255, 255, 0.58);
      --body-overlay-bottom: rgba(255, 255, 255, 0.12);
      --body-overlay-side: rgba(139, 112, 66, 0.08);
      --hero-bg: linear-gradient(180deg, rgba(255, 255, 255, 0.82), rgba(245, 236, 216, 0.92));
      --inset-glow: inset 0 0 0 1px rgba(21, 111, 63, 0.08);
      --subtle-fill: rgba(43, 56, 42, 0.035);
      --runtime-line: rgba(48, 64, 54, 0.13);
      --stream-live-bg: rgba(22, 111, 63, 0.11);
      --stream-live-ink: #166f3f;
      --stream-live-line: rgba(22, 111, 63, 0.28);
      --stream-idle-bg: rgba(155, 101, 18, 0.12);
      --stream-idle-ink: #9b6512;
      --stream-idle-line: rgba(155, 101, 18, 0.24);
      --stream-error-bg: rgba(182, 64, 55, 0.11);
      --stream-error-ink: #b64037;
      --stream-error-line: rgba(182, 64, 55, 0.26);
      --radius: 0px;
    }

    :root[data-theme="dark"] {
      color-scheme: dark;
      --bg: #000000;
      --panel: #050505;
      --panel-2: #020202;
      --terminal-bg: #000000;
      --ink: #d7e1d5;
      --muted: #7f8b82;
      --line: #1a221d;
      --line-strong: #304036;
      --green: #7fe6a6;
      --amber: #f0c46b;
      --red: #ff8d85;
      --grid-line-color: rgba(127, 230, 166, 0.05);
      --body-overlay-top: rgba(255, 255, 255, 0.015);
      --body-overlay-bottom: rgba(255, 255, 255, 0.0);
      --body-overlay-side: rgba(127, 230, 166, 0.02);
      --hero-bg: linear-gradient(180deg, rgba(127, 230, 166, 0.05), rgba(0, 0, 0, 0.88));
      --inset-glow: inset 0 0 0 1px rgba(127, 230, 166, 0.06);
      --subtle-fill: rgba(255, 255, 255, 0.02);
      --runtime-line: rgba(127, 230, 166, 0.08);
      --stream-live-bg: rgba(110, 207, 153, 0.12);
      --stream-live-ink: #79d7a4;
      --stream-live-line: rgba(110, 207, 153, 0.22);
      --stream-idle-bg: rgba(240, 179, 92, 0.12);
      --stream-idle-ink: #f0b35c;
      --stream-idle-line: rgba(240, 179, 92, 0.2);
      --stream-error-bg: rgba(255, 141, 133, 0.12);
      --stream-error-ink: #ff8d85;
      --stream-error-line: rgba(255, 141, 133, 0.22);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      color: var(--ink);
      font-family: "IBM Plex Mono", "SFMono-Regular", "Roboto Mono", monospace;
      background:
        linear-gradient(var(--grid-line-color) 1px, transparent 1px),
        linear-gradient(90deg, var(--grid-line-color) 1px, transparent 1px),
        var(--bg);
      background-size: 20px 20px, 20px 20px, auto;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(180deg, var(--body-overlay-top), var(--body-overlay-bottom)),
        linear-gradient(90deg, var(--body-overlay-side), transparent 48%);
      opacity: 0.65;
    }

    a {
      color: inherit;
    }

    .shell {
      width: min(1500px, calc(100vw - 24px));
      margin: 0 auto;
      padding: 48px 0 28px;
      position: relative;
      z-index: 1;
    }

    .utility-stack {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }

    .utility-inline {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .top-controls {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }

    .utility-pill,
    .badge,
    .theme-toggle {
      border: 1px solid var(--line-strong);
      padding: 6px 8px;
      font-size: 0.68rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      background: var(--panel);
      color: var(--ink);
    }

    .theme-toggle {
      cursor: pointer;
      font: inherit;
      min-width: 132px;
    }

    .theme-toggle:hover {
      border-color: var(--green);
      color: var(--green);
    }

    .stream-indicator[data-state="live"] {
      background: var(--stream-live-bg);
      border-color: var(--stream-live-line);
      color: var(--stream-live-ink);
    }

    .stream-indicator[data-state="connecting"],
    .stream-indicator[data-state="stale"] {
      background: var(--stream-idle-bg);
      border-color: var(--stream-idle-line);
      color: var(--stream-idle-ink);
    }

    .stream-indicator[data-state="error"] {
      background: var(--stream-error-bg);
      border-color: var(--stream-error-line);
      color: var(--stream-error-ink);
    }

    .hero {
      border: 1px solid var(--line-strong);
      background: var(--hero-bg);
      padding: 16px 18px;
      box-shadow: var(--inset-glow);
    }

    .hero-terminal-head,
    .hero-terminal-foot,
    .mode-banner,
    .panel-heading,
    .terminal-bar,
    .footer-meta {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }

    .hero-terminal-kicker,
    .hero-terminal-age,
    .panel-heading .eyebrow,
    .footer-meta,
    .muted {
      color: var(--muted);
      font-size: 0.68rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .cli-banner {
      margin: 14px 0;
      padding: 14px;
      white-space: pre-wrap;
      background: var(--terminal-bg);
      border: 1px solid rgba(127, 230, 166, 0.18);
      font-size: 0.82rem;
      line-height: 1.7;
      min-height: 124px;
    }

    .hero-ops-meta {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }

    .hero-ops-meta span,
    .terminal-line span {
      border: 1px solid var(--line);
      padding: 6px 8px;
      background: var(--subtle-fill);
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }

    .hero-terminal-foot strong {
      color: var(--green);
      font-weight: 600;
    }

    .mode-banner,
    .operator-summary,
    .decision-strip,
    .panel,
    .terminal-window,
    details.detail-block {
      margin-top: 10px;
      border: 1px solid var(--line);
      background: var(--panel);
      box-shadow: var(--inset-glow);
    }

    .mode-banner {
      padding: 12px;
    }

    .operator-summary {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 8px;
      padding: 10px;
      background: var(--terminal-bg);
      border-color: var(--line-strong);
    }

    .operator-card {
      border: 1px solid var(--line);
      background: var(--panel-2);
      padding: 10px;
      min-height: 96px;
    }

    .operator-card .label {
      color: var(--muted);
      display: block;
      font-size: 0.66rem;
      letter-spacing: 0.12em;
      margin-bottom: 6px;
      text-transform: uppercase;
    }

    .operator-card .value {
      display: block;
      font-size: clamp(0.98rem, 1.2vw, 1.35rem);
      font-weight: 700;
      letter-spacing: 0.02em;
      margin: 4px 0 6px;
      text-transform: uppercase;
    }

    .mode-title {
      font-size: clamp(1rem, 1.6vw, 1.45rem);
      letter-spacing: 0.18em;
      text-transform: uppercase;
    }

    .mode-cells {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      width: 100%;
    }

    .mode-cell,
    .status-card,
    .decision-card {
      padding: 10px 12px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      min-height: 104px;
    }

    .mode-cell .label,
    .status-card .label,
    .decision-card .label {
      color: var(--muted);
      font-size: 0.68rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin-bottom: 8px;
      display: block;
    }

    .card-description,
    .panel-description {
      color: var(--muted);
      font-size: 0.72rem;
      line-height: 1.45;
      letter-spacing: 0.02em;
      text-transform: none;
    }

    .card-description {
      min-height: 2.1em;
      margin: -2px 0 10px;
    }

    .mode-cell .value,
    .status-card .value,
    .decision-card .value {
      font-size: 0.9rem;
      text-transform: uppercase;
    }

    .status-grid,
    .decision-grid,
    .panel-grid {
      display: grid;
      gap: 8px;
    }

    .status-grid {
      grid-template-columns: repeat(5, minmax(0, 1fr));
      margin-top: 10px;
    }

    .decision-strip {
      padding: 12px;
    }

    .decision-head {
      font-size: 0.94rem;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }

    .decision-grid {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }

    .panel-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin-top: 10px;
    }

    .panel {
      padding: 12px;
    }

    .reject-blockers {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }

    .position-risk-list {
      display: grid;
      gap: 8px;
    }

    .position-risk-row {
      display: grid;
      grid-template-columns: minmax(260px, 1.6fr) repeat(4, minmax(112px, 0.7fr));
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      background: var(--panel-2);
    }

    .position-risk-main strong,
    .position-risk-metric strong {
      display: block;
      font-size: 0.82rem;
      line-height: 1.35;
    }

    .position-risk-main span,
    .position-risk-metric span,
    .position-risk-metric em {
      color: var(--muted);
      display: block;
      font-size: 0.68rem;
      font-style: normal;
      letter-spacing: 0.08em;
      line-height: 1.45;
      text-transform: uppercase;
    }

    .position-risk-main strong {
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .panel-heading {
      margin-bottom: 10px;
    }

    .panel-title {
      font-size: 0.82rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }

    .panel-description {
      margin-top: 4px;
    }

    .terminal-window {
      overflow: hidden;
    }

    .terminal-bar {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: rgba(127, 230, 166, 0.05);
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 0.7rem;
    }

    .terminal-body {
      background: var(--terminal-bg);
      padding: 12px;
      min-height: 200px;
      max-height: 380px;
      overflow: auto;
    }

    .terminal-line {
      display: grid;
      grid-template-columns: 132px 72px 1fr;
      gap: 8px;
      padding: 6px 0;
      border-bottom: 1px solid var(--runtime-line);
      font-size: 0.76rem;
    }

    details.detail-block {
      padding: 0;
    }

    details.detail-block summary {
      cursor: pointer;
      list-style: none;
      padding: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 0.76rem;
    }

    details.detail-block[open] summary {
      border-bottom: 1px solid var(--line);
    }

    .detail-body {
      padding: 12px;
    }

    table.data-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.76rem;
    }

    table.data-table th,
    table.data-table td {
      border-bottom: 1px solid var(--line);
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }

    table.data-table th {
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-size: 0.68rem;
    }

    .panel-empty {
      color: var(--muted);
      font-size: 0.76rem;
      padding: 8px 0 2px;
    }

    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .actions button,
    .actions input {
      font: inherit;
      border-radius: 0;
      border: 1px solid var(--line-strong);
      background: var(--terminal-bg);
      color: var(--ink);
      padding: 8px 10px;
    }

    .actions button {
      text-transform: uppercase;
      letter-spacing: 0.12em;
      cursor: pointer;
    }

    .actions button:hover:not(:disabled) {
      border-color: var(--green);
      color: var(--green);
    }

    .actions button:disabled {
      cursor: not-allowed;
      opacity: 0.5;
    }

    .tone-ok {
      border-color: var(--stream-live-line);
      color: var(--stream-live-ink);
    }

    .tone-warn {
      border-color: var(--stream-idle-line);
      color: var(--stream-idle-ink);
    }

    .tone-error {
      border-color: var(--stream-error-line);
      color: var(--stream-error-ink);
    }

    .footer-meta {
      margin-top: 12px;
      padding: 10px 0 4px;
      border-top: 1px solid var(--line);
    }

    pre.json-preview {
      margin: 0;
      padding: 10px;
      border: 1px solid var(--line);
      background: var(--terminal-bg);
      font-size: 0.72rem;
      overflow: auto;
    }

    @media (max-width: 980px) {
      .operator-summary {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .mode-cells,
      .status-grid,
      .decision-grid,
      .panel-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 720px) {
      .shell {
        width: min(100vw - 16px, 100%);
        padding-top: 24px;
      }

      .mode-cells,
      .operator-summary,
      .position-risk-row,
      .status-grid,
      .decision-grid,
      .panel-grid {
        grid-template-columns: 1fr;
      }

      table.data-table thead {
        display: none;
      }

      table.data-table,
      table.data-table tbody,
      table.data-table tr,
      table.data-table td {
        display: block;
        width: 100%;
      }

      table.data-table tr {
        padding: 8px 0;
        border-bottom: 1px solid var(--line);
      }

      table.data-table td {
        border-bottom: 0;
        padding: 4px 0;
      }

      table.data-table td::before {
        content: attr(data-label);
        display: block;
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.12em;
        font-size: 0.66rem;
        margin-bottom: 2px;
      }

      .terminal-line {
        grid-template-columns: 1fr;
      }
    }
  </style>
  <script>
    (function () {
      try {
        const storedTheme = window.localStorage.getItem("pm-alpha-console-theme");
        if (storedTheme === "dark") {
          document.documentElement.dataset.theme = "dark";
        }
      } catch (_) {
        document.documentElement.dataset.theme = "light";
      }
    })();
  </script>
</head>
<body>
  <main class="shell">
    <div class="top-controls">
      <button
        class="theme-toggle"
        id="theme-toggle"
        type="button"
        aria-label="다크 모드로 전환"
      >
        다크 모드
      </button>
      <div class="utility-stack">
        <span
          class="utility-pill stream-indicator"
          id="stream-status"
          data-state="connecting"
        >stream connecting</span>
        <span class="utility-inline" id="utility-stack"></span>
      </div>
    </div>

    <section
      class="operator-summary"
      id="operator-summary"
      aria-label="투자자용 상단 운영 요약"
    ></section>

    <section class="hero hero-terminal">
      <div class="hero-terminal-head">
        <span class="hero-terminal-kicker">root@ops:~/pm-alpha-bot/console</span>
        <span class="hero-terminal-age" id="generated-age">loading</span>
      </div>
      <pre class="cli-banner" id="hero-banner">LOADING CONSOLE PAYLOAD...</pre>
      <div class="hero-ops-meta" id="hero-meta"></div>
      <div class="hero-terminal-foot">
        <span id="hero-status">system health: <strong>loading</strong></span>
        <span id="hero-footnote">poll interval 5s</span>
      </div>
    </section>

    <section class="mode-banner">
      <div class="mode-title" id="mode-title">loading</div>
      <div class="mode-cells" id="mode-cells"></div>
    </section>

    <section class="status-grid" id="status-grid"></section>

    <section class="decision-strip">
      <div class="decision-head" id="decision-head">loading</div>
      <div class="decision-grid" id="decision-grid"></div>
    </section>

    <section class="panel position-risk-panel">
      <div class="panel-heading">
        <div>
          <div class="eyebrow">risk</div>
          <div class="panel-title">position risk</div>
          <div class="panel-description">
            포지션별 진입가, 현재가, 손익, 목표가와 손절가까지의 거리를 보여줍니다.
            목표/손절은 현재 설정값으로 계산한 감시용 밴드이며 자동 청산 명령은 아닙니다.
          </div>
        </div>
        <span class="muted">entry / mark / pnl / target / stop</span>
      </div>
      <div class="position-risk-list" id="position-risk-list"></div>
    </section>

    <section class="panel service-heartbeat-panel">
      <div class="panel-heading">
        <div>
          <div class="eyebrow">heartbeats</div>
          <div class="panel-title">service heartbeat</div>
          <div class="panel-description">
            scanner, wallet-tracker, stream, signal, exec 등 핵심 컴포넌트가 마지막으로
            정상 데이터를 남긴 시각입니다. 별도 워커 heartbeat가 없으면 DB 최신
            데이터와 runtime event를 기준으로 계산합니다.
          </div>
        </div>
        <span class="muted">component / status / last seen / source</span>
      </div>
      <div id="service-heartbeats-table"></div>
    </section>

    <section class="panel execution-reject-panel">
      <div class="panel-heading">
        <div>
          <div class="eyebrow">execution</div>
          <div class="panel-title">reject reasons</div>
          <div class="panel-description">
            최근 주문 제출이 건너뛰어지거나 거절된 이유를 집계합니다. 실거래 전에는
            주문이 왜 나가지 않았는지 확인하는 용도이며, 현재 차단 조건도 함께 표시합니다.
          </div>
        </div>
        <span class="muted">current blockers / recent rejects</span>
      </div>
      <div class="reject-blockers" id="execution-current-blockers"></div>
      <div id="execution-reject-summary-table"></div>
      <div id="execution-reject-recent-table"></div>
    </section>

    <section class="panel-grid">
      <section class="panel">
        <div class="panel-heading">
          <div>
            <div class="eyebrow">signals</div>
            <div class="panel-title">latest signal queue</div>
            <div class="panel-description">
              전략이 지금 매수 후보로 인정해 저장한 최신 신호 목록입니다.
            </div>
          </div>
          <span class="muted">edge / fair value / entry</span>
        </div>
        <div id="signals-table"></div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <div>
            <div class="eyebrow">observations</div>
            <div class="panel-title">promotion-ready empty books</div>
            <div class="panel-description">
              주문장은 비었지만 최근 거래 흔적과 지갑 움직임 때문에 지켜보는 후보입니다.
            </div>
          </div>
          <span class="muted">score / ready / last trade</span>
        </div>
        <div id="observations-table"></div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <div>
            <div class="eyebrow">promotion queue</div>
            <div class="panel-title">persistent observation candidates</div>
            <div class="panel-description">
              여러 수집 주기 동안 반복해서 관찰되어 우선 검토할 후보입니다.
            </div>
          </div>
          <span class="muted">streak / score / last trade</span>
        </div>
        <div id="promotion-queue-table"></div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <div>
            <div class="eyebrow">signal watch</div>
            <div class="panel-title">observation runtime events</div>
            <div class="panel-description">
              관찰 후보가 준비, 대기, 해제된 기록을 시간순으로 보여줍니다.
            </div>
          </div>
          <span class="muted">ready / queued / cleared</span>
        </div>
        <div id="signal-events-table"></div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <div>
            <div class="eyebrow">wallets</div>
            <div class="panel-title">top scored wallets</div>
            <div class="panel-description">
              리더보드와 거래 이력을 기준으로 점수가 높은 추적 지갑입니다.
            </div>
          </div>
          <span class="muted">roi / pnl / concentration</span>
        </div>
        <div id="wallets-table"></div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <div>
            <div class="eyebrow">live</div>
            <div class="panel-title">open live orders</div>
            <div class="panel-description">
              실거래 모드에서 거래소에 아직 남아 있는 주문 목록입니다.
            </div>
          </div>
          <span class="muted">status / price / size</span>
        </div>
        <div id="orders-table"></div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <div>
            <div class="eyebrow">actions</div>
            <div class="panel-title">restricted operator controls</div>
            <div class="panel-description">
              kill switch 초기화, 실거래 동기화 같은 제한된 관리 버튼입니다.
            </div>
          </div>
          <span class="muted">disabled by default</span>
        </div>
        <div class="actions">
          <button id="reset-kill-switch-btn">reset kill switch</button>
          <button id="sync-live-btn">sync live</button>
          <button id="preview-prune-btn">preview prune</button>
          <button id="apply-prune-btn">apply prune</button>
        </div>
        <div class="panel-empty" id="action-status">actions disabled</div>
      </section>
    </section>

    <section class="terminal-window">
      <div class="terminal-bar">
        <span>runtime events</span>
        <span id="events-count">0 rows</span>
      </div>
      <div class="terminal-body" id="terminal-body"></div>
    </section>

    <details class="detail-block" open>
      <summary>
        <span>live positions</span>
        <span class="badge" id="live-positions-badge">0</span>
      </summary>
      <div class="detail-body" id="live-positions-table"></div>
    </details>

    <details class="detail-block">
      <summary>
        <span>paper positions</span>
        <span class="badge" id="paper-positions-badge">0</span>
      </summary>
      <div class="detail-body" id="paper-positions-table"></div>
    </details>

    <details class="detail-block">
      <summary><span>health payload</span><span class="badge">json</span></summary>
      <div class="detail-body"><pre class="json-preview" id="health-json">{}</pre></div>
    </details>

    <footer class="footer-meta">
      <span id="footer-left">generated</span>
      <span id="footer-right">source / api-console</span>
    </footer>
  </main>

  <script>
    const refreshIntervalMs = 5000;
    const streamReconnectMs = 1500;
    const themeStorageKey = "pm-alpha-console-theme";
    let actionsEnabled = false;
    let fallbackTimer = null;
    let streamReconnectTimer = null;
    let streamSource = null;

    function currentTheme() {
      return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    }

    function updateThemeToggle() {
      const theme = currentTheme();
      const button = document.getElementById("theme-toggle");
      const nextLabel = theme === "dark" ? "라이트 모드" : "다크 모드";
      button.textContent = nextLabel;
      button.setAttribute("aria-label", `${nextLabel}로 전환`);
    }

    function setTheme(theme) {
      const resolved = theme === "dark" ? "dark" : "light";
      document.documentElement.dataset.theme = resolved;
      try {
        window.localStorage.setItem(themeStorageKey, resolved);
      } catch (_) {
        // Theme persistence is optional; rendering should continue without storage.
      }
      updateThemeToggle();
    }

    function esc(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function toneClass(tone) {
      if (tone === "ok") return "tone-ok";
      if (tone === "error") return "tone-error";
      return "tone-warn";
    }

    function formatNumber(value, digits = 2) {
      if (value === null || value === undefined || value === "") return "n/a";
      const numeric = Number(value);
      if (Number.isNaN(numeric)) return String(value);
      return numeric.toFixed(digits);
    }

    function formatPercent(value, digits = 1) {
      if (value === null || value === undefined || value === "") return "n/a";
      const numeric = Number(value);
      if (Number.isNaN(numeric)) return String(value);
      return (numeric * 100).toFixed(digits) + "%";
    }

    function formatUsd(value) {
      if (value === null || value === undefined || value === "") return "n/a";
      const numeric = Number(value);
      if (Number.isNaN(numeric)) return String(value);
      const sign = numeric < 0 ? "-" : "";
      return sign + "$" + Math.abs(numeric).toFixed(2);
    }

    function formatRelativeTime(isoString) {
      if (!isoString) return "unknown";
      const deltaMs = Date.now() - new Date(isoString).getTime();
      const deltaSec = Math.max(0, Math.round(deltaMs / 1000));
      if (deltaSec < 60) return deltaSec + "s ago";
      if (deltaSec < 3600) return (deltaSec / 60).toFixed(1) + "m ago";
      return (deltaSec / 3600).toFixed(1) + "h ago";
    }

    function renderPills(targetId, items) {
      const target = document.getElementById(targetId);
      target.innerHTML = items
        .map((item) => `<span class="utility-pill">${esc(item)}</span>`)
        .join("");
    }

    function renderModeCells(cells) {
      const target = document.getElementById("mode-cells");
      target.innerHTML = cells.map((cell) => `
        <div class="mode-cell ${toneClass(cell.tone)}">
          <span class="label">${esc(cell.label)}</span>
          <div class="card-description">${esc(cell.description)}</div>
          <span class="value">${esc(cell.value)}</span>
        </div>
      `).join("");
    }

    function renderCards(targetId, cards) {
      const target = document.getElementById(targetId);
      const className = targetId === "decision-grid" ? "decision-card" : "status-card";
      target.innerHTML = cards.map((card) => `
        <div class="${className} ${toneClass(card.tone)} ${card.prominent ? "prominent" : ""}">
          <span class="label">${esc(card.label)}</span>
          <div class="card-description">${esc(card.description)}</div>
          <div class="value">${esc(card.value)}</div>
          <div class="muted">${esc(card.detail)}</div>
        </div>
      `).join("");
    }

    function renderOperatorSummary(cards) {
      const target = document.getElementById("operator-summary");
      target.innerHTML = cards.map((card) => `
        <div class="operator-card ${toneClass(card.tone)}">
          <span class="label">${esc(card.label)}</span>
          <span class="value">${esc(card.value)}</span>
          <div class="card-description">${esc(card.description)}</div>
          <div class="muted">${esc(card.detail)}</div>
        </div>
      `).join("");
    }

    function renderPositionRisks(rows) {
      const target = document.getElementById("position-risk-list");
      if (!rows || rows.length === 0) {
        target.innerHTML = `<div class="panel-empty">No paper or live positions to monitor.</div>`;
        return;
      }
      target.innerHTML = rows.map((row) => `
        <div class="position-risk-row ${toneClass(row.tone)}">
          <div class="position-risk-main">
            <strong title="${esc(row.market_label)}">${esc(row.market_label)}</strong>
            <span>${esc(row.mode)} · ${esc(row.side)} · ${esc(row.token_id)}</span>
            <span>${esc(row.rule_note || "")}</span>
          </div>
          <div class="position-risk-metric">
            <span>entry / now</span>
            <strong>
              ${formatNumber(row.entry_price, 4)} -> ${formatNumber(row.current_price, 4)}
            </strong>
            <em>size ${formatNumber(row.size, 2)}</em>
          </div>
          <div class="position-risk-metric">
            <span>P/L</span>
            <strong>${formatPercent(row.pnl_pct)}</strong>
            <em>${formatUsd(row.pnl_cash)}</em>
          </div>
          <div class="position-risk-metric">
            <span>target</span>
            <strong>${formatNumber(row.target_price, 4)}</strong>
            <em>to ${formatPercent(row.target_distance_pct)}</em>
          </div>
          <div class="position-risk-metric">
            <span>stop buffer</span>
            <strong>${formatNumber(row.stop_price, 4)}</strong>
            <em>${formatPercent(row.stop_distance_pct)} left</em>
          </div>
        </div>
      `).join("");
    }

    function renderServiceHeartbeats(rows) {
      const target = document.getElementById("service-heartbeats-table");
      if (!rows || rows.length === 0) {
        target.innerHTML = `<div class="panel-empty">No service heartbeat data available.</div>`;
        return;
      }
      const tbody = rows.map((row) => `
        <tr>
          <td data-label="component">${esc(row.label || row.component)}</td>
          <td data-label="status">
            <span class="${toneClass(row.tone)}">${esc(row.status)}</span>
          </td>
          <td data-label="last seen">
            ${esc(row.last_seen ? formatRelativeTime(row.last_seen) : "n/a")}
          </td>
          <td data-label="age">${esc(row.age_label || "n/a")}</td>
          <td data-label="source">${esc(row.source || "")}</td>
          <td data-label="detail">${esc(row.detail || "")}</td>
        </tr>
      `).join("");
      target.innerHTML = [
        `<table class="data-table">`,
        `<thead><tr>`,
        `<th>component</th><th>status</th><th>last seen</th>`,
        `<th>age</th><th>source</th><th>detail</th>`,
        `</tr></thead>`,
        `<tbody>${tbody}</tbody>`,
        `</table>`
      ].join("");
    }

    function renderTable(targetId, columns, rows, emptyText) {
      const target = document.getElementById(targetId);
      if (!rows || rows.length === 0) {
        target.innerHTML = `<div class="panel-empty">${esc(emptyText)}</div>`;
        return;
      }
      const thead = columns.map((column) => `<th>${esc(column.label)}</th>`).join("");
      const tbody = rows.map((row) => `
        <tr>
          ${columns
            .map((column) => (
              `<td data-label="${esc(column.label)}">${esc(row[column.key] ?? "")}</td>`
            ))
            .join("")}
        </tr>
      `).join("");
      target.innerHTML = [
        `<table class="data-table">`,
        `<thead><tr>${thead}</tr></thead>`,
        `<tbody>${tbody}</tbody>`,
        `</table>`
      ].join("");
    }

    function renderExecutionRejects(report) {
      const resolved = report || {};
      const blockers = resolved.current_blockers || [];
      const blockerTarget = document.getElementById("execution-current-blockers");
      if (blockers.length === 0) {
        blockerTarget.innerHTML = `<span class="utility-pill tone-ok">no current blockers</span>`;
      } else {
        blockerTarget.innerHTML = blockers.map((row) => `
          <span class="utility-pill ${toneClass(row.tone)}" title="${esc(row.detail || "")}">
            ${esc(row.reason)}
          </span>
        `).join("");
      }

      renderTable("execution-reject-summary-table", [
        {key: "reason", label: "reason"},
        {key: "count", label: "count"},
        {key: "source", label: "source"},
        {key: "last_seen", label: "last seen"},
        {key: "detail", label: "detail"}
      ], (resolved.summary || []).map((row) => ({
        ...row,
        last_seen: row.last_seen ? formatRelativeTime(row.last_seen) : "current"
      })), "No recent reject reasons recorded.");

      renderTable("execution-reject-recent-table", [
        {key: "ts", label: "ts"},
        {key: "source", label: "source"},
        {key: "reason", label: "reason"},
        {key: "status", label: "status"},
        {key: "token_id", label: "token"},
        {key: "detail", label: "detail"}
      ], (resolved.recent || []).map((row) => ({
        ...row,
        ts: row.ts ? formatRelativeTime(row.ts) : ""
      })), "No supervisor skips or terminal rejected orders recorded.");
    }

    function renderEvents(events) {
      const target = document.getElementById("terminal-body");
      document.getElementById("events-count").textContent = `${events.length} rows`;
      if (!events || events.length === 0) {
        target.innerHTML = `<div class="panel-empty">No runtime events recorded.</div>`;
        return;
      }
      target.innerHTML = events.map((event) => `
        <div class="terminal-line">
          <span>${esc(event.ts)}</span>
          <span class="${toneClass(
            event.level === "error" || event.level === "critical"
              ? "error"
              : event.level === "info"
                ? "ok"
                : "warn"
          )}">${esc(event.level)}</span>
          <span>${esc(event.event_type)} :: ${esc(event.message || "")}</span>
        </div>
      `).join("");
    }

    function setActionButtons(enabled) {
      [
        "reset-kill-switch-btn",
        "sync-live-btn",
        "preview-prune-btn",
        "apply-prune-btn"
      ].forEach((id) => {
        document.getElementById(id).disabled = !enabled;
      });
      document.getElementById("action-status").textContent = enabled
        ? "actions enabled for this server session"
        : "actions disabled; restart with --enable-actions to allow mutations";
    }

    function setStreamStatus(state, label) {
      const indicator = document.getElementById("stream-status");
      indicator.dataset.state = state;
      indicator.textContent = label;
    }

    function startFallbackPolling(label) {
      setStreamStatus("stale", label || "stream unavailable; polling fallback");
      if (fallbackTimer !== null) return;
      fallbackTimer = window.setInterval(() => {
        refreshConsole().catch((error) => {
          setStreamStatus("error", "polling failed");
          document.getElementById("action-status").textContent = error.message;
        });
      }, refreshIntervalMs);
      refreshConsole().catch((error) => {
        setStreamStatus("error", "initial polling failed");
        document.getElementById("hero-banner").textContent =
          `CONSOLE LOAD FAILED\\n${error.message}`;
      });
    }

    function stopFallbackPolling() {
      if (fallbackTimer === null) return;
      window.clearInterval(fallbackTimer);
      fallbackTimer = null;
    }

    function scheduleStreamReconnect() {
      if (streamReconnectTimer !== null) return;
      streamReconnectTimer = window.setTimeout(() => {
        streamReconnectTimer = null;
        connectEventStream();
      }, streamReconnectMs);
    }

    async function postAction(action, payload) {
      const response = await fetch(`/api/actions/${action}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload || {})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.error || `Action failed with status ${response.status}`);
      }
      document.getElementById("action-status").textContent =
        `${action} ok :: ${JSON.stringify(data.result)}`;
      await refreshConsole();
    }

    function renderConsolePayload(payload, sourceLabel) {
      actionsEnabled = Boolean(payload.context.actions_enabled);
      setActionButtons(actionsEnabled);

      const summary = payload.summary;
      document.getElementById("generated-age").textContent =
        formatRelativeTime(payload.generated_at);
      document.getElementById("hero-banner").textContent = summary.hero_banner;
      renderPills("utility-stack", [
        `env ${payload.context.app_env}`,
        `db ${payload.context.database_backend}`,
        `mode ${summary.run_mode}`,
        `indicator ${summary.live_indicator}`
      ]);
      renderOperatorSummary(summary.operator_summary || []);
      renderPositionRisks(payload.position_risks || []);
      renderServiceHeartbeats(payload.service_heartbeats || []);
      renderExecutionRejects(payload.execution_rejects || {});
      renderPills("hero-meta", summary.top_meta_items);
      document.getElementById("hero-status").innerHTML =
        `system health: <strong>${esc(summary.primary_status)}</strong>`;
      document.getElementById("hero-footnote").textContent = summary.decision_summary;
      document.getElementById("mode-title").textContent = summary.run_mode;
      renderModeCells(summary.mode_cells);
      renderCards("status-grid", summary.status_cards);
      document.getElementById("decision-head").textContent = summary.decision_summary;
      renderCards("decision-grid", summary.decision_cards);
      renderTable("signals-table", [
        {key: "token_id", label: "token"},
        {key: "direction", label: "direction"},
        {key: "edge_bps", label: "edge bps"},
        {key: "fair_prob", label: "fair prob"},
        {key: "effective_entry_price", label: "entry"}
      ], payload.signals.map((row) => ({
        ...row,
        edge_bps: formatNumber(row.edge_bps, 1),
        fair_prob: formatNumber(row.fair_prob, 3),
        effective_entry_price: formatNumber(row.effective_entry_price, 3)
      })), "No signals stored.");
      renderTable("observations-table", [
        {key: "token_id", label: "token"},
        {key: "reason", label: "reason"},
        {key: "observation_streak", label: "streak"},
        {key: "observation_score", label: "score"},
        {key: "promotion_ready", label: "ready"},
        {key: "last_trade_price", label: "last trade"}
      ], payload.signal_observations.map((row) => ({
        ...row,
        observation_streak: row.observation_streak ?? 0,
        observation_score: formatNumber(row.observation_score, 3),
        promotion_ready: row.promotion_ready ? "yes" : "no",
        last_trade_price: formatNumber(row.last_trade_price, 3)
      })), "No observation candidates.");
      renderTable("promotion-queue-table", [
        {key: "token_id", label: "token"},
        {key: "observation_streak", label: "streak"},
        {key: "observation_score", label: "score"},
        {key: "last_trade_price", label: "last trade"},
        {key: "reason", label: "reason"}
      ], payload.signal_promotion_queue.map((row) => ({
        ...row,
        observation_streak: row.observation_streak ?? 0,
        observation_score: formatNumber(row.observation_score, 3),
        last_trade_price: formatNumber(row.last_trade_price, 3)
      })), "No persistent promotion queue candidates.");
      renderTable("signal-events-table", [
        {key: "ts", label: "ts"},
        {key: "event_type", label: "type"},
        {key: "level", label: "level"},
        {key: "message", label: "message"}
      ], (payload.signal_events || []).map((row) => ({
        ...row,
        ts: row.ts || "",
        event_type: row.event_type || "",
        level: row.level || "",
        message: row.message || ""
      })), "No signal observation events recorded.");
      renderTable("wallets-table", [
        {key: "proxy_wallet", label: "wallet"},
        {key: "category", label: "category"},
        {key: "score", label: "score"},
        {key: "roi", label: "roi"},
        {key: "pnl", label: "pnl"}
      ], payload.wallets.map((row) => ({
        ...row,
        score: formatNumber(row.score, 3),
        roi: formatNumber(row.roi, 3),
        pnl: formatNumber(row.pnl, 2)
      })), "No wallet scores stored.");
      renderTable("orders-table", [
        {key: "created_at", label: "created"},
        {key: "token_id", label: "token"},
        {key: "side", label: "side"},
        {key: "price", label: "price"},
        {key: "status", label: "status"}
      ], payload.live_orders.map((row) => ({
        ...row,
        price: formatNumber(row.price, 3)
      })), "No live orders stored.");
      renderEvents(payload.live_events || []);

      const livePositions = payload.live_report.positions || [];
      const paperPositions = payload.paper_report.positions || [];
      document.getElementById("live-positions-badge").textContent = String(livePositions.length);
      document.getElementById("paper-positions-badge").textContent = String(paperPositions.length);
      renderTable("live-positions-table", [
        {key: "token_id", label: "token"},
        {key: "side", label: "side"},
        {key: "size", label: "size"},
        {key: "avg_price", label: "avg price"},
        {key: "unrealized_pnl", label: "unrealized"}
      ], livePositions.map((row) => ({
        ...row,
        size: formatNumber(row.size, 3),
        avg_price: formatNumber(row.avg_price, 3),
        unrealized_pnl: formatNumber(row.unrealized_pnl, 2)
      })), "No live positions reconstructed.");
      renderTable("paper-positions-table", [
        {key: "token_id", label: "token"},
        {key: "side", label: "side"},
        {key: "size", label: "size"},
        {key: "avg_price", label: "avg price"},
        {key: "unrealized_pnl", label: "unrealized"}
      ], paperPositions.map((row) => ({
        ...row,
        size: formatNumber(row.size, 3),
        avg_price: formatNumber(row.avg_price, 3),
        unrealized_pnl: formatNumber(row.unrealized_pnl, 2)
      })), "No paper positions stored.");
      document.getElementById("health-json").textContent = JSON.stringify(payload.health, null, 2);
      document.getElementById("footer-left").textContent = `generated ${payload.generated_at}`;
      document.getElementById("footer-right").textContent = `source / ${sourceLabel}`;
    }

    async function refreshConsole() {
      const response = await fetch("/api/console", {cache: "no-store"});
      if (!response.ok) {
        throw new Error(`Failed to fetch console payload: ${response.status}`);
      }
      const payload = await response.json();
      renderConsolePayload(payload, "/api/console");
    }

    function connectEventStream() {
      if (!window.EventSource) {
        startFallbackPolling("SSE unsupported; polling /api/console");
        return;
      }

      if (streamSource !== null) {
        streamSource.close();
        streamSource = null;
      }

      setStreamStatus("connecting", "stream connecting");
      streamSource = new EventSource("/events");

      streamSource.onopen = () => {
        stopFallbackPolling();
        setStreamStatus("live", "stream connected");
      };

      streamSource.addEventListener("console", (event) => {
        try {
          const payload = JSON.parse(event.data);
          renderConsolePayload(payload, "/events");
          setStreamStatus("live", `stream live ${new Date().toLocaleTimeString()}`);
        } catch (error) {
          setStreamStatus("error", "stream payload error");
          document.getElementById("action-status").textContent = error.message;
        }
      });

      streamSource.addEventListener("console-error", (event) => {
        try {
          const payload = JSON.parse(event.data);
          setStreamStatus("error", payload.error || "stream server error");
        } catch (_) {
          setStreamStatus("error", "stream server error");
        }
      });

      streamSource.onerror = () => {
        if (streamSource !== null) {
          streamSource.close();
          streamSource = null;
        }
        startFallbackPolling("stream reconnecting; polling fallback");
        scheduleStreamReconnect();
      };
    }

    document.getElementById("reset-kill-switch-btn").addEventListener("click", async () => {
      if (
        !actionsEnabled ||
        !window.confirm([
          "Reset the persisted live kill switch?",
          "Confirm only after reviewing the cause and active live orders."
        ].join(" "))
      ) return;
      await postAction("reset-live-kill-switch", {
        acknowledge: true,
        reason: "console_operator_confirmed_reset"
      });
    });

    document.getElementById("sync-live-btn").addEventListener("click", async () => {
      if (!actionsEnabled) return;
      const input = window.prompt("Sync limit", "50");
      if (input === null) return;
      await postAction("sync-live", {limit: Number(input)});
    });

    document.getElementById("preview-prune-btn").addEventListener("click", async () => {
      if (!actionsEnabled) return;
      const input = window.prompt("Preview prune older than days", "30");
      if (input === null) return;
      await postAction("prune-runtime-events", {older_than_days: Number(input), apply: false});
    });

    document.getElementById("apply-prune-btn").addEventListener("click", async () => {
      if (!actionsEnabled) return;
      const input = window.prompt("Apply prune older than days", "30");
      if (input === null) return;
      if (!window.confirm("Delete matching runtime events?")) return;
      await postAction("prune-runtime-events", {older_than_days: Number(input), apply: true});
    });

    document.getElementById("theme-toggle").addEventListener("click", () => {
      setTheme(currentTheme() === "dark" ? "light" : "dark");
    });
    updateThemeToggle();
    connectEventStream();
  </script>
</body>
</html>
"""

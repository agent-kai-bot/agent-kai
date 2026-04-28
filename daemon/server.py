"""Phase 2 daemon WebSocket server."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shlex
import shutil
import time
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from agent_logger import get_logger, log_slash_command
from agent.signal_consumer import Signal, SignalConsumer
from agent.strategy_agent_tools import (
    InProcessStrategyRuntime,
    get_strategy_lineage,
    move_strategy,
    optimizer_pause,
    optimizer_report,
    optimizer_start,
    optimizer_status,
    propose_strategy,
    render_strategy_command_result,
    show_strategy,
    list_strategies as list_strategy_records,
)
from config import (
    AGENTS,
    DEFAULT_AGENT,
    ENDPOINTS,
    NATS_URL,
    get_agent_config,
    list_endpoint_models,
    set_agent_reasoning_effort,
)
from daemon.auth import (
    DAEMON_TOKEN_PATH,
    ensure_daemon_token,
    is_local_client_host,
    parse_bearer_token,
)
from daemon.db import DEFAULT_DB_PATH as DEFAULT_DAEMON_DB_PATH, apply_migrations
from daemon.forgejo_webhook_auth import (
    HEADER_DELIVERY as FORGEJO_HEADER_DELIVERY,
    HEADER_EVENT as FORGEJO_HEADER_EVENT,
    HEADER_GITEA_EVENT as FORGEJO_HEADER_GITEA_EVENT,
    HEADER_SIGNATURE as FORGEJO_HEADER_SIGNATURE,
    SUPPORTED_EVENTS as FORGEJO_SUPPORTED_EVENTS,
    WebhookHeaderError as ForgejoWebhookHeaderError,
    WebhookSignatureError as ForgejoWebhookSignatureError,
    parse_headers as parse_forgejo_headers,
    verify_signature as verify_forgejo_signature,
)
from daemon.secrets import (
    WebhookSecretError,
    WebhookSecretProvider,
    default_forgejo_webhook_secret_provider,
)
from daemon.core import (
    DEFAULT_AUTO_MAX_ITERATIONS,
    MAX_AUTO_ITERATIONS,
    Session,
    SessionEvent,
    get_indexed_session,
    list_indexed_sessions,
    remove_indexed_session,
    serialize_messages,
)
from daemon.scheduler import DaemonEventBus, Scheduler
from daemon.protocol import (
    AttachEnvelope,
    AutoProgressEnvelope,
    AutoStartedEnvelope,
    AutoStoppedEnvelope,
    ChartBarEnvelope,
    ChartViewEnvelope,
    ClientEnvelope,
    ErrorEnvelope,
    FinalEnvelope,
    HeartbeatEnvelope,
    InputEnvelope,
    InterruptEnvelope,
    NatsEventEnvelope,
    OptimizerCompletedEnvelope,
    ScheduledJobCancelledEnvelope,
    ScheduledJobCompletedEnvelope,
    ScheduledJobCreatedEnvelope,
    ScheduledJobFailedEnvelope,
    ScheduledJobPausedEnvelope,
    ScheduledJobResumedEnvelope,
    ScheduledJobTriggeredEnvelope,
    SessionAttachedEnvelope,
    SessionStateSnapshot,
    SignalEnvelope,
    SlashEnvelope,
    StatusEnvelope,
    SubscribeEnvelope,
    TokenEnvelope,
    ToolEndEnvelope,
    ToolStartEnvelope,
    UnsubscribeEnvelope,
    WatchlistEnvelope,
    decode_client_envelope,
    encode_envelope,
)
from nats_bus.bus import NatsBus
from taskboard_gateway.app import create_gateway_app as create_taskboard_gateway_app

DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 8765
DEFAULT_DAEMON_WS_PATH = "/ws"
DEFAULT_DAEMON_WS_URL = (
    f"ws://{DEFAULT_DAEMON_HOST}:{DEFAULT_DAEMON_PORT}{DEFAULT_DAEMON_WS_PATH}"
)
DEFAULT_WEB_BUILD_DIR = Path(__file__).resolve().parent.parent / "web" / "build"
_SCHEDULE_IN_PATTERN = re.compile(
    r"^in\s+(?P<count>\d+)\s+(?P<unit>minute|minutes|hour|hours|day|days)$",
    re.IGNORECASE,
)
_SCHEDULE_TOMORROW_PATTERN = re.compile(
    r"^tomorrow(?:\s+(?P<time>.+))?$",
    re.IGNORECASE,
)
SNAPSHOT_CHAT_HISTORY_MAX_MESSAGES = 200
SNAPSHOT_CHAT_HISTORY_MAX_CHARS = 180_000
TOKEN_FLUSH_INTERVAL_SECONDS = 0.04
TOKEN_FLUSH_CHARS = 48


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_schedule_at_value(raw: str, *, now: datetime | None = None) -> str:
    """Parse a minimal slash-command time expression into an ISO timestamp."""
    reference = now or _utc_now()
    text = raw.strip()
    if not text:
        raise ValueError("schedule time cannot be empty")

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.replace(microsecond=0).isoformat()

    in_match = _SCHEDULE_IN_PATTERN.match(text)
    if in_match:
        count = int(in_match.group("count"))
        unit = in_match.group("unit").lower()
        if "minute" in unit:
            target = reference + timedelta(minutes=count)
        elif "hour" in unit:
            target = reference + timedelta(hours=count)
        else:
            target = reference + timedelta(days=count)
        return target.replace(microsecond=0).isoformat()

    tomorrow_match = _SCHEDULE_TOMORROW_PATTERN.match(text)
    if tomorrow_match:
        tomorrow = (reference + timedelta(days=1)).astimezone(timezone.utc)
        clock = (tomorrow_match.group("time") or "09:00").strip().lower()
        for fmt in ("%H:%M", "%H", "%I%p", "%I:%M%p", "%I %p", "%I:%M %p"):
            try:
                parsed_time = datetime.strptime(clock, fmt)
            except ValueError:
                continue
            target = tomorrow.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=0,
                microsecond=0,
            )
            return target.isoformat()
        raise ValueError(
            "unsupported tomorrow time format; "
            "use ISO, 'in N minutes', or 'tomorrow 7am'"
        )

    raise ValueError(
        "unsupported schedule time format; "
        "use ISO, 'in N minutes', or 'tomorrow 7am'"
    )


def _looks_like_web_asset_request(asset_path: str) -> bool:
    normalized = asset_path.strip("/")
    if not normalized:
        return False
    tail = normalized.rsplit("/", 1)[-1]
    return normalized.startswith("_app/") or "." in tail


def _split_slash_command(command_text: str) -> tuple[str, str]:
    stripped = command_text.strip()
    if not stripped:
        return ("", "")
    command, _, remainder = stripped.partition(" ")
    return (command, remainder.strip())


def _resolve_web_asset_path(build_dir: Path, asset_path: str) -> Path | None:
    normalized = asset_path.strip("/")
    candidate = (
        (build_dir / normalized).resolve()
        if normalized
        else (build_dir / "index.html").resolve()
    )
    try:
        candidate.relative_to(build_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _missing_web_build_response(build_dir: Path) -> HTMLResponse:
    return HTMLResponse(
        content=(
            "<!doctype html><html><head><title>KAI Web UI unavailable</title></head>"
            "<body><h1>Web UI build not found</h1>"
            f"<p>Expected built assets under <code>{build_dir}</code>.</p></body></html>"
        ),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _fetch_watchlist_quote(symbol: str) -> dict[str, Any]:
    from agent.data_sources.coinbase import fetch_latest_price

    return fetch_latest_price(symbol)


def _load_portfolio_snapshot() -> dict[str, Any]:
    from data_api.paper_trading import portfolio

    return {
        "positions": portfolio.get_positions(),
        "pnl": portfolio.get_pnl(),
    }


def _load_chart_history(
    symbol: str,
    interval: str,
    source: str,
    limit: int = 300,
) -> list[dict[str, Any]]:
    normalized_source = source.strip().lower() or "kai-api"
    if normalized_source == "kai-api":
        from agent.data_sources.kai_api import fetch_candles

        return fetch_candles(symbol.upper(), interval, limit)
    if normalized_source == "coinbase":
        from agent.data_sources.coinbase import fetch_candles

        return fetch_candles(symbol.upper(), interval, min(limit, 300))
    raise ValueError(f"unsupported chart source '{source}'")


def _endpoint_default_model(endpoint_name: str) -> str | None:
    """Return the configured default model for an endpoint."""
    endpoint = ENDPOINTS.get(endpoint_name) or {}
    models = list_endpoint_models(endpoint_name)
    if endpoint.get("default_model"):
        return str(endpoint["default_model"])
    return models[0] if models else None


def _normalize_endpoint_ref(ref: Any) -> tuple[str | None, str | None]:
    """Extract the endpoint and model names from an agent endpoint ref."""
    if isinstance(ref, str):
        if "/" in ref:
            endpoint_name, model_name = ref.split("/", 1)
            return endpoint_name, model_name
        return ref, None
    if isinstance(ref, dict):
        endpoint_name = ref.get("endpoint") or ref.get("name")
        model_name = ref.get("model")
        return (
            str(endpoint_name) if endpoint_name else None,
            str(model_name) if model_name else None,
        )
    return None, None


def _agent_model_summary(
    agent_name: str,
    agent_config: dict[str, Any],
) -> dict[str, Any]:
    """Return one agent's selected model state for the UI."""
    resolved = get_agent_config(agent_name)
    endpoint_cfg = resolved.get("endpoint") or {}
    endpoint_name, model_name = _normalize_endpoint_ref(agent_config.get("endpoint"))
    explicit_model = agent_config.get("model")
    if isinstance(explicit_model, str) and explicit_model:
        model_name = explicit_model
    model_name = model_name or endpoint_cfg.get("model") or (
        _endpoint_default_model(endpoint_name) if endpoint_name else None
    )

    fallback_refs = agent_config.get("fallback_endpoints")
    if not isinstance(fallback_refs, list):
        fallback = agent_config.get("fallback_endpoint")
        fallback_refs = [fallback] if fallback else []

    fallbacks: list[dict[str, Any]] = []
    resolved_fallbacks = resolved.get("fallback_endpoints") or []
    for index, fallback_ref in enumerate(fallback_refs):
        fallback_endpoint, fallback_model = _normalize_endpoint_ref(fallback_ref)
        resolved_fallback = (
            resolved_fallbacks[index]
            if index < len(resolved_fallbacks)
            else {}
        )
        fallbacks.append(
            {
                "endpoint": fallback_endpoint,
                "model": fallback_model or resolved_fallback.get("model"),
                "provider": resolved_fallback.get("provider"),
                "base_url": resolved_fallback.get("base_url"),
            }
        )

    return {
        "name": agent_name,
        "description": agent_config.get("description", ""),
        "endpoint": endpoint_name,
        "model": model_name,
        "provider": endpoint_cfg.get("provider"),
        "base_url": endpoint_cfg.get("base_url"),
        "reasoning_effort": endpoint_cfg.get("reasoning_effort"),
        "text_verbosity": endpoint_cfg.get("text_verbosity"),
        "max_iterations": resolved.get("max_iterations"),
        "fallbacks": fallbacks,
    }


def _serialized_message_length(message: dict[str, str]) -> int:
    """Return the content length for one serialized chat message."""
    return len(str(message.get("content", "")))


def _recent_serialized_messages(
    messages: list[dict[str, str]],
    *,
    max_messages: int = SNAPSHOT_CHAT_HISTORY_MAX_MESSAGES,
    max_chars: int = SNAPSHOT_CHAT_HISTORY_MAX_CHARS,
) -> list[dict[str, str]]:
    """Return a bounded recent chat-history slice for websocket attach.

    Args:
        messages: Serialized chat messages ordered oldest to newest.
        max_messages: Maximum number of messages to return.
        max_chars: Approximate content-character budget.

    Returns:
        Recent messages ordered oldest to newest.
    """
    selected: list[dict[str, str]] = []
    total_chars = 0
    for message in reversed(messages):
        message_chars = _serialized_message_length(message)
        if selected and total_chars + message_chars > max_chars:
            break
        selected.append(message)
        total_chars += message_chars
        if len(selected) >= max_messages:
            break
    selected.reverse()
    return selected


class DaemonOHLCVFetcher:
    """Optimizer fetcher backed by the daemon's existing chart-data loaders."""

    def __init__(self, source: str = "kai-api") -> None:
        self.source = source

    async def fetch(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        raw_bars = await asyncio.to_thread(
            _load_chart_history,
            symbol,
            timeframe,
            self.source,
            bars,
        )
        frame = pd.DataFrame(raw_bars)
        if frame.empty:
            raise ValueError(f"no OHLCV bars returned for {symbol} {timeframe}")
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        frame = frame.set_index("ts").sort_index()
        columns = ["open", "high", "low", "close", "volume"]
        missing = [column for column in columns if column not in frame.columns]
        if missing:
            raise ValueError(f"OHLCV bars are missing columns: {', '.join(missing)}")
        return frame[columns]


def _process_memory_bytes() -> int | None:
    """Return the current process RSS when the host exposes /proc."""
    statm_path = Path("/proc/self/statm")
    try:
        fields = statm_path.read_text(encoding="utf-8").split()
        resident_pages = int(fields[1])
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (IndexError, OSError, ValueError):
        return None
    return resident_pages * page_size


@dataclass
class ManagedSession:
    """Server-owned session plus per-session coordination state."""

    session: Session
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_input_task: asyncio.Task | None = None


@dataclass
class InputRunResult:
    """Outcome of running one input turn through a session."""

    final_text: str = ""
    error: str | None = None


class SessionCreateRequest(BaseModel):
    """Payload for REST session creation."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)


class ModelSwitchRequest(BaseModel):
    """Payload for changing an agent's primary model at runtime."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str | None = Field(default=None, min_length=1)


class ChartViewUpdateRequest(BaseModel):
    """Payload for updating a session chart view."""

    model_config = ConfigDict(extra="forbid")

    symbol: str | None = None
    timeframe: str | None = None
    source: str | None = None
    mode: str | None = None


class WatchlistUpdateRequest(BaseModel):
    """Payload for updating a session watchlist."""

    model_config = ConfigDict(extra="forbid")

    symbols: list[str] | None = None
    add: str | None = None
    remove: str | None = None


class DaemonServer:
    """FastAPI-facing daemon runtime that owns sessions and shared state.

    Args:
        agent_name: Default local agent name for daemon sessions.
        nats_url: NATS connection URL.
        bus_factory: Optional bus factory for tests or alternate runtime
            wiring.
        scheduler_factory: Optional scheduler factory for tests.
        token_path: Optional daemon bearer token path.
        allow_unauthenticated_local: Whether local daemon calls bypass
            bearer-token auth.
        db_path: Optional SQLite state database path.
        forgejo_webhook_secret_provider: Optional Forgejo HMAC secret
            provider. When omitted, startup attempts the default provider.

    Raises:
        No exceptions are expected during construction; startup performs
        I/O and can raise connection or migration errors.

    Example:
        Build a daemon runtime with a test database path::

            server = DaemonServer(db_path="/tmp/daemon-state.sqlite3")
    """

    def __init__(
        self,
        *,
        agent_name: str = DEFAULT_AGENT,
        nats_url: str = NATS_URL,
        bus_factory: Callable[[str, str], Any] | None = None,
        scheduler_factory: Callable[..., Scheduler] | None = None,
        token_path: str | Path | None = None,
        allow_unauthenticated_local: bool = True,
        db_path: str | Path | None = None,
        forgejo_webhook_secret_provider: WebhookSecretProvider | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.nats_url = nats_url
        self.bus_factory = bus_factory or self._default_bus_factory
        self.scheduler_factory = scheduler_factory or Scheduler
        self.token_path = Path(token_path) if token_path is not None else DAEMON_TOKEN_PATH
        self.allow_unauthenticated_local = allow_unauthenticated_local
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DAEMON_DB_PATH
        self.forgejo_webhook_secret_provider = forgejo_webhook_secret_provider
        self.bus: Any | None = None
        self.sessions: dict[str, ManagedSession] = {}
        self.event_bus = DaemonEventBus()
        self.signal_consumer = SignalConsumer()
        self.scheduler: Scheduler | None = None
        self.daemon_token = ""
        self.started_at_monotonic: float | None = None
        self.log = get_logger("daemon.server")

    @staticmethod
    def _default_bus_factory(url: str, agent_name: str) -> NatsBus:
        return NatsBus(url=url, agent_name=agent_name)

    async def startup(self) -> None:
        """Connect shared resources used by daemon-backed sessions."""
        self.daemon_token = ensure_daemon_token(self.token_path)
        self.started_at_monotonic = time.monotonic()
        try:
            apply_migrations(self.db_path)
        except Exception as exc:  # noqa: BLE001
            self.log.error("daemon migrations failed: %s", exc)
            raise
        if self.forgejo_webhook_secret_provider is None:
            try:
                self.forgejo_webhook_secret_provider = (
                    default_forgejo_webhook_secret_provider()
                )
            except WebhookSecretError as exc:
                self.log.warning(
                    "forgejo webhook secret provider not configured: %s", exc
                )
        self.signal_consumer.on_signal = self._handle_signal
        if self.bus_factory is None:
            self.bus = None
        else:
            try:
                bus = self.bus_factory(self.nats_url, self.agent_name)
                await bus.connect()
            except Exception as exc:  # noqa: BLE001
                self.log.warning("daemon bus connect failed: %s", exc)
                self.bus = None
            else:
                self.bus = bus
                with suppress(Exception):
                    self.bus.on_message(self._handle_nats_message)
                with suppress(Exception):
                    await self.signal_consumer.subscribe(bus)

        self.scheduler = self.scheduler_factory(
            dispatch_callback=self._handle_scheduled_job_trigger,
            event_bus=self.event_bus,
            event_callback=self._handle_scheduler_event,
        )
        await self.scheduler.start()

    async def shutdown(self) -> None:
        """Stop all managed runtime resources."""
        for managed in self.sessions.values():
            with suppress(Exception):
                await managed.session.sub_agent_registry.stop_all()

        if self.scheduler is not None:
            with suppress(Exception):
                await self.scheduler.shutdown()
            self.scheduler = None

        if self.bus is not None:
            with suppress(Exception):
                await self.bus.disconnect()
            self.bus = None

    def uptime_seconds(self) -> float:
        """Return daemon uptime in seconds since the current process booted."""
        if self.started_at_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at_monotonic)

    def metrics_snapshot(self) -> dict[str, Any]:
        """Return detailed daemon metrics for diagnostics and smoke tests."""
        queue_depth_by_session = {
            name: len(managed.session.input_queue)
            for name, managed in sorted(self.sessions.items())
        }
        indexed_session_names = {entry.name for entry in list_indexed_sessions()}
        visible_session_names = set(self.sessions) | indexed_session_names
        scheduler_jobs = self.scheduler.list_jobs() if self.scheduler is not None else []
        if visible_session_names:
            scheduler_jobs = [
                job
                for job in scheduler_jobs
                if job.owner_session in visible_session_names
            ]
        else:
            scheduler_jobs = []
        scheduler_status_counts = Counter(job.status for job in scheduler_jobs)
        return {
            "agent_name": self.agent_name,
            "uptime_seconds": round(self.uptime_seconds(), 3),
            "bus_connected": self.bus is not None,
            "process": {
                "pid": os.getpid(),
                "memory_rss_bytes": _process_memory_bytes(),
            },
            "sessions": {
                "live_count": len(self.sessions),
                "indexed_count": len(list_indexed_sessions()),
                "queue_depth": {
                    "total": sum(queue_depth_by_session.values()),
                    "per_session": queue_depth_by_session,
                },
                "activity": {
                    name: managed.session.activity_status
                    for name, managed in sorted(self.sessions.items())
                },
            },
            "scheduler": {
                "job_count": len(scheduler_jobs),
                "status_counts": dict(sorted(scheduler_status_counts.items())),
            },
        }

    def health_snapshot(self) -> dict[str, Any]:
        """Return a compact health payload for readiness checks."""
        metrics = self.metrics_snapshot()
        return {
            "status": "ok",
            "agent_name": self.agent_name,
            "bus_connected": metrics["bus_connected"],
            "session_count": metrics["sessions"]["live_count"],
            "uptime_seconds": metrics["uptime_seconds"],
            "memory_rss_bytes": metrics["process"]["memory_rss_bytes"],
            "agent_queue_depth": metrics["sessions"]["queue_depth"]["total"],
            "scheduler_job_count": metrics["scheduler"]["job_count"],
        }

    def _is_authorized(self, *, token: str | None, client_host: str | None) -> bool:
        if token and self._token_is_accepted(token):
            return True
        return self.allow_unauthenticated_local and is_local_client_host(client_host)

    def _token_is_accepted(self, token: str) -> bool:
        """Return whether a bearer token is accepted by daemon routes.

        Args:
            token: Candidate bearer token.

        Returns:
            True when the token matches the daemon token or configured
            taskboard/OpenClaw gateway token.
        """

        for accepted in (
            self.daemon_token,
            os.getenv("AGENT_GATEWAY_TOKEN", "").strip(),
            os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip(),
            os.getenv("OPENCLAW_TOKEN", "").strip(),
        ):
            if accepted and secrets.compare_digest(token, accepted):
                return True
        return False

    def require_http_auth(self, request: Request) -> None:
        """Reject unauthorized REST requests."""
        client_host = request.client.host if request.client is not None else None
        token = parse_bearer_token(request.headers.get("authorization"))
        if self._is_authorized(token=token, client_host=client_host):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="daemon bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    def authorize_websocket(self, websocket: WebSocket) -> bool:
        """Validate websocket auth before the attach handshake."""
        client_host = websocket.client.host if websocket.client is not None else None
        token = parse_bearer_token(websocket.headers.get("authorization"))
        if token is None:
            token = websocket.query_params.get("token")
        return self._is_authorized(token=token, client_host=client_host)

    async def get_or_create_session(
        self,
        name: str,
        *,
        create_if_missing: bool,
    ) -> ManagedSession:
        """Return a live session, hydrating it from disk on first access."""
        if name in self.sessions:
            return self.sessions[name]

        session = Session(name)
        state_exists = session.paths.state_path.exists()
        indexed_entry = get_indexed_session(name)
        if not create_if_missing and not (state_exists or indexed_entry):
            raise KeyError(f"session '{name}' does not exist")

        session.load()
        session.attach_runtime(
            bus=self.bus,
            agent_name=self.agent_name,
            signal_consumer=self.signal_consumer,
            scheduler=self.scheduler,
        )
        session.strategy_runtime = InProcessStrategyRuntime(
            session_name=session.name,
            agent_name=session.agent_name or self.agent_name,
            ohlcv_fetcher=DaemonOHLCVFetcher(),
            event_callback=session.publish_event,
        )
        session.touch_index()

        managed = ManagedSession(session=session)
        self.sessions[session.name] = managed
        return managed

    async def run_input(
        self,
        managed: ManagedSession,
        text: str,
        *,
        source: str = "user",
        job_id: str | None = None,
        tool_budget: int | None = None,
    ) -> InputRunResult:
        """Run one input turn through the target session."""
        result = InputRunResult()
        async with managed.input_lock:
            managed.current_input_task = asyncio.current_task()
            managed.session.set_activity_status("thinking...")
            try:
                async for event in managed.session.stream_agent_events(
                    text,
                    source=source,
                    job_id=job_id,
                    tool_budget=tool_budget,
                ):
                    if event.get("type") == "final":
                        result.final_text = str(event.get("data") or "")
                    elif event.get("type") == "error":
                        result.error = str(event.get("data") or "agent stream failed")
            except asyncio.CancelledError:
                result.error = "current LLM stream stopped"
                managed.session.publish_event("agent.error", {"value": result.error})
                if managed.session.auto_mode:
                    managed.session.stop_auto_mode("stopped by user")
            except Exception as exc:  # noqa: BLE001
                result.error = str(exc)
                managed.session.publish_event("agent.error", {"value": str(exc)})
            finally:
                if managed.current_input_task is asyncio.current_task():
                    managed.current_input_task = None
                managed.session.set_activity_status("idle")
                with suppress(Exception):
                    managed.session.save()
        return result

    async def stop_session_run(self, session_name: str) -> dict[str, Any]:
        """Cancel the current LLM stream for a live session.

        Args:
            session_name: Name of the session whose active turn should stop.

        Returns:
            A JSON-serializable summary of the stop request.
        """
        managed = self.sessions.get(Session(session_name).name)
        if managed is None:
            raise KeyError(f"session '{session_name}' is not live")

        cancelled = False
        if (
            managed.current_input_task is not None
            and not managed.current_input_task.done()
        ):
            managed.current_input_task.cancel()
            cancelled = True
            await asyncio.sleep(0)

        if managed.session.auto_mode:
            managed.session.stop_auto_mode("stopped by user")

        if managed.session.input_queue:
            managed.session.input_queue.clear()
            managed.session.publish_event("input.dequeued", {"depth": 0})

        managed.session.set_activity_status("idle")
        return {
            "session": managed.session.name,
            "stopped": cancelled,
            "activity_status": managed.session.activity_status,
            "queue_depth": len(managed.session.input_queue),
        }

    def model_registry(self) -> dict[str, Any]:
        """Return configured agents and endpoint models for UI selection."""
        endpoint_summaries = []
        for endpoint_name, endpoint_cfg in sorted(ENDPOINTS.items()):
            models = list_endpoint_models(endpoint_name)
            endpoint_summaries.append(
                {
                    "name": endpoint_name,
                    "provider": endpoint_cfg.get("provider", "openai"),
                    "base_url": endpoint_cfg.get("base_url", ""),
                    "default_model": _endpoint_default_model(endpoint_name),
                    "models": models,
                }
            )
        return {
            "agents": [
                _agent_model_summary(agent_name, agent_cfg)
                for agent_name, agent_cfg in sorted(AGENTS.items())
            ],
            "endpoints": endpoint_summaries,
        }

    def switch_agent_model(
        self,
        agent_name: str,
        *,
        endpoint: str,
        model: str,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Switch one configured agent to an endpoint/model pair."""
        if agent_name not in AGENTS:
            raise KeyError(f"unknown agent '{agent_name}'")
        if endpoint not in ENDPOINTS:
            raise ValueError(f"unknown endpoint '{endpoint}'")
        available_models = list_endpoint_models(endpoint)
        if model not in available_models:
            raise ValueError(
                f"model '{model}' is not configured for endpoint '{endpoint}'"
            )

        AGENTS[agent_name]["endpoint"] = endpoint
        AGENTS[agent_name]["model"] = model
        if reasoning_effort:
            set_agent_reasoning_effort(agent_name, reasoning_effort)

        reloaded_sessions: list[dict[str, Any]] = []
        for session_name, managed in sorted(self.sessions.items()):
            if managed.session.agent_name != agent_name:
                continue
            runner = managed.session.agent_runner
            reload_llm = getattr(runner, "reload_llm", None)
            if not callable(reload_llm):
                continue
            reload_result = reload_llm()
            reloaded_sessions.append(
                {
                    "session": session_name,
                    "model": reload_result.get("model"),
                    "provider": reload_result.get("provider"),
                    "reasoning_effort": reload_result.get("reasoning_effort"),
                    "fallback_count": reload_result.get("fallback_count", 0),
                }
            )

        selected = _agent_model_summary(agent_name, AGENTS[agent_name])
        self.log.info(
            "MODEL_SWITCH agent=%s endpoint=%s model=%s reasoning=%s "
            "reloaded_sessions=%d",
            agent_name,
            endpoint,
            model,
            reasoning_effort or AGENTS[agent_name].get("reasoning_effort"),
            len(reloaded_sessions),
        )
        return {
            "agent": selected,
            "reloaded_sessions": reloaded_sessions,
        }

    async def publish_daemon_event(self, channel: str, payload: dict[str, Any]) -> None:
        """Publish one daemon-scoped event to scheduler subscribers."""
        await self.event_bus.publish(channel, payload)

    async def _handle_scheduled_job_trigger(self, job, fired_at) -> None:
        """Dispatch one scheduled job into its owner session."""
        if self.scheduler is None:
            return
        self.scheduler.notify_triggered(job.id, fired_at=fired_at)

        try:
            managed = await self.get_or_create_session(
                job.owner_session,
                create_if_missing=False,
            )
        except Exception as exc:  # noqa: BLE001
            self.scheduler.record_failure(job.id, fired_at=fired_at, error=str(exc))
            self.log.warning("scheduled job %s failed to attach session: %s", job.id, exc)
            return

        if job.concurrency == "skip" and managed.input_lock.locked():
            self.log.info(
                "scheduled job %s skipped because session %s is busy",
                job.id,
                job.owner_session,
            )
            return

        outcome = await self.run_input(
            managed,
            job.prompt,
            source="scheduler",
            job_id=job.id,
            tool_budget=job.tool_budget,
        )
        if outcome.error:
            self.scheduler.record_failure(job.id, fired_at=fired_at, error=outcome.error)
            return
        self.scheduler.record_completion(
            job.id,
            fired_at=fired_at,
            result_preview=outcome.final_text,
        )

    def _handle_scheduler_event(self, event_type: str, *, job, **payload: Any) -> None:
        """Publish scheduler lifecycle events onto the owner session bus."""
        managed = self.sessions.get(job.owner_session)
        if managed is None:
            return

        topic_map = {
            "created": "scheduled_job.created",
            "triggered": "scheduled_job.triggered",
            "completed": "scheduled_job.completed",
            "failed": "scheduled_job.failed",
            "cancelled": "scheduled_job.cancelled",
            "paused": "scheduled_job.paused",
            "resumed": "scheduled_job.resumed",
        }
        topic = topic_map.get(event_type)
        if not topic:
            return

        event_payload: dict[str, Any]
        if event_type == "created":
            event_payload = {"job": job.model_dump(mode="json")}
        elif event_type == "triggered":
            event_payload = {"job_id": job.id, "fired_at": payload.get("fired_at")}
        elif event_type == "completed":
            event_payload = {
                "job_id": job.id,
                "result_preview": payload.get("result_preview"),
            }
        elif event_type == "failed":
            event_payload = {"job_id": job.id, "error": payload.get("error") or "job failed"}
        else:
            event_payload = {"job_id": job.id}

        managed.session.publish_event(topic, event_payload)

    async def handle_schedule_command(self, managed: ManagedSession, command_text: str) -> str:
        """Execute one scheduler slash command for the attached session."""
        if self.scheduler is None:
            raise RuntimeError("scheduler is unavailable")

        try:
            parts = shlex.split(command_text)
        except ValueError as exc:
            raise ValueError(f"invalid schedule command: {exc}") from exc

        if not parts or parts[0] != "/schedule":
            raise ValueError("unsupported schedule command")

        session_name = managed.session.name
        sub = parts[1].lower() if len(parts) > 1 else "list"

        if sub == "list":
            show_all = len(parts) > 2 and parts[2].lower() == "all"
            jobs = (
                self.scheduler.list_jobs()
                if show_all
                else self.scheduler.list_jobs_for_session(session_name)
            )
            visible = [job for job in jobs if job.status in {"active", "paused"}]
            if not visible:
                scope = "all sessions" if show_all else f"session {session_name}"
                return f"No scheduled jobs for {scope}."
            return "\n".join(self._format_scheduled_job(job) for job in visible)

        if sub == "show":
            if len(parts) < 3:
                raise ValueError("usage: /schedule show JOB_ID")
            job = self._require_session_job(session_name, parts[2])
            return self._format_scheduled_job(job, include_prompt=True)

        if sub == "add":
            if len(parts) < 5:
                raise ValueError("usage: /schedule add at|cron ...")
            mode = parts[2].lower()
            if mode == "at":
                when = _parse_schedule_at_value(parts[3])
                job = self.scheduler.create_absolute_job(
                    when=when,
                    prompt=parts[4],
                    owner_session=session_name,
                    created_by="user",
                )
                return f"Scheduled {job.id} at {job.next_run} for session {job.owner_session}."
            if mode == "cron":
                job = self.scheduler.create_recurring_job(
                    cron=parts[3],
                    prompt=parts[4],
                    owner_session=session_name,
                    created_by="user",
                )
                return f"Scheduled recurring job {job.id} next={job.next_run}."
            raise ValueError("usage: /schedule add at|cron ...")

        if sub == "cancel":
            if len(parts) < 3:
                raise ValueError("usage: /schedule cancel JOB_ID")
            job = self._require_session_job(session_name, parts[2])
            self.scheduler.cancel_job(job.id)
            return f"Cancelled scheduled job {job.id}."

        if sub == "pause":
            if len(parts) < 3:
                raise ValueError("usage: /schedule pause JOB_ID|all [--global]")
            target = parts[2].lower()
            if target == "all":
                global_scope = "--global" in parts[3:]
                paused = self.scheduler.pause_all_jobs(None if global_scope else session_name)
                scope = "all sessions" if global_scope else f"session {session_name}"
                if not paused:
                    return f"No active scheduled jobs to pause for {scope}."
                return f"Paused {len(paused)} scheduled jobs for {scope}."
            job = self._require_session_job(session_name, parts[2])
            self.scheduler.pause_job(job.id)
            return f"Paused scheduled job {job.id}."

        if sub == "resume":
            if len(parts) < 3:
                raise ValueError("usage: /schedule resume JOB_ID")
            job = self._require_session_job(session_name, parts[2])
            self.scheduler.resume_job(job.id)
            return f"Resumed scheduled job {job.id}."

        raise ValueError("unsupported schedule command")

    async def handle_auto_command(self, managed: ManagedSession, command_text: str) -> str:
        """Execute one autonomous-mode slash command for the attached session."""

        try:
            parts = shlex.split(command_text)
        except ValueError as exc:
            raise ValueError(f"invalid auto command: {exc}") from exc

        if not parts or parts[0] != "/auto":
            raise ValueError("unsupported auto command")

        session = managed.session
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub == "off":
            self.log.info(
                "SLASH_AUTO action=%s budget=%d session=%s",
                "off",
                int(session.auto_iterations_remaining or 0),
                session.name,
            )
            if not session.auto_mode:
                return "Auto mode is already off."
            payload = session.stop_auto_mode("stopped by user")
            return (
                "Auto mode stopped. "
                f"Used {payload['iterations_used']}/{payload['iterations_total']} iterations "
                f"in {payload['elapsed_seconds']:.2f}s."
            )

        if sub == "status":
            self.log.info(
                "SLASH_AUTO action=%s budget=%d session=%s",
                "status",
                int(session.auto_iterations_remaining or 0),
                session.name,
            )
            if not session.auto_mode:
                return "Auto mode is off."
            payload = session.auto_status_payload()
            mode = "readonly" if payload["readonly"] else "standard"
            return (
                f"Auto mode {mode}: "
                f"{payload['iterations_used']}/{payload['iterations_total']} iterations used, "
                f"{payload['iterations_remaining']} remaining, "
                f"{payload['elapsed_seconds']:.2f}s elapsed."
            )

        readonly = False
        max_iterations = DEFAULT_AUTO_MAX_ITERATIONS
        args = [
            part.lower() if index == 1 else part
            for index, part in enumerate(parts[1:], start=1)
        ]
        if args:
            if args[0] == "readonly":
                readonly = True
                if len(args) > 1:
                    try:
                        max_iterations = int(args[1])
                    except ValueError as exc:
                        raise ValueError("usage: /auto [N]|off|status|readonly [N]") from exc
            else:
                try:
                    max_iterations = int(parts[1])
                except ValueError as exc:
                    raise ValueError("usage: /auto [N]|off|status|readonly [N]") from exc

        if max_iterations < 1 or max_iterations > MAX_AUTO_ITERATIONS:
            raise ValueError(f"auto iterations must be between 1 and {MAX_AUTO_ITERATIONS}")

        self.log.info(
            "SLASH_AUTO action=%s budget=%d session=%s",
            "readonly" if readonly else "start",
            max_iterations,
            session.name,
        )
        payload = session.start_auto_mode(max_iterations=max_iterations, readonly=readonly)
        mode = "readonly" if payload["readonly"] else "standard"
        return (
            f"Auto mode enabled ({mode}). "
            f"Budget: {payload['iterations_total']} iterations, "
            f"wall-clock cap: {session.auto_max_duration:.0f}s."
        )

    async def handle_optimizer_command(self, managed: ManagedSession, command_text: str) -> str:
        """Execute one optimizer slash command for the attached session."""
        try:
            parts = shlex.split(command_text)
        except ValueError as exc:
            raise ValueError(f"invalid optimizer command: {exc}") from exc

        if not parts or parts[0] != "/optimizer":
            raise ValueError("unsupported optimizer command")

        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub == "status":
            return render_strategy_command_result(optimizer_status(managed.session))
        if sub == "start":
            max_cycles = int(parts[2]) if len(parts) > 2 else 10
            return render_strategy_command_result(
                optimizer_start(managed.session, max_cycles=max_cycles)
            )
        if sub == "pause":
            return render_strategy_command_result(optimizer_pause(managed.session))
        if sub == "report":
            return render_strategy_command_result(
                optimizer_report(managed.session, limit=5)
            )
        raise ValueError("usage: /optimizer status|start [N]|pause|report")

    async def handle_strategies_command(self, managed: ManagedSession, command_text: str) -> str:
        """Execute one strategy-management slash command for the attached session."""
        try:
            parts = shlex.split(command_text)
        except ValueError as exc:
            raise ValueError(f"invalid strategies command: {exc}") from exc

        if not parts or parts[0] != "/strategies":
            raise ValueError("unsupported strategies command")

        sub = parts[1].lower() if len(parts) > 1 else "list"
        session = managed.session

        if sub == "list":
            pool = parts[2].lower() if len(parts) > 2 else "all"
            return render_strategy_command_result(
                list_strategy_records(session, pool=pool)
            )

        if sub == "show":
            if len(parts) < 3:
                raise ValueError("usage: /strategies show NAME [VERSION]")
            version = int(parts[3]) if len(parts) > 3 else None
            return render_strategy_command_result(
                show_strategy(session, name=parts[2], version=version)
            )

        if sub == "propose":
            yaml_str = _extract_command_remainder(command_text, "/strategies", "propose")
            if not yaml_str:
                raise ValueError("usage: /strategies propose YAML_OR_PATH")
            # Only try as a file path if it's short enough to be a path
            # and doesn't look like YAML content (no colons or newlines)
            if len(yaml_str) < 260 and "\n" not in yaml_str and ":" not in yaml_str:
                candidate_path = Path(yaml_str).expanduser()
                if candidate_path.is_file():
                    yaml_str = candidate_path.read_text(encoding="utf-8")
            return render_strategy_command_result(
                propose_strategy(session, yaml_str=yaml_str)
            )

        if sub == "promote":
            if len(parts) < 3:
                raise ValueError("usage: /strategies promote NAME")
            return render_strategy_command_result(
                move_strategy(session, name=parts[2], to_pool="active")
            )

        if sub == "demote":
            if len(parts) < 3:
                raise ValueError("usage: /strategies demote NAME")
            return render_strategy_command_result(
                move_strategy(session, name=parts[2], to_pool="candidates")
            )

        if sub == "retire":
            if len(parts) < 3:
                raise ValueError("usage: /strategies retire NAME")
            return render_strategy_command_result(
                move_strategy(session, name=parts[2], to_pool="graveyard")
            )

        if sub == "lineage":
            if len(parts) < 3:
                raise ValueError("usage: /strategies lineage NAME")
            return render_strategy_command_result(get_strategy_lineage(session, name=parts[2]))

        raise ValueError(
            "usage: /strategies list [pool]|show NAME [VERSION]|propose YAML_OR_PATH|"
            "promote NAME|demote NAME|retire NAME|lineage NAME"
        )

    def _require_session_job(self, session_name: str, job_id: str):
        if self.scheduler is None:
            raise RuntimeError("scheduler is unavailable")
        job = self.scheduler.get_job(job_id)
        if job is None:
            raise ValueError(f"scheduled job '{job_id}' was not found")
        if job.owner_session != session_name:
            raise ValueError(f"scheduled job '{job_id}' belongs to session '{job.owner_session}'")
        return job

    @staticmethod
    def _format_scheduled_job(job, *, include_prompt: bool = False) -> str:
        next_run = job.next_run or ("event-driven" if job.type == "event" else "n/a")
        text = (
            f"{job.id} [{job.status}] session={job.owner_session} "
            f"type={job.type} next={next_run}"
        )
        if include_prompt:
            text = f"{text} prompt={job.prompt}"
        return text

    def _handle_signal(self, signal: Signal) -> None:
        """Fan out shared signal events to live sessions and the daemon bus."""
        payload = signal.to_dict()
        for managed in self.sessions.values():
            managed.session.publish_event("signal.received", {"signal": payload})
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.publish_daemon_event("signals", payload))

    def _handle_nats_message(self, direction: str, subject: str, payload: dict[str, Any]) -> None:
        """Mirror shared NATS traffic into each live session bus."""
        event_payload = {
            "direction": direction,
            "subject": subject,
            "payload": payload,
        }
        for managed in self.sessions.values():
            managed.session.publish_event("nats.message", event_payload)

    def describe_session(self, name: str) -> dict[str, Any]:
        """Return one session summary suitable for REST and slash listings."""
        entry = get_indexed_session(name)
        if entry is None:
            raise KeyError(f"session '{name}' does not exist")
        managed = self.sessions.get(entry.name)
        activity_status = managed.session.activity_status if managed else "idle"
        queued_inputs = len(managed.session.input_queue) if managed else 0
        return {
            "name": entry.name,
            "created_at": entry.created_at,
            "last_activity": entry.last_activity,
            "state_path": entry.state_path,
            "activity_status": activity_status,
            "queued_inputs": queued_inputs,
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return the persisted session index merged with live runtime state."""
        return [self.describe_session(entry.name) for entry in list_indexed_sessions()]

    async def create_session(self, name: str) -> dict[str, Any]:
        """Create a new named session and persist its initial state."""
        normalized = Session(name).name
        if (
            normalized in self.sessions
            or get_indexed_session(normalized) is not None
            or Session(normalized).paths.state_path.exists()
        ):
            raise FileExistsError(f"session '{normalized}' already exists")

        managed = await self.get_or_create_session(
            normalized,
            create_if_missing=True,
        )
        managed.session.save()
        return self.describe_session(normalized)

    async def delete_session(self, name: str) -> dict[str, Any]:
        """Delete a named session from memory, disk, and the session index."""
        session = Session(name)
        normalized = session.name
        state_exists = session.paths.state_path.exists()
        indexed_entry = get_indexed_session(normalized)
        managed = self.sessions.pop(normalized, None)
        if managed is None and not state_exists and indexed_entry is None:
            raise KeyError(f"session '{normalized}' does not exist")

        if managed is not None:
            with suppress(Exception):
                await managed.session.sub_agent_registry.stop_all()

        for path in (
            session.paths.state_path,
            session.paths.state_path.with_suffix(session.paths.state_path.suffix + ".lock"),
        ):
            with suppress(FileNotFoundError):
                path.unlink()

        if session.paths.root_dir.exists():
            shutil.rmtree(session.paths.root_dir, ignore_errors=True)

        remove_indexed_session(normalized)
        return {"deleted": True, "name": normalized}

    async def get_session_chart_view(self, name: str) -> dict[str, Any]:
        """Return the current chart view for a persisted session."""
        managed = await self.get_or_create_session(name, create_if_missing=False)
        return {
            "session": managed.session.name,
            "chart": managed.session.chart_view_payload(),
        }

    async def update_session_chart_view(
        self,
        name: str,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        source: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Update a session chart view and persist it.

        Args:
            name: Session name.
            symbol: Optional chart symbol.
            timeframe: Optional chart timeframe.
            source: Optional chart source.
            mode: Optional chart layout mode.

        Returns:
            JSON-ready payload with the updated chart view.
        """
        managed = await self.get_or_create_session(name, create_if_missing=False)
        chart = managed.session.set_chart_view(
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            mode=mode,
        )
        managed.session.save()
        return {"session": managed.session.name, "chart": chart}

    async def get_session_watchlist(self, name: str) -> dict[str, Any]:
        """Return the current watchlist for a persisted session."""
        managed = await self.get_or_create_session(name, create_if_missing=False)
        return {
            "session": managed.session.name,
            "watchlist": managed.session.watchlist_payload(),
        }

    async def update_session_watchlist(
        self,
        name: str,
        *,
        symbols: list[str] | None = None,
        add: str | None = None,
        remove: str | None = None,
    ) -> dict[str, Any]:
        """Update a session watchlist and persist it."""
        managed = await self.get_or_create_session(name, create_if_missing=False)
        if symbols is not None:
            watchlist = managed.session.set_watchlist_symbols(symbols)
        elif add is not None:
            watchlist = managed.session.add_watchlist_symbol(add)
        elif remove is not None:
            watchlist = managed.session.remove_watchlist_symbol(remove)
        else:
            watchlist = managed.session.watchlist_payload()
        managed.session.save()
        return {"session": managed.session.name, "watchlist": watchlist}

    async def forward_session_events(
        self,
        websocket: WebSocket,
        session: Session,
        event_queue: asyncio.Queue[SessionEvent],
        subscriptions: dict[str, Any],
    ) -> None:
        """Translate session-bus events into daemon wire messages."""
        tool_start_times: dict[str, float] = {}
        token_buffer: list[str] = []
        token_buffer_started = 0.0

        async def flush_tokens() -> None:
            """Send buffered LLM text to the websocket."""
            nonlocal token_buffer, token_buffer_started
            if not token_buffer:
                return
            text = "".join(token_buffer)
            token_buffer = []
            token_buffer_started = 0.0
            await _send_server_envelope(
                websocket,
                TokenEnvelope(type="token", text=text),
            )

        while True:
            if token_buffer:
                try:
                    event = await asyncio.wait_for(
                        event_queue.get(),
                        timeout=TOKEN_FLUSH_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    await flush_tokens()
                    continue
            else:
                event = await event_queue.get()
            message = self._event_to_message(
                session=session,
                event=event,
                subscriptions=subscriptions,
                tool_start_times=tool_start_times,
            )
            if message is None:
                continue
            if isinstance(message, TokenEnvelope):
                if not token_buffer:
                    token_buffer_started = time.monotonic()
                token_buffer.append(message.text)
                buffered_chars = sum(len(item) for item in token_buffer)
                should_flush = (
                    buffered_chars >= TOKEN_FLUSH_CHARS
                    or time.monotonic() - token_buffer_started
                    >= TOKEN_FLUSH_INTERVAL_SECONDS
                )
                if should_flush:
                    await flush_tokens()
                continue
            await flush_tokens()
            await _send_server_envelope(websocket, message)

    def _event_to_message(
        self,
        *,
        session: Session,
        event: SessionEvent,
        subscriptions: dict[str, Any],
        tool_start_times: dict[str, float],
    ):
        """Map one internal session event to a WS envelope."""
        topic = event.topic
        payload = event.payload

        if topic == "agent.token":
            text = payload.get("value")
            return TokenEnvelope(type="token", text=text or "")

        if topic == "agent.tool_start":
            tool = str(payload.get("tool") or "")
            if tool:
                tool_start_times[tool] = time.monotonic()
            return ToolStartEnvelope(
                type="tool_start",
                tool=tool,
                args=payload.get("input"),
            )

        if topic == "agent.tool_end":
            tool = str(payload.get("tool") or "")
            started_at = tool_start_times.pop(tool, None)
            elapsed_ms = None
            if started_at is not None:
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return ToolEndEnvelope(
                type="tool_end",
                tool=tool,
                elapsed_ms=elapsed_ms,
                ok=True,
            )

        if topic == "agent.final":
            return FinalEnvelope(type="final", text=payload.get("value") or "")

        if topic == "agent.status":
            return StatusEnvelope(
                type="status",
                activity=payload.get("value") or "idle",
                queue=len(session.input_queue),
            )

        if topic == "agent.error":
            return ErrorEnvelope(
                type="error",
                code="agent_error",
                message=payload.get("value") or "agent stream failed",
            )

        if topic == "status.updated":
            return StatusEnvelope(
                type="status",
                activity=payload.get("status") or "idle",
                queue=len(session.input_queue),
            )

        if topic == "auto.started":
            return AutoStartedEnvelope(
                type="auto_started",
                readonly=bool(payload.get("readonly")),
                iterations_total=int(payload.get("iterations_total") or 0),
                iterations_remaining=int(payload.get("iterations_remaining") or 0),
                iterations_used=int(payload.get("iterations_used") or 0),
                elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
            )

        if topic == "auto.progress":
            return AutoProgressEnvelope(
                type="auto_progress",
                readonly=bool(payload.get("readonly")),
                iterations_total=int(payload.get("iterations_total") or 0),
                iterations_remaining=int(payload.get("iterations_remaining") or 0),
                iterations_used=int(payload.get("iterations_used") or 0),
                elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
            )

        if topic == "auto.stopped":
            return AutoStoppedEnvelope(
                type="auto_stopped",
                readonly=bool(payload.get("readonly")),
                iterations_total=int(payload.get("iterations_total") or 0),
                iterations_remaining=int(payload.get("iterations_remaining") or 0),
                iterations_used=int(payload.get("iterations_used") or 0),
                elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
                reason=str(payload.get("reason") or ""),
            )

        if topic == "input.queued" or topic == "input.dequeued":
            return StatusEnvelope(
                type="status",
                activity=session.activity_status,
                queue=payload.get("depth", len(session.input_queue)),
            )

        if topic == "signal.received":
            if not subscriptions.get("signals"):
                return None
            signal = payload.get("signal")
            return SignalEnvelope(type="signal", signal=signal or payload)

        if topic == "chart.bar":
            chart_subs = subscriptions.get("chart", set())
            symbol = payload.get("symbol")
            timeframe = payload.get("tf")
            if chart_subs and (symbol, timeframe) not in chart_subs:
                return None
            if not chart_subs:
                return None
            return ChartBarEnvelope(
                type="chart_bar",
                symbol=symbol,
                tf=timeframe,
                bar=payload.get("bar"),
            )

        if topic == "ui_state.updated":
            return ChartViewEnvelope(
                type="chart_view",
                chart_symbol=str(
                    payload.get("chart_symbol") or session.ui_state.chart_symbol
                ),
                chart_timeframe=str(
                    payload.get("chart_timeframe") or session.ui_state.chart_timeframe
                ),
                chart_source=str(
                    payload.get("chart_source") or session.ui_state.chart_source
                ),
                chart_layout_mode=str(
                    payload.get("chart_layout_mode")
                    or session.ui_state.chart_layout_mode
                ),
            )

        if topic == "watchlist.updated":
            return WatchlistEnvelope(
                type="watchlist",
                watchlist_symbols=list(
                    payload.get("watchlist_symbols")
                    or session.ui_state.watchlist_symbols
                ),
            )

        if topic == "nats.message":
            if not subscriptions.get("nats"):
                return None
            direction = str(payload.get("direction") or "")
            subject = str(payload.get("subject") or "")
            if not direction or not subject:
                return None
            return NatsEventEnvelope(
                type="nats_event",
                direction=direction,
                subject=subject,
                payload=payload.get("payload"),
            )

        if topic == "scheduled_job.created":
            return ScheduledJobCreatedEnvelope(
                type="scheduled_job_created",
                job=payload.get("job"),
            )

        if topic == "scheduled_job.triggered":
            return ScheduledJobTriggeredEnvelope(
                type="scheduled_job_triggered",
                job_id=str(payload.get("job_id") or ""),
                fired_at=str(payload.get("fired_at") or ""),
            )

        if topic == "scheduled_job.completed":
            return ScheduledJobCompletedEnvelope(
                type="scheduled_job_completed",
                job_id=str(payload.get("job_id") or ""),
                result_preview=payload.get("result_preview"),
            )

        if topic == "scheduled_job.failed":
            return ScheduledJobFailedEnvelope(
                type="scheduled_job_failed",
                job_id=str(payload.get("job_id") or ""),
                error=str(payload.get("error") or "job failed"),
            )

        if topic == "scheduled_job.cancelled":
            return ScheduledJobCancelledEnvelope(
                type="scheduled_job_cancelled",
                job_id=str(payload.get("job_id") or ""),
            )

        if topic == "scheduled_job.paused":
            return ScheduledJobPausedEnvelope(
                type="scheduled_job_paused",
                job_id=str(payload.get("job_id") or ""),
            )

        if topic == "scheduled_job.resumed":
            return ScheduledJobResumedEnvelope(
                type="scheduled_job_resumed",
                job_id=str(payload.get("job_id") or ""),
            )

        if topic == "optimizer.completed":
            return OptimizerCompletedEnvelope(
                type="optimizer_completed",
                session=str(payload.get("session") or session.name),
                cycle_count=int(payload.get("cycle_count") or 0),
                cancelled=bool(payload.get("cancelled")),
                error=payload.get("error"),
                last_cycle_result=payload.get("last_cycle_result"),
            )

        return None

    @staticmethod
    def session_snapshot(session: Session) -> SessionStateSnapshot:
        """Serialize the attach-time state snapshot for one session."""
        serialized_history = serialize_messages(session.chat_history)
        recent_history = _recent_serialized_messages(serialized_history)
        return SessionStateSnapshot(
            chart_symbol=session.ui_state.chart_symbol,
            chart_timeframe=session.ui_state.chart_timeframe,
            chart_source=session.ui_state.chart_source,
            chart_layout_mode=session.ui_state.chart_layout_mode,
            chart_color_scheme=session.ui_state.chart_color_scheme,
            watchlist_symbols=list(session.ui_state.watchlist_symbols),
            autotrade_enabled=bool(session.ui_state.autotrade_enabled),
            activity_status=session.ui_state.activity_status,
            auto_mode=bool(session.auto_mode),
            auto_readonly=bool(session.auto_readonly),
            auto_iterations_total=int(session.auto_iterations_total),
            auto_iterations_remaining=int(session.auto_iterations_remaining),
            auto_elapsed_seconds=round(session.auto_elapsed_seconds(), 3),
            chat_history=recent_history,
            chat_history_total=len(serialized_history),
            chat_history_omitted=max(0, len(serialized_history) - len(recent_history)),
        )


async def _receive_client_envelope(websocket: WebSocket) -> ClientEnvelope:
    return decode_client_envelope(await websocket.receive_text())


async def _send_server_envelope(websocket: WebSocket, envelope) -> None:
    await websocket.send_json(encode_envelope(envelope))


def _extract_command_remainder(command_text: str, root: str, subcommand: str) -> str:
    prefix = f"{root} {subcommand}"
    stripped = command_text.strip()
    if not stripped.startswith(prefix):
        return ""
    remainder = stripped[len(prefix) :].strip()
    if len(remainder) >= 2 and remainder[0] == remainder[-1] and remainder[0] in {'"', "'"}:
        return remainder[1:-1]
    return remainder


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await _send_server_envelope(
        websocket,
        ErrorEnvelope(
            type="error",
            code=code,
            message=message,
        ),
    )


def _decode_forgejo_payload(body_bytes: bytes) -> tuple[str, dict[str, Any]]:
    import json

    try:
        payload_text = body_bytes.decode("utf-8")
        payload = json.loads(payload_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid JSON body: {exc}",
        ) from None
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid JSON body: payload must be an object",
        )
    return payload_text, payload


def _required_payload_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"missing required body field: {field_name}",
        )
    return value.strip()


def _extract_forgejo_pull_request_fields(
    payload: dict[str, Any],
) -> tuple[str, str, int, str]:
    action = _required_payload_string(payload.get("action"), "action")
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="missing required body field: repository",
        )
    repo = _required_payload_string(repository.get("full_name"), "repository.full_name")

    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="missing required body field: pull_request",
        )
    raw_number = pull_request.get("number") or payload.get("number")
    try:
        pr_number = int(raw_number)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="missing required body field: pull_request.number",
        ) from exc
    if pr_number <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pull_request.number must be positive",
        )

    head = pull_request.get("head")
    if not isinstance(head, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="missing required body field: pull_request.head",
        )
    head_sha = _required_payload_string(head.get("sha"), "pull_request.head.sha")
    return action, repo, pr_number, head_sha


def _handle_forgejo_webhook(
    daemon_server: "DaemonServer",
    request: Request,
    body_bytes: bytes,
) -> dict[str, Any]:
    """Validate, de-duplicate, and queue one Forgejo webhook delivery.

    The handler is split out from the FastAPI route so route code remains
    compact and tests can exercise the same persistence path without a
    dispatcher worker. Phase 1 only persists accepted deliveries.

    Args:
        daemon_server: Runtime object that owns the database path and
            Forgejo webhook secret provider.
        request: FastAPI request used for header access.
        body_bytes: Raw request body bytes used for HMAC verification.

    Returns:
        Response payload for a newly accepted delivery.

    Raises:
        HTTPException: 401 for invalid HMAC, 409 for replay, 422 for
            malformed headers/body/unsupported event, and 503 when the
            HMAC secret is unavailable.
    """

    import hashlib
    import sqlite3 as _sqlite3

    try:
        verified = parse_forgejo_headers(
            event_header=request.headers.get(FORGEJO_HEADER_EVENT),
            gitea_event_header=request.headers.get(FORGEJO_HEADER_GITEA_EVENT),
            delivery_header=request.headers.get(FORGEJO_HEADER_DELIVERY),
            signature_header=request.headers.get(FORGEJO_HEADER_SIGNATURE),
        )
    except ForgejoWebhookHeaderError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from None

    provider = daemon_server.forgejo_webhook_secret_provider
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="forgejo webhook secret is not configured",
        )

    try:
        secret = provider.get_secret()
    except WebhookSecretError as exc:
        daemon_server.log.error("forgejo webhook secret unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="forgejo webhook secret is not available",
        ) from None

    try:
        verify_forgejo_signature(secret=secret, body=body_bytes, headers=verified)
    except ForgejoWebhookSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from None

    if verified.event_type not in FORGEJO_SUPPORTED_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unsupported forgejo event: {verified.event_type}",
        )

    payload_text, payload = _decode_forgejo_payload(body_bytes)
    action, repo, pr_number, head_sha = _extract_forgejo_pull_request_fields(payload)

    received_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    expires_at_dt = received_at_dt + timedelta(hours=24)
    received_at = received_at_dt.isoformat()
    expires_at = expires_at_dt.isoformat()

    from daemon.db import connect as _db_connect

    conn = _db_connect(daemon_server.db_path)
    try:
        conn.execute(
            "DELETE FROM forgejo_deliveries WHERE expires_at <= ?",
            (received_at,),
        )
        existing = conn.execute(
            "SELECT delivery_id FROM forgejo_deliveries WHERE delivery_id = ?",
            (verified.delivery_id,),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE forgejo_deliveries"
                " SET duplicate_count = duplicate_count + 1"
                " WHERE delivery_id = ?",
                (verified.delivery_id,),
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="delivery_id already received within replay window",
                headers={"X-Forgejo-Delivery-Status": "duplicate"},
            )

        try:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO forgejo_deliveries ("
                "delivery_id, event_type, action, repo, pr_number, head_sha,"
                " received_at, expires_at, signature_sha256, payload_json,"
                " hmac_status, dispatch_status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verified.delivery_id,
                    verified.event_type,
                    action,
                    repo,
                    pr_number,
                    head_sha,
                    received_at,
                    expires_at,
                    verified.signature_hex,
                    payload_text,
                    "verified",
                    "pending",
                ),
            )
            conn.execute(
                "INSERT INTO forgejo_pending ("
                "delivery_id, event_type, action, repo, pr_number, head_sha,"
                " received_at, payload_json, status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verified.delivery_id,
                    verified.event_type,
                    action,
                    repo,
                    pr_number,
                    head_sha,
                    received_at,
                    payload_text,
                    "pending",
                ),
            )
            conn.execute("COMMIT")
        except _sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"duplicate forgejo delivery: {exc}",
                headers={"X-Forgejo-Delivery-Status": "duplicate"},
            ) from None
    finally:
        conn.close()

    body_hash = hashlib.sha256(body_bytes).hexdigest()
    daemon_server.log.info(
        "forgejo webhook accepted: delivery_id=%s event=%s repo=%s pr=%s body_sha256=%s",
        verified.delivery_id,
        verified.event_type,
        repo,
        pr_number,
        body_hash,
    )
    return {
        "status": "accepted",
        "delivery_id": verified.delivery_id,
        "event_type": verified.event_type,
    }


def create_app(
    *,
    agent_name: str = DEFAULT_AGENT,
    nats_url: str = NATS_URL,
    bus_factory: Callable[[str, str], Any] | None = None,
    scheduler_factory: Callable[..., Scheduler] | None = None,
    web_build_dir: str | Path | None = None,
    token_path: str | Path | None = None,
    allow_unauthenticated_local: bool = True,
    include_taskboard_gateway: bool = True,
    db_path: str | Path | None = None,
    forgejo_webhook_secret_provider: WebhookSecretProvider | None = None,
) -> FastAPI:
    """Build the FastAPI app that exposes the daemon WebSocket server.

    Args:
        agent_name: Default local agent name for daemon sessions.
        nats_url: NATS connection URL.
        bus_factory: Optional bus factory for tests.
        scheduler_factory: Optional scheduler factory for tests.
        web_build_dir: Optional static web build directory.
        token_path: Optional daemon bearer token path.
        allow_unauthenticated_local: Whether local daemon calls bypass auth.
        include_taskboard_gateway: Whether to register taskboard gateway routes.
        db_path: Optional override for the daemon SQLite state database.
        forgejo_webhook_secret_provider: Optional provider that resolves
            the shared HMAC secret for Forgejo webhook ingress.

    Returns:
        Configured FastAPI application.

    Raises:
        No exceptions are expected during construction; application
        startup can raise if required runtime initialization fails.

    Example:
        Build an app with an injected Forgejo webhook secret provider::

            app = create_app(
                forgejo_webhook_secret_provider=provider,
                db_path="/tmp/daemon-state.sqlite3",
            )
    """
    daemon_server = DaemonServer(
        agent_name=agent_name,
        nats_url=nats_url,
        bus_factory=bus_factory,
        scheduler_factory=scheduler_factory,
        token_path=token_path,
        allow_unauthenticated_local=allow_unauthenticated_local,
        db_path=db_path,
        forgejo_webhook_secret_provider=forgejo_webhook_secret_provider,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.daemon_server = daemon_server
        await daemon_server.startup()
        try:
            yield
        finally:
            await daemon_server.shutdown()

    app = FastAPI(lifespan=lifespan)
    build_dir = Path(web_build_dir) if web_build_dir is not None else DEFAULT_WEB_BUILD_DIR

    if include_taskboard_gateway:
        taskboard_app = create_taskboard_gateway_app()
        app.router.routes.extend(taskboard_app.router.routes)

    @app.get("/api/health")
    async def health_endpoint(request: Request) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        return daemon_server.health_snapshot()

    @app.post("/api/webhooks/forgejo")
    async def forgejo_webhook_endpoint(request: Request) -> dict[str, Any]:
        """Receive a signed Forgejo webhook delivery.

        This route deliberately bypasses bearer-token authorization.
        Authentication is performed by HMAC-SHA256 verification of the
        raw request body against the Vault-managed
        ``kai/forgejo-webhook-secret``. Phase 1 only validates,
        de-duplicates, and queues the delivery in SQLite.

        Returns:
            ``{"status": "accepted", "delivery_id": "<uuid>"}`` on a
            fresh, well-formed delivery.

        Raises:
            HTTPException: 401 on bad HMAC, 409 on replay within the
                delivery window, 422 on malformed input or unsupported
                events, and 503 when no HMAC secret is available.
        """

        body_bytes = await request.body()
        return _handle_forgejo_webhook(daemon_server, request, body_bytes)

    @app.get("/api/metrics")
    async def metrics_endpoint(request: Request) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        return daemon_server.metrics_snapshot()

    @app.get("/api/sessions")
    async def list_sessions_endpoint(request: Request) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        return {"sessions": daemon_server.list_sessions()}

    @app.get("/api/models")
    async def model_registry_endpoint(request: Request) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        return daemon_server.model_registry()

    @app.post("/api/models/{agent_name}")
    async def switch_model_endpoint(
        request: Request,
        agent_name: str,
        payload: ModelSwitchRequest,
    ) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            return daemon_server.switch_agent_model(
                agent_name,
                endpoint=payload.endpoint,
                model=payload.model,
                reasoning_effort=payload.reasoning_effort,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    @app.get("/api/market/watchlist")
    async def watchlist_endpoint(request: Request, symbols: str = "") -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        requested = []
        seen: set[str] = set()
        for raw_symbol in symbols.split(","):
            symbol = raw_symbol.strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            requested.append(symbol)

        quotes: list[dict[str, Any]] = []
        for symbol in requested:
            try:
                quote = await asyncio.to_thread(_fetch_watchlist_quote, symbol)
            except Exception as exc:  # noqa: BLE001
                quote = {"symbol": symbol, "error": str(exc)}
            quotes.append(quote)
        return {"quotes": quotes}

    @app.get("/api/market/ohlcv")
    async def market_ohlcv_endpoint(
        request: Request,
        symbol: str,
        interval: str = "1m",
        source: str = "kai-api",
        limit: int = 300,
    ) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            bars = await asyncio.to_thread(
                _load_chart_history,
                symbol,
                interval,
                source,
                limit,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        return {"bars": bars}

    @app.get("/api/portfolio")
    async def portfolio_endpoint(request: Request) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            snapshot = await asyncio.to_thread(_load_portfolio_snapshot)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        return snapshot

    @app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session_endpoint(
        request: Request,
        payload: SessionCreateRequest,
    ) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            session_info = await daemon_server.create_session(payload.name)
        except FileExistsError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        return {"session": session_info}

    @app.delete("/api/sessions/{session_name}")
    async def delete_session_endpoint(request: Request, session_name: str) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            return await daemon_server.delete_session(session_name)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    @app.get("/api/sessions/{session_name}/ui/chart")
    async def get_session_chart_endpoint(
        request: Request,
        session_name: str,
    ) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            return await daemon_server.get_session_chart_view(session_name)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    @app.patch("/api/sessions/{session_name}/ui/chart")
    async def update_session_chart_endpoint(
        request: Request,
        session_name: str,
        payload: ChartViewUpdateRequest,
    ) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            return await daemon_server.update_session_chart_view(
                session_name,
                symbol=payload.symbol,
                timeframe=payload.timeframe,
                source=payload.source,
                mode=payload.mode,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    @app.get("/api/sessions/{session_name}/ui/watchlist")
    async def get_session_watchlist_endpoint(
        request: Request,
        session_name: str,
    ) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            return await daemon_server.get_session_watchlist(session_name)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    @app.patch("/api/sessions/{session_name}/ui/watchlist")
    async def update_session_watchlist_endpoint(
        request: Request,
        session_name: str,
        payload: WatchlistUpdateRequest,
    ) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            return await daemon_server.update_session_watchlist(
                session_name,
                symbols=payload.symbols,
                add=payload.add,
                remove=payload.remove,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    @app.post("/api/sessions/{session_name}/stop")
    async def stop_session_endpoint(
        request: Request,
        session_name: str,
    ) -> dict[str, Any]:
        daemon_server.require_http_auth(request)
        try:
            return await daemon_server.stop_session_run(session_name)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    @app.websocket(DEFAULT_DAEMON_WS_PATH)
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()

        if not daemon_server.authorize_websocket(websocket):
            await _send_error(websocket, "unauthorized", "daemon bearer token required")
            await websocket.close(code=1008)
            return

        try:
            first_message = await _receive_client_envelope(websocket)
        except ValueError as exc:
            await _send_error(websocket, "bad_request", str(exc))
            await websocket.close(code=1003)
            return

        if not isinstance(first_message, AttachEnvelope):
            await _send_error(
                websocket,
                "bad_request",
                "first client message must be an attach envelope",
            )
            await websocket.close(code=1008)
            return

        try:
            managed = await daemon_server.get_or_create_session(
                first_message.session,
                create_if_missing=first_message.create_if_missing,
            )
        except (KeyError, TypeError, ValueError) as exc:
            await _send_error(websocket, "attach_failed", str(exc))
            await websocket.close(code=1008)
            return

        session = managed.session
        subscriptions: dict[str, Any] = {"signals": False, "chart": set(), "nats": False}
        event_queue = session.subscribe_events()
        forward_task = asyncio.create_task(
            daemon_server.forward_session_events(
                websocket,
                session,
                event_queue,
                subscriptions,
            )
        )

        await _send_server_envelope(
            websocket,
            SessionAttachedEnvelope(
                type="session_attached",
                session=session.name,
                state=daemon_server.session_snapshot(session),
            ),
        )
        await _send_server_envelope(
            websocket,
            StatusEnvelope(
                type="status",
                activity=session.activity_status,
                queue=len(session.input_queue),
            ),
        )

        try:
            while True:
                try:
                    payload = await _receive_client_envelope(websocket)
                except ValueError as exc:
                    await _send_error(websocket, "bad_request", str(exc))
                    continue

                if isinstance(payload, InputEnvelope):
                    if payload.text.strip().startswith("/auto"):
                        command, args = _split_slash_command(payload.text)
                        log_slash_command(session, command, args, "intercepted:auto")
                        try:
                            response_text = await daemon_server.handle_auto_command(
                                managed,
                                payload.text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "auto_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    if payload.text.strip().startswith("/schedule"):
                        command, args = _split_slash_command(payload.text)
                        log_slash_command(session, command, args, "intercepted:schedule")
                        try:
                            response_text = await daemon_server.handle_schedule_command(
                                managed,
                                payload.text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "schedule_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    if payload.text.strip().startswith("/optimizer"):
                        command, args = _split_slash_command(payload.text)
                        log_slash_command(session, command, args, "intercepted:optimizer")
                        try:
                            response_text = await daemon_server.handle_optimizer_command(
                                managed,
                                payload.text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "optimizer_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    if payload.text.strip().startswith("/strategies"):
                        command, args = _split_slash_command(payload.text)
                        log_slash_command(session, command, args, "intercepted:strategies")
                        try:
                            response_text = await daemon_server.handle_strategies_command(
                                managed,
                                payload.text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "strategies_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    if payload.text.strip().startswith("/"):
                        command, args = _split_slash_command(payload.text)
                        log_slash_command(session, command, args, "forwarded:input")
                    await daemon_server.run_input(managed, payload.text)
                    continue

                if isinstance(payload, SlashEnvelope):
                    if payload.command.strip() == "/auto":
                        command_text = payload.command.strip()
                        if payload.args.strip():
                            command_text = f"{command_text} {payload.args.strip()}"
                        log_slash_command(
                            session,
                            payload.command.strip(),
                            payload.args.strip(),
                            "intercepted:auto",
                        )
                        try:
                            response_text = await daemon_server.handle_auto_command(
                                managed,
                                command_text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "auto_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    if payload.command.strip() == "/schedule":
                        command_text = payload.command.strip()
                        if payload.args.strip():
                            command_text = f"{command_text} {payload.args.strip()}"
                        log_slash_command(
                            session,
                            payload.command.strip(),
                            payload.args.strip(),
                            "intercepted:schedule",
                        )
                        try:
                            response_text = await daemon_server.handle_schedule_command(
                                managed,
                                command_text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "schedule_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    if payload.command.strip() == "/optimizer":
                        command_text = payload.command.strip()
                        if payload.args.strip():
                            command_text = f"{command_text} {payload.args.strip()}"
                        log_slash_command(
                            session,
                            payload.command.strip(),
                            payload.args.strip(),
                            "intercepted:optimizer",
                        )
                        try:
                            response_text = await daemon_server.handle_optimizer_command(
                                managed,
                                command_text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "optimizer_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    if payload.command.strip() == "/strategies":
                        command_text = payload.command.strip()
                        if payload.args.strip():
                            command_text = f"{command_text} {payload.args.strip()}"
                        log_slash_command(
                            session,
                            payload.command.strip(),
                            payload.args.strip(),
                            "intercepted:strategies",
                        )
                        try:
                            response_text = await daemon_server.handle_strategies_command(
                                managed,
                                command_text,
                            )
                            await _send_server_envelope(
                                websocket,
                                FinalEnvelope(type="final", text=response_text),
                            )
                        except Exception as exc:  # noqa: BLE001
                            await _send_error(websocket, "strategies_failed", str(exc))
                        await _send_server_envelope(
                            websocket,
                            StatusEnvelope(
                                type="status",
                                activity=session.activity_status,
                                queue=len(session.input_queue),
                            ),
                        )
                        continue
                    parts = [payload.command.strip()]
                    if payload.args.strip():
                        parts.append(payload.args.strip())
                    log_slash_command(
                        session,
                        payload.command.strip(),
                        payload.args.strip(),
                        "forwarded:slash",
                    )
                    await daemon_server.run_input(managed, " ".join(parts))
                    continue

                if isinstance(payload, HeartbeatEnvelope):
                    continue

                if isinstance(payload, InterruptEnvelope):
                    try:
                        await daemon_server.stop_session_run(session.name)
                    except Exception as exc:  # noqa: BLE001
                        await _send_error(websocket, "interrupt_failed", str(exc))
                    continue

                if isinstance(payload, SubscribeEnvelope):
                    if payload.channel == "signals":
                        subscriptions["signals"] = True
                    elif payload.channel == "chart":
                        subscriptions["chart"].add((payload.symbol, payload.tf))
                    elif payload.channel == "nats":
                        subscriptions["nats"] = True
                    continue

                if isinstance(payload, UnsubscribeEnvelope):
                    if payload.channel == "signals":
                        subscriptions["signals"] = False
                    elif payload.channel == "chart":
                        subscriptions["chart"].discard((payload.symbol, payload.tf))
                    elif payload.channel == "nats":
                        subscriptions["nats"] = False
                    continue

                await _send_error(
                    websocket,
                    "bad_request",
                    f"unsupported message type: {type(payload).__name__}",
                )
        except WebSocketDisconnect:
            pass
        finally:
            if session.auto_mode:
                session.stop_auto_mode("client disconnected")
            session.event_bus.unsubscribe(event_queue)
            forward_task.cancel()
            with suppress(asyncio.CancelledError):
                await forward_task

    @app.get("/", include_in_schema=False)
    async def web_index():
        index_path = _resolve_web_asset_path(build_dir, "")
        if index_path is None:
            return _missing_web_build_response(build_dir)
        return FileResponse(index_path)

    @app.get("/{asset_path:path}", include_in_schema=False)
    async def web_asset(asset_path: str):
        index_path = _resolve_web_asset_path(build_dir, "")
        if index_path is None:
            return _missing_web_build_response(build_dir)

        resolved = _resolve_web_asset_path(build_dir, asset_path)
        if resolved is not None:
            return FileResponse(resolved)

        if _looks_like_web_asset_request(asset_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="web asset not found",
            )
        return FileResponse(index_path)

    return app


app = create_app()

"""Shared contracts for signal-router action executors."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from daemon.signal_router.domain_model import ActionDescriptor


ACTION_STATUS_FIRED = "fired"
ACTION_STATUS_SKIPPED = "skipped"
ACTION_STATUS_FAILED = "failed"
ACTION_STATUS_SUPPRESSED_DRY_RUN = "suppressed_dry_run"


@dataclass(frozen=True)
class ValidationError:
    """One action validation issue."""

    field: str
    message: str


@dataclass(frozen=True)
class ActionResult:
    """Outcome of one action executor invocation."""

    kind: str
    target: str | None
    status: str
    detail: str | None
    metrics: dict[str, Any]


@dataclass
class ExecutionContext:
    """Runtime dependencies available to action executors."""

    dry_run: bool = False
    channel: str | None = None
    subject: str | None = None
    route_name: str | None = None
    sessions: Mapping[str, Any] = field(default_factory=dict)
    dedup_table: Any | None = None
    event_injector: Any | None = None
    daemon_event_publisher: Callable[[str, dict[str, Any]], Any] | None = None
    nats_publisher: Callable[[str, dict[str, Any]], Any] | None = None
    webhook_poster: Callable[[str, dict[str, Any]], Any] | None = None
    http_poster: Callable[..., Any] | None = None
    chat_logger: Callable[[str], Any] | None = None
    audit_writer: Callable[[dict[str, Any]], Any] | None = None
    telemetry_emitter: Callable[[str, dict[str, Any]], Any] | None = None
    autotrade_enabled: Callable[[], bool] = lambda: False
    monotonic_seconds: Callable[[], float] = time.monotonic


class ActionExecutor(Protocol):
    """Executor interface for one public action kind."""

    kind: str

    def validate(self, action: ActionDescriptor) -> list[ValidationError]:
        """Return config validation errors for an action descriptor."""

    def execute(
        self,
        action: ActionDescriptor,
        envelope: dict[str, Any],
        context: ExecutionContext,
    ) -> ActionResult:
        """Execute or dry-run one action."""


class SafeFormatDict(dict):
    """Format mapping that keeps unknown placeholders visible."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def stable_json(value: Any) -> str:
    """Return deterministic JSON for template payload fields."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def event_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return the routed event payload with top-level envelope metadata."""

    payload = envelope.get("payload")
    if isinstance(payload, dict):
        merged = dict(payload)
    else:
        merged = {
            key: value
            for key, value in envelope.items()
            if key not in {"headers"}
        }
    for key in ("subject", "channel", "received_at"):
        if key in envelope:
            merged.setdefault(key, envelope[key])
    return merged


def render_template(template: str, envelope: dict[str, Any], context: ExecutionContext) -> str:
    """Render a signal-router inline template against routed event fields."""

    values = event_payload(envelope)
    values.setdefault("channel", context.channel or envelope.get("channel", ""))
    values.setdefault("subject", context.subject or envelope.get("subject", ""))
    values.setdefault("raw_event", stable_json(envelope))
    values.setdefault("payload_json", stable_json(values))
    return template.format_map(SafeFormatDict(values))


def emit_telemetry(
    context: ExecutionContext,
    topic: str,
    payload: dict[str, Any],
) -> None:
    """Emit optional telemetry without letting telemetry failures block routes."""

    if context.telemetry_emitter is not None:
        try:
            _dispatch_maybe_async(context.telemetry_emitter(topic, dict(payload)))
        except Exception:  # noqa: BLE001
            return
    if context.daemon_event_publisher is not None:
        try:
            _dispatch_maybe_async(context.daemon_event_publisher(topic, dict(payload)))
        except Exception:  # noqa: BLE001
            return


def write_audit(context: ExecutionContext, payload: dict[str, Any]) -> None:
    """Write an optional structured audit record."""

    if context.audit_writer is None:
        return
    try:
        _dispatch_maybe_async(context.audit_writer(dict(payload)))
    except Exception:  # noqa: BLE001
        return


def dry_run_result(
    action: ActionDescriptor,
    *,
    detail: str = "would_fire",
    metrics: dict[str, Any] | None = None,
) -> ActionResult:
    """Return a standard dry-run result and caller-visible metric payload."""

    return ActionResult(
        kind=action.kind,
        target=action.target,
        status=ACTION_STATUS_SUPPRESSED_DRY_RUN,
        detail=detail,
        metrics={"would_fire": True, **(metrics or {})},
    )


def _dispatch_maybe_async(result: Any) -> None:
    if not inspect.isawaitable(result):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(result)
    else:
        loop.create_task(result)

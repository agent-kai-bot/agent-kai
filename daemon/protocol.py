"""Typed wire-protocol envelopes for daemon/client communication."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProtocolModel(BaseModel):
    """Base model with strict extra-field rejection."""

    model_config = ConfigDict(extra="forbid")


class ChatHistoryEntry(ProtocolModel):
    """One serialized chat turn in an attach snapshot."""

    role: NonEmptyString
    content: str
    ts: str | None = None


class SessionStateSnapshot(ProtocolModel):
    """Attach-time session snapshot sent to newly connected clients."""

    chart_symbol: str = "BTC"
    chart_timeframe: str = "1m"
    chart_source: str = "kai-api"
    chart_layout_mode: str = "dashboard"
    chart_color_scheme: str = "classic"
    watchlist_symbols: list[str] = Field(default_factory=list)
    autotrade_enabled: bool = False
    activity_status: str = "idle"
    auto_mode: bool = False
    auto_readonly: bool = False
    auto_iterations_total: int = 0
    auto_iterations_remaining: int = 0
    auto_elapsed_seconds: float = 0.0
    chat_history_total: int = 0
    chat_history_omitted: int = 0
    chat_history: list[ChatHistoryEntry] = Field(default_factory=list)


class AttachEnvelope(ProtocolModel):
    type: Literal["attach"]
    session: NonEmptyString
    create_if_missing: bool = False


class InputEnvelope(ProtocolModel):
    type: Literal["input"]
    text: NonEmptyString


class InterruptEnvelope(ProtocolModel):
    type: Literal["interrupt"]


class SubscribeEnvelope(ProtocolModel):
    type: Literal["subscribe"]
    channel: Literal["chart", "signals", "nats"]
    symbol: str | None = None
    tf: str | None = None

    @model_validator(mode="after")
    def validate_chart_fields(self) -> "SubscribeEnvelope":
        if self.channel == "chart" and (not self.symbol or not self.tf):
            raise ValueError("chart subscriptions require symbol and tf")
        return self


class UnsubscribeEnvelope(ProtocolModel):
    type: Literal["unsubscribe"]
    channel: Literal["chart", "signals", "nats"]
    symbol: str | None = None
    tf: str | None = None

    @model_validator(mode="after")
    def validate_chart_fields(self) -> "UnsubscribeEnvelope":
        if self.channel == "chart" and (not self.symbol or not self.tf):
            raise ValueError("chart unsubscriptions require symbol and tf")
        return self


class SlashEnvelope(ProtocolModel):
    type: Literal["slash"]
    command: NonEmptyString
    args: str = ""


class HeartbeatEnvelope(ProtocolModel):
    type: Literal["heartbeat"]


ClientEnvelope = Annotated[
    AttachEnvelope
    | InputEnvelope
    | InterruptEnvelope
    | SubscribeEnvelope
    | UnsubscribeEnvelope
    | SlashEnvelope
    | HeartbeatEnvelope,
    Field(discriminator="type"),
]


class SessionAttachedEnvelope(ProtocolModel):
    type: Literal["session_attached"]
    session: NonEmptyString
    state: SessionStateSnapshot


class TokenEnvelope(ProtocolModel):
    type: Literal["token"]
    text: str


class ToolStartEnvelope(ProtocolModel):
    type: Literal["tool_start"]
    tool: str
    args: Any = None


class ToolEndEnvelope(ProtocolModel):
    type: Literal["tool_end"]
    tool: str
    elapsed_ms: int | None = None
    ok: bool


class FinalEnvelope(ProtocolModel):
    type: Literal["final"]
    text: str


class StatusEnvelope(ProtocolModel):
    type: Literal["status"]
    activity: str
    queue: int = 0


class AutoStartedEnvelope(ProtocolModel):
    type: Literal["auto_started"]
    readonly: bool = False
    iterations_total: int = 0
    iterations_remaining: int = 0
    iterations_used: int = 0
    elapsed_seconds: float = 0.0


class AutoStoppedEnvelope(ProtocolModel):
    type: Literal["auto_stopped"]
    readonly: bool = False
    iterations_total: int = 0
    iterations_remaining: int = 0
    iterations_used: int = 0
    elapsed_seconds: float = 0.0
    reason: str = ""


class AutoProgressEnvelope(ProtocolModel):
    type: Literal["auto_progress"]
    readonly: bool = False
    iterations_total: int = 0
    iterations_remaining: int = 0
    iterations_used: int = 0
    elapsed_seconds: float = 0.0


class SignalEnvelope(ProtocolModel):
    type: Literal["signal"]
    signal: Any


class ChartBarEnvelope(ProtocolModel):
    type: Literal["chart_bar"]
    symbol: str
    tf: str
    bar: Any


class ChartViewEnvelope(ProtocolModel):
    type: Literal["chart_view"]
    chart_symbol: str
    chart_timeframe: str
    chart_source: str
    chart_layout_mode: str


class WatchlistEnvelope(ProtocolModel):
    type: Literal["watchlist"]
    watchlist_symbols: list[str]


class NatsEventEnvelope(ProtocolModel):
    type: Literal["nats_event"]
    direction: NonEmptyString
    subject: NonEmptyString
    payload: Any


class ErrorEnvelope(ProtocolModel):
    type: Literal["error"]
    code: NonEmptyString
    message: str


class ScheduledJobCreatedEnvelope(ProtocolModel):
    type: Literal["scheduled_job_created"]
    job: Any


class ScheduledJobTriggeredEnvelope(ProtocolModel):
    type: Literal["scheduled_job_triggered"]
    job_id: NonEmptyString
    fired_at: NonEmptyString
    target_agent_role: str | None = None
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    extra_env: dict[str, str] | None = None


class ScheduledJobCompletedEnvelope(ProtocolModel):
    type: Literal["scheduled_job_completed"]
    job_id: NonEmptyString
    result_preview: str | None = None
    target_agent_role: str | None = None
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    extra_env: dict[str, str] | None = None


class ScheduledJobFailedEnvelope(ProtocolModel):
    type: Literal["scheduled_job_failed"]
    job_id: NonEmptyString
    error: str
    target_agent_role: str | None = None
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    extra_env: dict[str, str] | None = None


class ScheduledJobCancelledEnvelope(ProtocolModel):
    type: Literal["scheduled_job_cancelled"]
    job_id: NonEmptyString
    target_agent_role: str | None = None
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    extra_env: dict[str, str] | None = None


class ScheduledJobPausedEnvelope(ProtocolModel):
    type: Literal["scheduled_job_paused"]
    job_id: NonEmptyString
    target_agent_role: str | None = None
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    extra_env: dict[str, str] | None = None


class ScheduledJobResumedEnvelope(ProtocolModel):
    type: Literal["scheduled_job_resumed"]
    job_id: NonEmptyString
    target_agent_role: str | None = None
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    extra_env: dict[str, str] | None = None


class OptimizerCompletedEnvelope(ProtocolModel):
    type: Literal["optimizer_completed"]
    session: NonEmptyString
    cycle_count: int = 0
    cancelled: bool = False
    error: str | None = None
    last_cycle_result: Any = None


ServerEnvelope = Annotated[
    SessionAttachedEnvelope
    | TokenEnvelope
    | ToolStartEnvelope
    | ToolEndEnvelope
    | FinalEnvelope
    | StatusEnvelope
    | AutoStartedEnvelope
    | AutoStoppedEnvelope
    | AutoProgressEnvelope
    | SignalEnvelope
    | ChartBarEnvelope
    | ChartViewEnvelope
    | WatchlistEnvelope
    | NatsEventEnvelope
    | ScheduledJobCreatedEnvelope
    | ScheduledJobTriggeredEnvelope
    | ScheduledJobCompletedEnvelope
    | ScheduledJobFailedEnvelope
    | ScheduledJobCancelledEnvelope
    | ScheduledJobPausedEnvelope
    | ScheduledJobResumedEnvelope
    | OptimizerCompletedEnvelope
    | ErrorEnvelope,
    Field(discriminator="type"),
]

CLIENT_ENVELOPE_ADAPTER = TypeAdapter(ClientEnvelope)
SERVER_ENVELOPE_ADAPTER = TypeAdapter(ServerEnvelope)


def _coerce_json_object(payload: str | bytes | bytearray | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON payload") from exc
    if not isinstance(decoded, dict):
        raise ValueError("message payload must be a JSON object")
    return decoded


def decode_client_envelope(
    payload: str | bytes | bytearray | dict[str, Any],
) -> ClientEnvelope:
    """Parse and validate one client envelope."""
    data = _coerce_json_object(payload)
    try:
        return CLIENT_ENVELOPE_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise ValueError(exc.errors(include_url=False)[0]["msg"]) from exc


def decode_server_envelope(
    payload: str | bytes | bytearray | dict[str, Any],
) -> ServerEnvelope:
    """Parse and validate one server envelope."""
    data = _coerce_json_object(payload)
    try:
        return SERVER_ENVELOPE_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise ValueError(exc.errors(include_url=False)[0]["msg"]) from exc


def encode_envelope(envelope: BaseModel) -> dict[str, Any]:
    """Convert an envelope model into a JSON-ready dict."""
    return envelope.model_dump(mode="json", exclude_none=True)

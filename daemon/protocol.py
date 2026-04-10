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
    channel: Literal["chart", "signals"]
    symbol: str | None = None
    tf: str | None = None

    @model_validator(mode="after")
    def validate_chart_fields(self) -> "SubscribeEnvelope":
        if self.channel == "chart" and (not self.symbol or not self.tf):
            raise ValueError("chart subscriptions require symbol and tf")
        return self


class UnsubscribeEnvelope(ProtocolModel):
    type: Literal["unsubscribe"]
    channel: Literal["chart", "signals"]
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


class SignalEnvelope(ProtocolModel):
    type: Literal["signal"]
    signal: Any


class ChartBarEnvelope(ProtocolModel):
    type: Literal["chart_bar"]
    symbol: str
    tf: str
    bar: Any


class ErrorEnvelope(ProtocolModel):
    type: Literal["error"]
    code: NonEmptyString
    message: str


ServerEnvelope = Annotated[
    SessionAttachedEnvelope
    | TokenEnvelope
    | ToolStartEnvelope
    | ToolEndEnvelope
    | FinalEnvelope
    | StatusEnvelope
    | SignalEnvelope
    | ChartBarEnvelope
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


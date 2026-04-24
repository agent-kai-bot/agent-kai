"""Pydantic models for OpenClaw-compatible gateway payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GatewayModel(BaseModel):
    """Base gateway model with strict extra-field rejection."""

    model_config = ConfigDict(extra="forbid")


class ToolsInvokeRequest(GatewayModel):
    """OpenClaw-compatible ``/tools/invoke`` request."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class SessionSpawnArgs(GatewayModel):
    """Arguments accepted for ``sessions_spawn``."""

    agentId: str
    task: str
    label: str = ""
    cleanup: str = "keep"
    model: str | None = None


class SessionSendArgs(GatewayModel):
    """Arguments accepted for ``sessions_send``."""

    sessionKey: str
    message: str
    timeoutSeconds: int | None = None


class SessionListArgs(GatewayModel):
    """Arguments accepted for ``sessions_list``."""

    limit: int = 50
    messageLimit: int | None = None


class CronWakeRequest(GatewayModel):
    """Payload accepted by the OpenClaw-compatible cron wake endpoint."""

    action: str = "wake"
    text: str = ""


class ToolInvokeResponse(GatewayModel):
    """OpenClaw-compatible tool response envelope."""

    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None


class RunSummary(GatewayModel):
    """Session/run summary returned through ``sessions_list``."""

    key: str
    agentId: str
    status: str
    label: str
    runId: str
    taskId: int | None = None
    createdAt: str
    updatedAt: str
    endedAt: str | None = None
    display: str | None = None


class StatusResponse(GatewayModel):
    """Gateway health/status payload."""

    status: Literal["ok"]
    service: Literal["taskboard-agent-gateway"] = "taskboard-agent-gateway"
    runs: int

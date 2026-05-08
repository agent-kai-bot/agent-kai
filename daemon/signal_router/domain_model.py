"""Core configuration data types for the daemon signal router."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionDescriptor:
    """A typed terminal effect declared by a router route."""

    kind: str
    target: str | None
    params: dict[str, Any]


@dataclass(frozen=True)
class Channel:
    """A named NATS subject group plus optional payload schema name."""

    name: str
    subjects: list[str]
    schema: str | None


@dataclass(frozen=True)
class Route:
    """A channel-scoped rule and its ordered action descriptors."""

    name: str
    channel: str
    match: dict[str, Any]
    actions: list[ActionDescriptor]
    pre_action: dict[str, Any] | None
    enabled: bool
    cooldown_seconds: int = 0
    requires_autotrade: bool = False
    config: dict[str, Any] = field(default_factory=dict)

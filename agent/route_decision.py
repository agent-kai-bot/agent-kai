"""Structured auto-fire routing decisions for taskboard dispatcher events."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskStatus(str, Enum):
    """Normalized task statuses understood by the router boundary."""

    IN_PROGRESS = "in progress"
    IN_PROGRESS_SNAKE = "in_progress"
    REVIEW = "review"


@dataclass(frozen=True)
class RouteDecision:
    """One auditable dispatcher routing decision.

    Attributes:
        role: Canonical taskboard display role to spawn.
        reason: Stable route-reason label for logs and audits.
        concurrency_group: Group identifier for future capacity policy.
        allow_parallel: Whether this decision may run alongside peers.
    """

    role: str
    reason: str
    concurrency_group: str | None = None
    allow_parallel: bool = True

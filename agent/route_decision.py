"""Structured auto-fire routing decisions for taskboard dispatcher events."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskStatus(str, Enum):
    """Normalized task statuses understood by the router boundary.

    SPEC v23 canonical sequential pipeline:
    Backlog -> In Progress -> Code Review -> Security Audit -> QA -> Ready to Merge -> Done

    The legacy ``Review`` lump is mapped forward to ``Code Review`` so existing
    tasks still in that status route to a single role (CR) instead of fanning
    out to CR + SA + QA in parallel — which violates the SRP-V16-ACTIVE-STAGE
    invariant and races the per-task session_token generation.
    """

    IN_PROGRESS = "in progress"
    IN_PROGRESS_SNAKE = "in_progress"
    REVIEW = "review"
    CODE_REVIEW = "code review"
    SECURITY_AUDIT = "security audit"
    QA = "qa"
    READY_TO_MERGE = "ready to merge"
    FIXING = "fixing"


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

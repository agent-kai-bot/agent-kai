"""Map task events to structured auto-fire routing decisions.

Used by the taskboard dispatcher to decide which role(s) to spawn for a given
``task.status_changed`` event. Routing produces auditable
:class:`agent.route_decision.RouteDecision` values consumed by the dispatcher.

Routing model
-------------

SPEC v23 sequential staged-review pipeline:

    *  -> "Code Review"     : ["Code Reviewer"]
    *  -> "Security Audit"  : ["Security Auditor"]
    *  -> "QA"              : ["QA Agent"]
    *  -> "Ready to Merge"  : []   (orchestrator/merger TBD; not auto-fired)
    *  -> "Review" (legacy) : ["Code Reviewer"]   (forward-compat alias)

Implementation stages route by ``latest_task.agent`` (Router v2 #10258):

    *  -> "In Progress"     : route by ``latest_task.agent``
    *  -> "Fixing"          : route by ``latest_task.agent``

When ``latest_task.agent`` is missing, fall back to a ``task_type`` -> role
table, and if that is also missing fail-closed (return ``()``) so the
dispatcher can mark the row ``unknown_role`` and post an audit comment.

Other transitions return ``()`` (no-op).

Why
---

The legacy ``Review`` -> CR + SA + QA parallel fanout was the cause of the
parallel-mint session_token generation race (Router v2 #10276); single-role
fanout per status entry eliminates that race by construction.

The hardcoded ``"in_progress" -> Developer`` mapping silently dropped
non-Developer task assignments (Architect design tickets, QA-led, security-led,
ops/runbook). Routing by ``task.agent`` honors the operator's intent and
fail-closing on unknown agents catches misfiled tickets at dispatch time
instead of in a Developer agent that improvises without context.
"""

from __future__ import annotations

from typing import Any

from agent.route_decision import RouteDecision, TaskStatus

_REASON_BY_STATUS: dict[str, str] = {
    TaskStatus.IN_PROGRESS.value: "status_to_in_progress",
    TaskStatus.IN_PROGRESS_SNAKE.value: "status_to_in_progress",
    TaskStatus.FIXING.value: "status_to_fixing",
}

_AGENT_TO_ROLE: dict[str, str] = {
    "developer": "Developer",
    "architect": "Architect",
    "code reviewer": "Code Reviewer",
    "code-reviewer": "Code Reviewer",
    "security auditor": "Security Auditor",
    "security-auditor": "Security Auditor",
    "qa agent": "QA Agent",
    "qa-agent": "QA Agent",
    "qa": "QA Agent",
    "orchestrator": "Orchestrator",
}

_TASK_TYPE_TO_ROLE: dict[str, str] = {
    "design": "Architect",
    "architecture": "Architect",
    "spec": "Architect",
    "security": "Security Auditor",
    "audit": "Security Auditor",
    "qa": "QA Agent",
    "test": "QA Agent",
    "bug": "Developer",
    "feature": "Developer",
    "chore": "Developer",
    "fix": "Developer",
    "refactor": "Developer",
    "docs": "Developer",
}

_CONCURRENCY_GROUP_FOR_ROLE: dict[str, str] = {
    "Developer": "implementation",
    "Architect": "implementation",
    "Code Reviewer": "review",
    "Security Auditor": "review",
    "QA Agent": "review",
    "Orchestrator": "orchestration",
}

_REVIEW_DECISIONS: dict[str, RouteDecision] = {
    TaskStatus.CODE_REVIEW.value: RouteDecision(
        role="Code Reviewer",
        reason="status_to_code_review",
        concurrency_group="review",
        allow_parallel=False,
    ),
    TaskStatus.SECURITY_AUDIT.value: RouteDecision(
        role="Security Auditor",
        reason="status_to_security_audit",
        concurrency_group="review",
        allow_parallel=False,
    ),
    TaskStatus.QA.value: RouteDecision(
        role="QA Agent",
        reason="status_to_qa",
        concurrency_group="review",
        allow_parallel=False,
    ),
    TaskStatus.REVIEW.value: RouteDecision(
        role="Code Reviewer",
        reason="status_to_review_legacy_alias",
        concurrency_group="review",
        allow_parallel=False,
    ),
}


def resolve_role_for_task(latest_task: dict[str, Any] | None) -> str | None:
    """Return the canonical role for a task, or ``None`` if unresolved.

    Order of resolution (Router v2 #10258):

    1. ``latest_task.agent`` (case-insensitive, with kebab/space variants)
    2. ``latest_task.task_type`` fallback (``design`` -> Architect, etc.)
    3. ``None`` (fail-closed; caller posts ``unknown_role`` audit)
    """

    if not latest_task:
        return None
    agent = latest_task.get("agent")
    if isinstance(agent, str) and agent.strip():
        normalized = agent.strip().lower()
        role = _AGENT_TO_ROLE.get(normalized)
        if role is not None:
            return role
    task_type = latest_task.get("task_type")
    if isinstance(task_type, str) and task_type.strip():
        return _TASK_TYPE_TO_ROLE.get(task_type.strip().lower())
    return None


def route_event(
    payload: dict[str, Any],
    latest_task: dict[str, Any] | None,
    review_context: dict[str, Any] | None,
) -> tuple[RouteDecision, ...]:
    """Return structured routing decisions for one taskboard event.

    Args:
        payload: Raw webhook/event payload containing ``from_status`` and
            ``to_status``.
        latest_task: Most recent taskboard task document. Inspected for
            ``agent`` and ``task_type`` when routing implementation stages.
        review_context: Review metadata bundle accepted for future policy.

    Returns:
        Tuple of :class:`RouteDecision` objects. Empty tuple if the transition
        is a no-op (identity transition, unknown status, or fail-closed
        unknown-role on an actionable status).
    """

    del review_context  # accepted for future verdict-driven gating

    to_status = payload.get("to_status")
    if to_status is None:
        return ()

    normalized = str(to_status).strip().lower()
    from_status = str(payload.get("from_status") or "").strip().lower()
    if from_status == normalized:
        return ()

    review_decision = _REVIEW_DECISIONS.get(normalized)
    if review_decision is not None:
        return (review_decision,)

    reason = _REASON_BY_STATUS.get(normalized)
    if reason is None:
        return ()

    role = resolve_role_for_task(latest_task)
    if role is None:
        return ()
    return (
        RouteDecision(
            role=role,
            reason=reason,
            concurrency_group=_CONCURRENCY_GROUP_FOR_ROLE.get(role, "implementation"),
            allow_parallel=False,
        ),
    )

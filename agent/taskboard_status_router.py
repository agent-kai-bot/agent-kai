"""Map task events to structured auto-fire routing decisions.

Used by the taskboard dispatcher to decide which role(s) to spawn for a given
``task.status_changed`` event. Routing produces auditable
:class:`agent.route_decision.RouteDecision` values consumed by the dispatcher.

SPEC v23 sequential staged-review routing:

    *  -> "In Progress"     : ["Developer"]
    *  -> "Code Review"     : ["Code Reviewer"]
    *  -> "Security Audit"  : ["Security Auditor"]
    *  -> "QA"              : ["QA Agent"]
    *  -> "Fixing"          : ["Developer"]
    *  -> "Ready to Merge"  : []   (orchestrator/merger TBD; not auto-fired)
    *  -> "Review" (legacy) : ["Code Reviewer"]   (forward-compat alias)
    *  other transitions    : []  (no-op)

Verdict-driven advancement (CR APPROVED -> task moves to Security Audit, etc.)
is the responsibility of the taskboard side; the router only converts the
resulting ``task.status_changed`` event into the next role to fire.

The legacy ``Review`` -> CR + SA + QA parallel fanout was the cause of the
parallel-mint session_token generation race (Router v2 #10276); single-role
fanout per status entry eliminates that race by construction.
"""

from __future__ import annotations

from typing import Any

from agent.route_decision import RouteDecision, TaskStatus

_ROUTE_DECISIONS_FOR_STATUS: dict[str, tuple[RouteDecision, ...]] = {
    TaskStatus.IN_PROGRESS.value: (
        RouteDecision(
            role="Developer",
            reason="status_to_in_progress",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    ),
    TaskStatus.IN_PROGRESS_SNAKE.value: (
        RouteDecision(
            role="Developer",
            reason="status_to_in_progress",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    ),
    TaskStatus.CODE_REVIEW.value: (
        RouteDecision(
            role="Code Reviewer",
            reason="status_to_code_review",
            concurrency_group="review",
            allow_parallel=False,
        ),
    ),
    TaskStatus.SECURITY_AUDIT.value: (
        RouteDecision(
            role="Security Auditor",
            reason="status_to_security_audit",
            concurrency_group="review",
            allow_parallel=False,
        ),
    ),
    TaskStatus.QA.value: (
        RouteDecision(
            role="QA Agent",
            reason="status_to_qa",
            concurrency_group="review",
            allow_parallel=False,
        ),
    ),
    TaskStatus.FIXING.value: (
        RouteDecision(
            role="Developer",
            reason="status_to_fixing",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    ),
    TaskStatus.REVIEW.value: (
        RouteDecision(
            role="Code Reviewer",
            reason="status_to_review_legacy_alias",
            concurrency_group="review",
            allow_parallel=False,
        ),
    ),
}


def route_event(
    payload: dict[str, Any],
    latest_task: dict[str, Any] | None,
    review_context: dict[str, Any] | None,
) -> tuple[RouteDecision, ...]:
    """Return structured routing decisions for one taskboard event.

    Args:
        payload: Raw webhook/event payload containing ``from_status`` and
            ``to_status``.
        latest_task: Most recent taskboard task document. Accepted for the v2
            boundary; not yet inspected for status-only routing.
        review_context: Review metadata bundle accepted for future policy.

    Returns:
        Tuple of :class:`RouteDecision` objects. Empty tuple if the transition
        is a no-op, including identity transitions and unknown statuses.
    """

    del latest_task, review_context  # boundary-only inputs for future phases

    from_status = payload.get("from_status")
    to_status = payload.get("to_status")
    if to_status is None:
        return ()

    normalized_to_status = str(to_status).strip().lower()
    if str(from_status or "").strip().lower() == normalized_to_status:
        return ()
    return _ROUTE_DECISIONS_FOR_STATUS.get(normalized_to_status, ())

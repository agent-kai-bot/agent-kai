"""Map task events to structured auto-fire routing decisions.

Used by the taskboard dispatcher to decide which role(s) to spawn for a given
``task.status_changed`` event. Router v2 keeps the v1 forward-path behavior
while upgrading the boundary from raw role tuples to auditable
:class:`agent.route_decision.RouteDecision` values.

Current behavior remains intentionally narrow:

    *  -> "In Progress" : ["Developer"]
    *  -> "Review"      : ["Code Reviewer", "Security Auditor", "QA Agent"]
    *  other transitions : []  (no-op)

Future task-aware reassignment, request-changes loops, and capacity policy are
out of scope for this boundary refactor.
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
    TaskStatus.REVIEW.value: (
        RouteDecision(
            role="Code Reviewer",
            reason="status_to_review",
            concurrency_group="review",
            allow_parallel=True,
        ),
        RouteDecision(
            role="Security Auditor",
            reason="status_to_review",
            concurrency_group="review",
            allow_parallel=True,
        ),
        RouteDecision(
            role="QA Agent",
            reason="status_to_review",
            concurrency_group="review",
            allow_parallel=True,
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
            boundary even though v1-compatible routing does not inspect it yet.
        review_context: Review metadata bundle accepted for future policy.

    Returns:
        Tuple of :class:`RouteDecision` objects. Empty tuple if the transition
        is a no-op, including identity transitions.
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

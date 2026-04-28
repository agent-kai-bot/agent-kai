"""Map task status transitions to the agent roles that should fire.

Used by the taskboard dispatcher to decide which role(s) to spawn for a given
``task.status_changed`` event. v1 implements the forward path only:

    *  -> "In Progress" : ["Developer"]
    *  -> "Review"      : ["Code Reviewer", "Security Auditor", "QA Agent"]
    other transitions   : []  (no-op)

Roles are returned as canonical taskboard display strings consumed by
``agent.taskboard_dispatcher.resolve_taskboard_role``.

The REQUEST_CHANGES loop, original-developer reassignment, and verdict
aggregation are out of scope for v1; they ship as v1.5.
"""

from __future__ import annotations

ROLES_FOR_TRANSITION = {
    "in progress": ("Developer",),
    "in_progress": ("Developer",),
    "review": ("Code Reviewer", "Security Auditor", "QA Agent"),
}


def roles_to_fire(from_status: str | None, to_status: str | None) -> tuple[str, ...]:
    """Return roles that should fire for a status transition.

    Args:
        from_status: Taskboard status the task moved from (may be None).
        to_status: Taskboard status the task moved to.

    Returns:
        Tuple of canonical role display strings to fire. Empty tuple if the
        transition is a no-op (every transition NOT in the v1 forward path,
        including identity transitions where ``from_status == to_status``).

    Example:
        >>> roles_to_fire("Backlog", "In Progress")
        ('Developer',)
        >>> roles_to_fire("In Progress", "Review")
        ('Code Reviewer', 'Security Auditor', 'QA Agent')
        >>> roles_to_fire("Review", "Done")
        ()
    """

    if not to_status:
        return ()
    normalized_to_status = to_status.strip().lower()
    if (from_status or "").strip().lower() == normalized_to_status:
        return ()
    return ROLES_FOR_TRANSITION.get(normalized_to_status, ())

"""Tests for taskboard status-transition role routing."""

from agent.route_decision import RouteDecision
from agent.taskboard_status_router import resolve_role_for_task, route_event


def _developer_task() -> dict:
    return {"id": 1, "agent": "Developer"}


def _developer_decision() -> RouteDecision:
    return RouteDecision(
        role="Developer",
        reason="status_to_in_progress",
        concurrency_group="implementation",
        allow_parallel=False,
    )


def _route(payload: dict, task: dict | None = None) -> tuple[RouteDecision, ...]:
    return route_event(payload, task if task is not None else _developer_task(), {})


# ----- SPEC v23 sequential staged-review pipeline -----


def test_backlog_to_in_progress_returns_developer_for_developer_task() -> None:
    assert _route({"from_status": "Backlog", "to_status": "In Progress"}) == (
        _developer_decision(),
    )


def test_in_progress_to_code_review_fires_only_code_reviewer() -> None:
    """SPEC v23 sequential: Code Review status fires CR alone (not CR + SA + QA)."""

    assert _route({"from_status": "In Progress", "to_status": "Code Review"}) == (
        RouteDecision(
            role="Code Reviewer",
            reason="status_to_code_review",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_code_review_to_security_audit_fires_only_security_auditor() -> None:
    """SPEC v23 sequential: Security Audit status fires SA alone."""

    assert _route(
        {"from_status": "Code Review", "to_status": "Security Audit"}
    ) == (
        RouteDecision(
            role="Security Auditor",
            reason="status_to_security_audit",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_security_audit_to_qa_fires_only_qa_agent() -> None:
    """SPEC v23 sequential: QA status fires QA alone."""

    assert _route({"from_status": "Security Audit", "to_status": "QA"}) == (
        RouteDecision(
            role="QA Agent",
            reason="status_to_qa",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_qa_to_ready_to_merge_is_noop() -> None:
    """Ready to Merge has no auto-fired role yet (orchestrator/merger TBD)."""

    assert _route({"from_status": "QA", "to_status": "Ready to Merge"}) == ()


def test_review_stage_to_fixing_routes_by_task_agent() -> None:
    """REQUEST_CHANGES routes to Fixing, fired by the original implementor's role."""

    expected_developer = (
        RouteDecision(
            role="Developer",
            reason="status_to_fixing",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )
    assert _route(
        {"from_status": "Code Review", "to_status": "Fixing"}
    ) == expected_developer
    assert _route(
        {"from_status": "Security Audit", "to_status": "Fixing"}
    ) == expected_developer
    assert _route({"from_status": "QA", "to_status": "Fixing"}) == expected_developer


def test_fixing_status_for_architect_fires_architect() -> None:
    """An architect task in Fixing routes back to the architect, not Developer."""

    expected = (
        RouteDecision(
            role="Architect",
            reason="status_to_fixing",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )
    assert _route(
        {"from_status": "Code Review", "to_status": "Fixing"},
        task={"id": 7, "agent": "Architect"},
    ) == expected


def test_fixing_to_code_review_fires_code_reviewer() -> None:
    """After dev pushes fix, status flips back to Code Review and CR fires alone."""

    assert _route({"from_status": "Fixing", "to_status": "Code Review"}) == (
        RouteDecision(
            role="Code Reviewer",
            reason="status_to_code_review",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_legacy_review_status_routes_to_code_reviewer_alone() -> None:
    """Backward compat: tasks still in legacy ``Review`` route to CR alone, not all three."""

    assert _route({"from_status": "In Progress", "to_status": "Review"}) == (
        RouteDecision(
            role="Code Reviewer",
            reason="status_to_review_legacy_alias",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_review_to_done_is_noop() -> None:
    assert _route({"from_status": "Review", "to_status": "Done"}) == ()


def test_code_approve_verdict_fires_security_auditor() -> None:
    assert _route(
        {
            "event_type": "review.verdict_submitted",
            "gate_type": "code",
            "verdict": "APPROVED",
        }
    ) == (
        RouteDecision(
            role="Security Auditor",
            reason="review_verdict_code_approved",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_security_approve_verdict_fires_qa_agent() -> None:
    assert _route(
        {
            "event_type": "review.verdict_submitted",
            "gate_type": "security",
            "verdict": "APPROVE",
        }
    ) == (
        RouteDecision(
            role="QA Agent",
            reason="review_verdict_security_approved",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_qa_approve_verdict_is_noop() -> None:
    assert _route(
        {
            "event_type": "review.verdict_submitted",
            "gate_type": "qa",
            "verdict": "APPROVED",
        }
    ) == ()


def test_late_approve_verdict_after_done_is_noop() -> None:
    assert _route(
        {
            "event_type": "review.verdict_submitted",
            "gate_type": "code",
            "verdict": "APPROVED",
        },
        task={"id": 16, "agent": "Developer", "status": "Done"},
    ) == ()


def test_request_changes_verdict_is_move_only_for_dispatcher() -> None:
    assert _route(
        {
            "event_type": "review.verdict_submitted",
            "gate_type": "code",
            "verdict": "REQUEST_CHANGES",
        },
        task={"id": 15, "agent": "Code Reviewer", "implementation_agent": "Architect"},
    ) == ()


def test_fixing_status_prefers_implementation_agent() -> None:
    assert _route(
        {
            "event_type": "task.status_changed",
            "from_status": "Code Review",
            "to_status": "Fixing",
        },
        task={"id": 15, "agent": "Code Reviewer", "implementation_agent": "Architect"},
    ) == (
        RouteDecision(
            role="Architect",
            reason="status_to_fixing",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )


def test_task_prefixed_review_verdict_event_alias_is_accepted() -> None:
    assert _route(
        {
            "event_type": "task.review_verdict_submitted",
            "payload": {"gate_type": "code", "verdict": "APPROVED"},
        }
    )[0].role == "Security Auditor"


def test_code_review_to_done_is_noop() -> None:
    """Done is a terminal status with no auto-fire."""

    assert _route({"from_status": "Code Review", "to_status": "Done"}) == ()


def test_identity_transition_is_noop() -> None:
    assert _route({"from_status": "In Progress", "to_status": "In Progress"}) == ()
    assert _route({"from_status": "Code Review", "to_status": "Code Review"}) == ()


def test_none_to_in_progress_returns_developer_for_developer_task() -> None:
    assert _route({"from_status": None, "to_status": "In Progress"}) == (
        _developer_decision(),
    )


def test_case_insensitive_status_matching() -> None:
    expected = (_developer_decision(),)
    assert _route({"from_status": "Backlog", "to_status": "in progress"}) == expected
    assert _route({"from_status": "Backlog", "to_status": "In Progress"}) == expected
    assert _route({"from_status": "Backlog", "to_status": "IN PROGRESS"}) == expected


def test_canonical_statuses_are_case_insensitive() -> None:
    cr = RouteDecision(
        role="Code Reviewer",
        reason="status_to_code_review",
        concurrency_group="review",
        allow_parallel=False,
    )
    assert _route({"from_status": "In Progress", "to_status": "code review"}) == (cr,)
    assert _route({"from_status": "In Progress", "to_status": "CODE REVIEW"}) == (cr,)
    assert _route({"from_status": "In Progress", "to_status": "Code Review"}) == (cr,)


def test_space_and_underscore_variants_match() -> None:
    expected = (_developer_decision(),)
    assert _route({"from_status": "Backlog", "to_status": "in progress"}) == expected
    assert _route({"from_status": "Backlog", "to_status": "in_progress"}) == expected


def test_route_event_accepts_payload_task_and_review_context_signature() -> None:
    latest_task = {"id": 123, "agent": "Developer"}
    review_context = {"reviews": [], "review_requests": []}

    result = route_event(
        {"from_status": "Backlog", "to_status": "In Progress"},
        latest_task,
        review_context,
    )

    assert result[0].role == "Developer"


def test_full_spec_v23_pipeline_each_stage_fires_single_role() -> None:
    """End-to-end SPEC v23 sequence: each transition fires exactly one role."""

    pipeline = [
        ("Backlog", "In Progress", "Developer"),
        ("In Progress", "Code Review", "Code Reviewer"),
        ("Code Review", "Security Audit", "Security Auditor"),
        ("Security Audit", "QA", "QA Agent"),
    ]
    for from_status, to_status, expected_role in pipeline:
        decisions = _route({"from_status": from_status, "to_status": to_status})
        assert len(decisions) == 1, (
            f"{from_status} -> {to_status} fired {len(decisions)} roles, "
            f"SPEC v23 requires exactly one"
        )
        assert decisions[0].role == expected_role
        assert decisions[0].allow_parallel is False, (
            f"{to_status} must not allow parallel within stage (SRP-V16-ACTIVE-STAGE-INVARIANT)"
        )


# ----- Router v2 #10258: route by task.agent -----


def test_in_progress_routes_by_task_agent_architect() -> None:
    decisions = _route(
        {"from_status": "Backlog", "to_status": "In Progress"},
        task={"id": 5, "agent": "Architect"},
    )
    assert decisions == (
        RouteDecision(
            role="Architect",
            reason="status_to_in_progress",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )


def test_in_progress_routes_by_task_agent_qa() -> None:
    decisions = _route(
        {"from_status": "Backlog", "to_status": "In Progress"},
        task={"id": 6, "agent": "QA Agent"},
    )
    assert decisions == (
        RouteDecision(
            role="QA Agent",
            reason="status_to_in_progress",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_in_progress_routes_by_task_agent_security_auditor() -> None:
    decisions = _route(
        {"from_status": "Backlog", "to_status": "In Progress"},
        task={"id": 7, "agent": "Security Auditor"},
    )
    assert decisions == (
        RouteDecision(
            role="Security Auditor",
            reason="status_to_in_progress",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_in_progress_falls_back_to_task_type_when_agent_missing() -> None:
    decisions = _route(
        {"from_status": "Backlog", "to_status": "In Progress"},
        task={"id": 8, "agent": None, "task_type": "design"},
    )
    assert decisions == (
        RouteDecision(
            role="Architect",
            reason="status_to_in_progress",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )


def test_in_progress_task_type_security_falls_back_to_security_auditor() -> None:
    decisions = _route(
        {"from_status": "Backlog", "to_status": "In Progress"},
        task={"id": 9, "agent": "", "task_type": "security"},
    )
    assert decisions[0].role == "Security Auditor"


def test_in_progress_task_type_bug_falls_back_to_developer() -> None:
    decisions = _route(
        {"from_status": "Backlog", "to_status": "In Progress"},
        task={"id": 10, "agent": "", "task_type": "bug"},
    )
    assert decisions[0].role == "Developer"


def test_in_progress_fails_closed_when_neither_agent_nor_task_type_resolves() -> None:
    """SPEC v23 fail-closed: unknown agent + unknown task_type returns ()."""

    decisions = _route(
        {"from_status": "Backlog", "to_status": "In Progress"},
        task={"id": 11, "agent": None, "task_type": None},
    )
    assert decisions == ()


def test_in_progress_fails_closed_when_latest_task_is_empty() -> None:
    decisions = _route(
        {"from_status": "Backlog", "to_status": "In Progress"},
        task={},
    )
    assert decisions == ()


def test_resolve_role_for_task_kebab_case_agent() -> None:
    assert resolve_role_for_task({"agent": "code-reviewer"}) == "Code Reviewer"
    assert resolve_role_for_task({"agent": "qa-agent"}) == "QA Agent"
    assert resolve_role_for_task({"agent": "security-auditor"}) == "Security Auditor"


def test_resolve_role_for_task_mixed_case() -> None:
    assert resolve_role_for_task({"agent": "DEVELOPER"}) == "Developer"
    assert resolve_role_for_task({"agent": "Architect"}) == "Architect"


def test_resolve_role_for_task_unknown_agent_returns_none() -> None:
    assert resolve_role_for_task({"agent": "Unicorn"}) is None
    assert resolve_role_for_task({"agent": ""}) is None
    assert resolve_role_for_task({}) is None
    assert resolve_role_for_task(None) is None


def test_review_stage_routing_ignores_task_agent() -> None:
    """Code Review fires Code Reviewer regardless of who authored the task."""

    decisions = _route(
        {"from_status": "In Progress", "to_status": "Code Review"},
        task={"id": 12, "agent": "Architect"},
    )
    assert decisions[0].role == "Code Reviewer"


def test_fixing_routes_by_task_agent() -> None:
    """Fixing routes back to whoever was the implementor (not always Developer)."""

    decisions = _route(
        {"from_status": "Security Audit", "to_status": "Fixing"},
        task={"id": 13, "agent": "Architect"},
    )
    assert decisions == (
        RouteDecision(
            role="Architect",
            reason="status_to_fixing",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )

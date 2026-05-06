"""Tests for taskboard status-transition role routing."""

from agent.route_decision import RouteDecision
from agent.taskboard_status_router import route_event


def _developer_decision() -> RouteDecision:
    return RouteDecision(
        role="Developer",
        reason="status_to_in_progress",
        concurrency_group="implementation",
        allow_parallel=False,
    )


def test_backlog_to_in_progress_returns_developer() -> None:
    assert route_event({"from_status": "Backlog", "to_status": "In Progress"}, {}, {}) == (
        _developer_decision(),
    )


def test_in_progress_to_code_review_fires_only_code_reviewer() -> None:
    """SPEC v23 sequential: Code Review status fires CR alone (not CR + SA + QA)."""

    assert route_event({"from_status": "In Progress", "to_status": "Code Review"}, {}, {}) == (
        RouteDecision(
            role="Code Reviewer",
            reason="status_to_code_review",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_code_review_to_security_audit_fires_only_security_auditor() -> None:
    """SPEC v23 sequential: Security Audit status fires SA alone."""

    assert route_event(
        {"from_status": "Code Review", "to_status": "Security Audit"}, {}, {}
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

    assert route_event({"from_status": "Security Audit", "to_status": "QA"}, {}, {}) == (
        RouteDecision(
            role="QA Agent",
            reason="status_to_qa",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_qa_to_ready_to_merge_is_noop() -> None:
    """Ready to Merge has no auto-fired role yet (orchestrator/merger TBD)."""

    assert route_event({"from_status": "QA", "to_status": "Ready to Merge"}, {}, {}) == ()


def test_any_review_stage_to_fixing_fires_developer() -> None:
    """REQUEST_CHANGES at any review stage routes to Fixing -> Developer."""

    expected = (
        RouteDecision(
            role="Developer",
            reason="status_to_fixing",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )
    assert route_event({"from_status": "Code Review", "to_status": "Fixing"}, {}, {}) == expected
    assert (
        route_event({"from_status": "Security Audit", "to_status": "Fixing"}, {}, {}) == expected
    )
    assert route_event({"from_status": "QA", "to_status": "Fixing"}, {}, {}) == expected


def test_fixing_to_code_review_fires_code_reviewer() -> None:
    """After dev pushes fix, status flips back to Code Review and CR fires alone."""

    assert route_event({"from_status": "Fixing", "to_status": "Code Review"}, {}, {}) == (
        RouteDecision(
            role="Code Reviewer",
            reason="status_to_code_review",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_legacy_review_status_routes_to_code_reviewer_alone() -> None:
    """Backward compat: tasks still in legacy ``Review`` route to CR alone, not all three."""

    assert route_event({"from_status": "In Progress", "to_status": "Review"}, {}, {}) == (
        RouteDecision(
            role="Code Reviewer",
            reason="status_to_review_legacy_alias",
            concurrency_group="review",
            allow_parallel=False,
        ),
    )


def test_review_to_done_is_noop() -> None:
    assert route_event({"from_status": "Review", "to_status": "Done"}, {}, {}) == ()


def test_code_review_to_done_is_noop() -> None:
    """Done is a terminal status with no auto-fire."""

    assert route_event({"from_status": "Code Review", "to_status": "Done"}, {}, {}) == ()


def test_identity_transition_is_noop() -> None:
    assert route_event({"from_status": "In Progress", "to_status": "In Progress"}, {}, {}) == ()
    assert route_event({"from_status": "Code Review", "to_status": "Code Review"}, {}, {}) == ()


def test_none_to_in_progress_returns_developer() -> None:
    assert route_event({"from_status": None, "to_status": "In Progress"}, {}, {}) == (
        _developer_decision(),
    )


def test_case_insensitive_status_matching() -> None:
    expected = (_developer_decision(),)
    assert route_event({"from_status": "Backlog", "to_status": "in progress"}, {}, {}) == expected
    assert route_event({"from_status": "Backlog", "to_status": "In Progress"}, {}, {}) == expected
    assert route_event({"from_status": "Backlog", "to_status": "IN PROGRESS"}, {}, {}) == expected


def test_canonical_statuses_are_case_insensitive() -> None:
    cr = RouteDecision(
        role="Code Reviewer",
        reason="status_to_code_review",
        concurrency_group="review",
        allow_parallel=False,
    )
    assert route_event({"from_status": "In Progress", "to_status": "code review"}, {}, {}) == (cr,)
    assert route_event({"from_status": "In Progress", "to_status": "CODE REVIEW"}, {}, {}) == (cr,)
    assert route_event({"from_status": "In Progress", "to_status": "Code Review"}, {}, {}) == (cr,)


def test_space_and_underscore_variants_match() -> None:
    expected = (_developer_decision(),)
    assert route_event({"from_status": "Backlog", "to_status": "in progress"}, {}, {}) == expected
    assert route_event({"from_status": "Backlog", "to_status": "in_progress"}, {}, {}) == expected


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
        decisions = route_event({"from_status": from_status, "to_status": to_status}, {}, {})
        assert len(decisions) == 1, (
            f"{from_status} -> {to_status} fired {len(decisions)} roles, "
            f"SPEC v23 requires exactly one"
        )
        assert decisions[0].role == expected_role
        assert decisions[0].allow_parallel is False, (
            f"{to_status} must not allow parallel within stage (SRP-V16-ACTIVE-STAGE-INVARIANT)"
        )

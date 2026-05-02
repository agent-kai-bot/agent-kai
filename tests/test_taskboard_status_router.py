"""Tests for taskboard status-transition role routing."""

from agent.route_decision import RouteDecision
from agent.taskboard_status_router import route_event


def test_backlog_to_in_progress_returns_developer() -> None:
    assert route_event({"from_status": "Backlog", "to_status": "In Progress"}, {}, {}) == (
        RouteDecision(
            role="Developer",
            reason="status_to_in_progress",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )


def test_in_progress_to_review_returns_review_roles() -> None:
    assert route_event({"from_status": "In Progress", "to_status": "Review"}, {}, {}) == (
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
    )


def test_review_to_done_is_noop() -> None:
    assert route_event({"from_status": "Review", "to_status": "Done"}, {}, {}) == ()


def test_identity_transition_is_noop() -> None:
    assert route_event({"from_status": "In Progress", "to_status": "In Progress"}, {}, {}) == ()


def test_none_to_in_progress_returns_developer() -> None:
    assert route_event({"from_status": None, "to_status": "In Progress"}, {}, {}) == (
        RouteDecision(
            role="Developer",
            reason="status_to_in_progress",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )


def test_case_insensitive_status_matching() -> None:
    expected = (
        RouteDecision(
            role="Developer",
            reason="status_to_in_progress",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )
    assert route_event({"from_status": "Backlog", "to_status": "in progress"}, {}, {}) == expected
    assert route_event({"from_status": "Backlog", "to_status": "In Progress"}, {}, {}) == expected
    assert route_event({"from_status": "Backlog", "to_status": "IN PROGRESS"}, {}, {}) == expected


def test_space_and_underscore_variants_match() -> None:
    expected = (
        RouteDecision(
            role="Developer",
            reason="status_to_in_progress",
            concurrency_group="implementation",
            allow_parallel=False,
        ),
    )
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

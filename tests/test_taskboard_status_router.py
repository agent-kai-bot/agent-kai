"""Tests for taskboard status-transition role routing."""

from agent.taskboard_status_router import roles_to_fire


def test_backlog_to_in_progress_returns_developer() -> None:
    assert roles_to_fire("Backlog", "In Progress") == ("Developer",)


def test_in_progress_to_review_returns_review_roles() -> None:
    assert roles_to_fire("In Progress", "Review") == (
        "Code Reviewer",
        "Security Auditor",
        "QA Agent",
    )


def test_review_to_done_is_noop() -> None:
    assert roles_to_fire("Review", "Done") == ()


def test_identity_transition_is_noop() -> None:
    assert roles_to_fire("In Progress", "In Progress") == ()


def test_none_to_in_progress_returns_developer() -> None:
    assert roles_to_fire(None, "In Progress") == ("Developer",)


def test_case_insensitive_status_matching() -> None:
    assert roles_to_fire("Backlog", "in progress") == ("Developer",)
    assert roles_to_fire("Backlog", "In Progress") == ("Developer",)
    assert roles_to_fire("Backlog", "IN PROGRESS") == ("Developer",)


def test_space_and_underscore_variants_match() -> None:
    assert roles_to_fire("Backlog", "in progress") == ("Developer",)
    assert roles_to_fire("Backlog", "in_progress") == ("Developer",)

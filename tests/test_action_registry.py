from __future__ import annotations

import pytest

from daemon.signal_router import ActionDescriptor
from daemon.signal_router.actions import EXECUTORS, get_executor, validate_action


def test_action_registry_has_phase_3_action_kinds_only() -> None:
    assert sorted(EXECUTORS) == [
        "alert",
        "ignore",
        "inject_session",
        "log",
        "notify",
        "trade",
        "ui_panel",
    ]


def test_unknown_action_kind_rejected_with_clear_error() -> None:
    with pytest.raises(KeyError, match="unknown signal_router action kind"):
        get_executor("spawn_agent")

    errors = validate_action(ActionDescriptor(kind="spawn_agent", target=None, params={}))
    assert errors[0].field == "kind"
    assert "unknown signal_router action kind" in errors[0].message

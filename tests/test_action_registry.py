from __future__ import annotations

import pytest

from daemon.signal_router import ActionDescriptor
from daemon.signal_router.actions import EXECUTORS, get_executor, validate_action


def test_action_registry_has_phase_4_action_kinds() -> None:
    assert sorted(EXECUTORS) == [
        "alert",
        "ignore",
        "inject_session",
        "log",
        "notify",
        "spawn_agent",
        "trade",
        "ui_panel",
    ]


def test_unknown_action_kind_rejected_with_clear_error() -> None:
    with pytest.raises(KeyError, match="unknown signal_router action kind"):
        get_executor("missing")

    errors = validate_action(ActionDescriptor(kind="missing", target=None, params={}))
    assert errors[0].field == "kind"
    assert "unknown signal_router action kind" in errors[0].message

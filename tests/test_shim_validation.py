from __future__ import annotations

import pytest

from daemon.signal_router.shim_signal_handlers import (
    ShimValidationError,
    raise_for_startup_errors,
    translate_signal_handlers_config,
)


def _config(handler: dict) -> dict:
    return {"agents": {"analyst": {}}, "signal_handlers": [handler]}


def test_unknown_action_kind_is_startup_error_in_shadow() -> None:
    _, errors = translate_signal_handlers_config(
        _config({"name": "bad", "action": "does_not_exist"}),
        mode="shadow",
    )

    assert len(errors) == 1
    assert errors[0].mode == "shadow"
    assert errors[0].severity == "error"
    with pytest.raises(ShimValidationError):
        raise_for_startup_errors(errors, "shadow")


def test_missing_required_dispatch_agent_field_is_error() -> None:
    _, errors = translate_signal_handlers_config(
        _config(
            {
                "name": "missing-agent",
                "action": "dispatch_agent",
                "agent": "",
                "task_template": "review",
            }
        ),
        mode="new",
    )

    assert any("missing required field 'agent'" in error.message for error in errors)
    with pytest.raises(ShimValidationError):
        raise_for_startup_errors(errors, "new")


def test_malformed_match_is_error() -> None:
    _, errors = translate_signal_handlers_config(
        _config(
            {
                "name": "bad-match",
                "action": "chat_message",
                "match": ["not", "a", "dict"],
                "template": "x",
            }
        ),
        mode="shadow",
    )

    assert any(error.message == "match must be a dict" for error in errors)


def test_disabled_malformed_handler_in_legacy_is_warning_only() -> None:
    _, errors = translate_signal_handlers_config(
        _config({"name": "disabled", "enabled": False, "action": "does_not_exist"}),
        mode="legacy",
    )

    assert errors
    assert all(error.severity == "warning" for error in errors)
    raise_for_startup_errors(errors, "legacy")


def test_disabled_malformed_handler_in_shadow_does_not_fail_startup() -> None:
    _, errors = translate_signal_handlers_config(
        _config({"name": "disabled", "enabled": False, "action": "does_not_exist"}),
        mode="shadow",
    )

    assert errors
    assert all(error.severity == "warning" for error in errors)
    raise_for_startup_errors(errors, "shadow")

from __future__ import annotations

from unittest import mock

import pytest

from daemon.server import DaemonServer
from daemon.signal_router.shim_signal_handlers import ShimValidationError


def _daemon_config(tmp_path, mode: str, handler: dict) -> dict:
    return {
        "agents": {"analyst": {}},
        "signal_handlers": [handler],
        "daemon": {
            "signal_router": {
                "mode": mode,
                "channels": {},
                "routes": [],
                "dedup_table_path": str(tmp_path / f"{mode}.sqlite3"),
            },
            "heartbeat": {"enabled": False},
        },
    }


def test_daemon_boots_legacy_with_malformed_handler_and_logs_warning(tmp_path) -> None:
    config = _daemon_config(
        tmp_path,
        "legacy",
        {"name": "bad", "action": "dispatch_agent", "agent": ""},
    )

    with mock.patch("daemon.server.get_agent_config", return_value=config), mock.patch(
        "daemon.server.log_shim_errors"
    ) as log_errors:
        server = DaemonServer()

    assert server.signal_router.mode.value == "legacy"
    logged_errors = log_errors.call_args.args[0]
    assert logged_errors[0].severity == "warning"
    assert "missing required field 'agent'" in logged_errors[0].message


def test_daemon_shadow_mode_fails_with_malformed_enabled_handler(tmp_path) -> None:
    config = _daemon_config(
        tmp_path,
        "shadow",
        {"name": "bad", "action": "dispatch_agent", "agent": ""},
    )

    with mock.patch("daemon.server.get_agent_config", return_value=config):
        with pytest.raises(ShimValidationError):
            DaemonServer()


def test_daemon_new_mode_fails_with_malformed_enabled_handler(tmp_path) -> None:
    config = _daemon_config(
        tmp_path,
        "new",
        {"name": "bad", "action": "dispatch_agent", "agent": ""},
    )

    with mock.patch("daemon.server.get_agent_config", return_value=config):
        with pytest.raises(ShimValidationError):
            DaemonServer()

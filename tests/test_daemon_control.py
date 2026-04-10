"""Tests for daemon process management helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from daemon.control import (
    build_daemon_command,
    clear_daemon_pid,
    read_daemon_pid,
    start_local_daemon,
)


class _FakePopen:
    """Detached process stub used to validate start-up flow."""

    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self._exit_code = None

    def poll(self):
        return self._exit_code

    def wait(self, timeout=None):  # noqa: D401, ANN001
        self._exit_code = 0
        return self._exit_code


class DaemonControlTests(unittest.TestCase):
    """Validate daemon command construction and auto-spawn behavior."""

    def test_build_daemon_command_includes_required_flags(self):
        command = build_daemon_command(
            agent_name="kai",
            nats_url="nats://unit-test",
            log_level="DEBUG",
            python_executable="/tmp/python",
            entrypoint="/repo/main.py",
        )

        self.assertEqual(
            command,
            [
                "/tmp/python",
                "/repo/main.py",
                "--daemon",
                "--name",
                "kai",
                "--nats-url",
                "nats://unit-test",
                "--log-level",
                "DEBUG",
            ],
        )

    @mock.patch("daemon.control.subprocess.Popen")
    @mock.patch("daemon.control.daemon_healthcheck")
    def test_start_local_daemon_reuses_healthy_instance(self, healthcheck, popen):
        healthcheck.return_value = {"status": "ok"}

        result = start_local_daemon(
            agent_name="kai",
            nats_url="nats://unit-test",
        )

        self.assertTrue(result.already_running)
        popen.assert_not_called()

    @mock.patch("daemon.control.wait_for_daemon_health")
    @mock.patch("daemon.control.subprocess.Popen")
    @mock.patch("daemon.control.daemon_healthcheck")
    def test_start_local_daemon_spawns_process_and_writes_pid(
        self,
        healthcheck,
        popen,
        wait_for_health,
    ):
        healthcheck.return_value = None
        popen.return_value = _FakePopen(pid=2468)

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            pid_path = base_dir / "kaid.pid"
            log_path = base_dir / "kaid.log"

            result = start_local_daemon(
                agent_name="kai",
                nats_url="nats://unit-test",
                pid_path=pid_path,
                log_path=log_path,
                remote_url="ws://127.0.0.1:8765/ws",
                health_url="http://127.0.0.1:8765/api/health",
            )

            self.assertFalse(result.already_running)
            self.assertEqual(result.pid, 2468)
            self.assertEqual(read_daemon_pid(pid_path), 2468)
            wait_for_health.assert_called_once()

            clear_daemon_pid(pid_path)
            self.assertIsNone(read_daemon_pid(pid_path))

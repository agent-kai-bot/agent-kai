"""Tests for daemon and remote CLI flag semantics."""

from __future__ import annotations

import unittest
from unittest import mock

from main import (
    _resolve_terminal_session_name,
    _run_local_terminal,
    _run_remote_terminal,
    _run_terminal_mode,
    build_parser,
    validate_args,
)


class _FakeTerminal:
    """Minimal async terminal stub for session-switch loop tests."""

    results: list[dict | None] = []
    seen_sessions: list[str] = []

    def __init__(self, session, bus=None):
        self.session = session
        self.bus = bus

    async def run_async(self):
        type(self).seen_sessions.append(self.session.name)
        return type(self).results.pop(0)


class _FakeRemoteSession:
    """Remote session stub that records connect/close flow."""

    instances: list["_FakeRemoteSession"] = []

    def __init__(self, remote_url: str, *, session_name: str):
        self.remote_url = remote_url
        self.name = session_name
        self.connected = False
        self.closed = False
        type(self).instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True


class _FakeManager:
    """Sub-agent manager stub for local terminal loop tests."""

    def __init__(self) -> None:
        self.stopped = False

    async def stop_all(self) -> None:
        self.stopped = True


class _FakeLocalSession:
    """Local session stub that records touch/attach lifecycle."""

    instances: list["_FakeLocalSession"] = []

    def __init__(self, name: str):
        self.name = name
        self.touch_index_calls = 0
        self.attach_calls: list[tuple[object, str]] = []
        self.sub_agent_manager = _FakeManager()
        type(self).instances.append(self)

    def touch_index(self) -> None:
        self.touch_index_calls += 1

    def attach_runtime(self, *, bus=None, agent_name="kai"):
        self.attach_calls.append((bus, agent_name))
        return object()


class MainCliTests(unittest.TestCase):
    """Validate the new daemon/remote/standalone flag semantics."""

    def test_remote_requires_terminal(self):
        parser = build_parser()
        args = parser.parse_args(["--remote", "ws://127.0.0.1:8765"])

        with self.assertRaises(SystemExit):
            validate_args(parser, args)

    def test_remote_and_standalone_conflict(self):
        parser = build_parser()
        args = parser.parse_args(
            ["--terminal", "--remote", "ws://127.0.0.1:8765", "--standalone"]
        )

        with self.assertRaises(SystemExit):
            validate_args(parser, args)

    def test_daemon_flag_is_valid_on_its_own(self):
        parser = build_parser()
        args = parser.parse_args(["--daemon"])

        validate_args(parser, args)
        self.assertTrue(args.daemon)
        self.assertFalse(args.terminal)

    def test_terminal_remote_combination_is_valid(self):
        parser = build_parser()
        args = parser.parse_args(["--terminal", "--remote", "ws://example.com:9999"])

        validate_args(parser, args)
        self.assertTrue(args.terminal)
        self.assertEqual(args.remote, "ws://example.com:9999")

    def test_session_requires_terminal(self):
        parser = build_parser()
        args = parser.parse_args(["--session", "btc-scalper"])

        with self.assertRaises(SystemExit):
            validate_args(parser, args)

    def test_terminal_session_is_valid_and_resolved(self):
        parser = build_parser()
        args = parser.parse_args(["--terminal", "--session", "btc-scalper"])

        validate_args(parser, args)
        self.assertEqual(_resolve_terminal_session_name(args), "btc-scalper")

    def test_terminal_session_defaults_to_terminal(self):
        parser = build_parser()
        args = parser.parse_args(["--terminal"])

        validate_args(parser, args)
        self.assertEqual(_resolve_terminal_session_name(args), "terminal")


class MainTerminalLoopTests(unittest.IsolatedAsyncioTestCase):
    """Validate session switching in the terminal launch loops."""

    def setUp(self) -> None:
        _FakeTerminal.results = []
        _FakeTerminal.seen_sessions = []
        _FakeRemoteSession.instances = []
        _FakeLocalSession.instances = []

    async def test_run_remote_terminal_reconnects_after_switch(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "--terminal",
                "--remote",
                "ws://127.0.0.1:8765",
                "--session",
                "alpha",
            ]
        )
        _FakeTerminal.results = [
            {"action": "switch_session", "session": "beta"},
            None,
        ]

        with mock.patch("main.RemoteSession", _FakeRemoteSession), mock.patch(
            "tui.terminal.TradingTerminal", _FakeTerminal
        ):
            await _run_remote_terminal(args)

        self.assertEqual(_FakeTerminal.seen_sessions, ["alpha", "beta"])
        self.assertEqual(
            [session.name for session in _FakeRemoteSession.instances],
            ["alpha", "beta"],
        )
        self.assertTrue(all(session.connected for session in _FakeRemoteSession.instances))
        self.assertTrue(all(session.closed for session in _FakeRemoteSession.instances))

    async def test_run_local_terminal_recreates_session_after_switch(self):
        parser = build_parser()
        args = parser.parse_args(["--terminal", "--session", "alpha"])
        _FakeTerminal.results = [
            {"action": "switch_session", "session": "beta"},
            None,
        ]
        bus = object()

        with mock.patch("main.Session", _FakeLocalSession), mock.patch(
            "tui.terminal.TradingTerminal", _FakeTerminal
        ):
            await _run_local_terminal(args, bus)

        self.assertEqual(_FakeTerminal.seen_sessions, ["alpha", "beta"])
        self.assertEqual(
            [session.name for session in _FakeLocalSession.instances],
            ["alpha", "beta"],
        )
        self.assertTrue(
            all(session.touch_index_calls == 1 for session in _FakeLocalSession.instances)
        )
        self.assertEqual(
            _FakeLocalSession.instances[0].attach_calls,
            [(bus, args.name)],
        )
        self.assertTrue(
            all(session.sub_agent_manager.stopped for session in _FakeLocalSession.instances)
        )


class MainTerminalModeTests(unittest.IsolatedAsyncioTestCase):
    """Validate the Phase 4 daemon-default terminal routing."""

    async def test_terminal_mode_prefers_daemon_when_available(self):
        parser = build_parser()
        args = parser.parse_args(["--terminal", "--session", "alpha"])

        with mock.patch(
            "main._ensure_local_daemon",
            mock.AsyncMock(return_value="ws://127.0.0.1:8765/ws"),
        ) as ensure_daemon, mock.patch(
            "main._run_remote_terminal",
            mock.AsyncMock(),
        ) as run_remote, mock.patch(
            "main._connect_bus",
            mock.AsyncMock(),
        ) as connect_bus, mock.patch(
            "main._run_local_terminal",
            mock.AsyncMock(),
        ) as run_local:
            await _run_terminal_mode(args)

        ensure_daemon.assert_awaited_once_with(args)
        connect_bus.assert_not_awaited()
        run_local.assert_not_awaited()
        run_remote.assert_awaited_once()
        remote_args = run_remote.await_args.args[0]
        self.assertEqual(remote_args.remote, "ws://127.0.0.1:8765/ws")
        self.assertEqual(remote_args.session, "alpha")

    async def test_terminal_mode_falls_back_to_standalone_when_daemon_fails(self):
        parser = build_parser()
        args = parser.parse_args(["--terminal", "--session", "alpha"])
        bus = mock.Mock()
        bus.disconnect = mock.AsyncMock()

        with mock.patch(
            "main._ensure_local_daemon",
            mock.AsyncMock(return_value=None),
        ) as ensure_daemon, mock.patch(
            "main._run_remote_terminal",
            mock.AsyncMock(),
        ) as run_remote, mock.patch(
            "main._connect_bus",
            mock.AsyncMock(return_value=bus),
        ) as connect_bus, mock.patch(
            "main._run_local_terminal",
            mock.AsyncMock(),
        ) as run_local:
            await _run_terminal_mode(args)

        ensure_daemon.assert_awaited_once_with(args)
        connect_bus.assert_awaited_once_with(args)
        run_local.assert_awaited_once_with(args, bus)
        bus.disconnect.assert_awaited_once_with()
        run_remote.assert_not_awaited()

    async def test_terminal_mode_respects_explicit_standalone(self):
        parser = build_parser()
        args = parser.parse_args(["--terminal", "--standalone"])
        bus = mock.Mock()
        bus.disconnect = mock.AsyncMock()

        with mock.patch("main._ensure_local_daemon", mock.AsyncMock()) as ensure_daemon, mock.patch(
            "main._run_remote_terminal",
            mock.AsyncMock(),
        ) as run_remote, mock.patch(
            "main._connect_bus",
            mock.AsyncMock(return_value=bus),
        ) as connect_bus, mock.patch(
            "main._run_local_terminal",
            mock.AsyncMock(),
        ) as run_local:
            await _run_terminal_mode(args)

        ensure_daemon.assert_not_awaited()
        connect_bus.assert_awaited_once_with(args)
        run_local.assert_awaited_once_with(args, bus)
        bus.disconnect.assert_awaited_once_with()
        run_remote.assert_not_awaited()

    async def test_terminal_mode_keeps_explicit_remote_flow(self):
        parser = build_parser()
        args = parser.parse_args(["--terminal", "--remote", "ws://example.com/ws"])

        with mock.patch("main._ensure_local_daemon", mock.AsyncMock()) as ensure_daemon, mock.patch(
            "main._run_remote_terminal",
            mock.AsyncMock(),
        ) as run_remote, mock.patch(
            "main._connect_bus",
            mock.AsyncMock(),
        ) as connect_bus:
            await _run_terminal_mode(args)

        ensure_daemon.assert_not_awaited()
        connect_bus.assert_not_awaited()
        run_remote.assert_awaited_once_with(args)


if __name__ == "__main__":
    unittest.main()

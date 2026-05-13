from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from daemon.server import DaemonServer


class _FakeResolver:
    def __init__(self) -> None:
        self.startup_diagnostics_logged = 0

    def log_startup_diagnostics(self) -> None:
        self.startup_diagnostics_logged += 1


class DaemonServerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_config_resolver_constructed_once_and_logs_startup(self) -> None:
        resolver = _FakeResolver()
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "daemon.server.RuntimeConfigResolver",
            return_value=resolver,
        ) as resolver_cls:
            server = DaemonServer(
                agent_name="kai",
                nats_url="nats://unit-test",
                bus_factory=None,
                db_path=Path(tmpdir) / "daemon.sqlite3",
                taskboard_dispatcher_enabled=False,
            )

            await server.startup()
            try:
                self.assertIs(server.runtime_config_resolver, resolver)
                self.assertEqual(resolver_cls.call_count, 1)
                self.assertEqual(resolver.startup_diagnostics_logged, 1)
            finally:
                await server.shutdown()


if __name__ == "__main__":
    unittest.main()

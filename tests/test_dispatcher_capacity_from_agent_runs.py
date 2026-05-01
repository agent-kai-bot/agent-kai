"""Tests for Phase 0 of epic #10030 (#10247): capacity from agent_runs ledger."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agent.taskboard_dispatcher import _TaskboardQueueStore, _utc_now


class _StubAgentRunsClient:
    """Minimal stub mirroring AgentRunsClient.list_by_status."""

    def __init__(self, *, enabled: bool = True, by_status: dict | None = None) -> None:
        self.enabled = enabled
        self._by_status = by_status or {}
        self.calls: list[str] = []

    def list_by_status(self, status: str, limit: int = 200):
        self.calls.append(status)
        rows = self._by_status.get(status, [])
        return list(rows) if rows is not None else None


class ActiveRunCountFromLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "daemon-state.sqlite3"
        self.store = _TaskboardQueueStore(self.db, clock=_utc_now)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_returns_none_when_client_disabled(self) -> None:
        client = _StubAgentRunsClient(enabled=False)
        self.assertIsNone(self.store.active_run_count_from_ledger(agent_runs_client=client))

    def test_sums_across_active_statuses(self) -> None:
        client = _StubAgentRunsClient(
            by_status={
                "queued": [{"id": 1}, {"id": 2}],
                "dispatching": [{"id": 3}],
                "spawning": [],
                "running": [{"id": 4}, {"id": 5}, {"id": 6}],
            }
        )
        # Bypass cache for this test
        self.store._ledger_capacity_cache = None
        count = self.store.active_run_count_from_ledger(agent_runs_client=client)
        self.assertEqual(count, 6)
        self.assertEqual(set(client.calls), {"queued", "dispatching", "spawning", "running"})

    def test_returns_none_when_all_queries_fail(self) -> None:
        client = _StubAgentRunsClient(
            by_status={"queued": None, "dispatching": None, "spawning": None, "running": None}
        )
        self.store._ledger_capacity_cache = None
        self.assertIsNone(self.store.active_run_count_from_ledger(agent_runs_client=client))

    def test_caches_for_5_seconds(self) -> None:
        client = _StubAgentRunsClient(by_status={"queued": [{"id": 1}]})
        self.store._ledger_capacity_cache = None
        first = self.store.active_run_count_from_ledger(agent_runs_client=client)
        first_calls = list(client.calls)
        # Second call within TTL — should hit cache, not re-query.
        second = self.store.active_run_count_from_ledger(agent_runs_client=client)
        self.assertEqual(first, second)
        self.assertEqual(client.calls, first_calls, "second call should be cached")

    def test_partial_failure_returns_none_for_safe_fallback(self) -> None:
        """Codex CR fix: a partial failure (running 5xxs but queued returns)
        must return None so the caller falls back to the conservative local
        sessions count. Returning a partial sum lets the dispatcher
        oversubscribe under a partial taskboard outage.
        """
        client = _StubAgentRunsClient(
            by_status={
                "queued": [{"id": 1}, {"id": 2}],
                "dispatching": None,
                "spawning": [],
                "running": None,
            }
        )
        self.store._ledger_capacity_cache = None
        self.assertIsNone(
            self.store.active_run_count_from_ledger(agent_runs_client=client)
        )

    def test_invalidate_capacity_cache_forces_refetch(self) -> None:
        """Codex CR fix: after a spawn, invalidate_capacity_cache() must
        force the next read back to the API so we don't burn through the
        whole queue in one batch on a stale low count.
        """
        client = _StubAgentRunsClient(
            by_status={
                "queued": [{"id": 1}],
                "dispatching": [],
                "spawning": [],
                "running": [],
            }
        )
        self.store._ledger_capacity_cache = None
        first = self.store.active_run_count_from_ledger(agent_runs_client=client)
        self.assertEqual(first, 1)
        first_calls = list(client.calls)

        self.store.invalidate_capacity_cache()

        # Add a row to simulate the freshly-spawned session showing up.
        client._by_status["spawning"] = [{"id": 99}]
        second = self.store.active_run_count_from_ledger(agent_runs_client=client)
        self.assertEqual(second, 2, "post-invalidate read must hit the API again")
        self.assertGreater(len(client.calls), len(first_calls))


if __name__ == "__main__":
    unittest.main()

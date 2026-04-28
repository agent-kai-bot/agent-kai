"""Tests for the taskboard auto-fire dispatcher."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent.taskboard_dispatcher import (
    BACKPRESSURE_SUBJECT,
    DISPATCHER_SOURCE,
    TaskboardDispatcher,
    resolve_taskboard_role,
)


NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


class _FakeTaskClient:
    """In-memory taskboard client for dispatcher tests."""

    def __init__(self, tasks: dict[int, dict]) -> None:
        self.tasks = tasks
        self.fetches: list[int] = []

    async def fetch_task(self, task_id: int) -> dict:
        """Return the configured task for ``task_id``."""

        self.fetches.append(task_id)
        return dict(self.tasks[task_id])


class _FakeSessionManager:
    """Record spawn and abort calls without starting agents."""

    def __init__(self) -> None:
        self.spawn_calls: list[dict] = []
        self.abort_calls: list[str] = []

    async def spawn(self, **kwargs):
        """Record spawn arguments and return the requested session id."""

        self.spawn_calls.append(kwargs)
        return kwargs["session_id"]

    async def abort(self, session_id: str) -> None:
        """Record an abort request."""

        self.abort_calls.append(session_id)


class _FakeBus:
    """Record NATS publish calls."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: dict) -> None:
        """Record the publish request."""

        self.published.append((subject, payload))


class TaskboardDispatcherTests(unittest.IsolatedAsyncioTestCase):
    """Validate taskboard dispatcher behavior."""

    def setUp(self) -> None:
        """Create an isolated SQLite database for each test."""

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "daemon-state.sqlite3"
        self._create_pending_table()

    def tearDown(self) -> None:
        """Clean up the isolated database."""

        self.temp_dir.cleanup()

    async def test_happy_path_spawns_and_marks_row_processed(self) -> None:
        """A pending row spawns once and records session metadata."""

        self._create_sessions_table_without_fire_generation()
        row_id = self._insert_pending(10152, 7, "Developer")
        task = {
            "id": 10152,
            "title": "Build dispatcher",
            "agent": "Developer",
            "fire_generation": 7,
        }
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={10152: task},
            session_manager=session_manager,
        )

        with mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered prompt",
        ), self.assertLogs("agent.taskboard_dispatcher", level="INFO") as logs:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        self.assertEqual(len(session_manager.spawn_calls), 1)
        spawn = session_manager.spawn_calls[0]
        self.assertEqual(spawn["role"], "Developer")
        self.assertEqual(spawn["agent_id"], "developer")
        self.assertEqual(spawn["model"], "codex")
        self.assertEqual(spawn["profile"], "xhigh")
        self.assertEqual(spawn["prompt"], "rendered prompt")
        self.assertIn(
            "taskboard_fire_spawned task_id=10152 fire_generation=7 "
            "role=Developer session_id=taskboard-10152-7-developer",
            "\n".join(logs.output),
        )
        row = self._pending_row(row_id)
        self.assertEqual(row["dispatch_status"], "spawned")
        self.assertEqual(row["session_id"], "taskboard-10152-7-developer")
        self.assertIsNotNone(row["processed_at"])
        session = self._session_row()
        self.assertEqual(session["taskboard_task_id"], 10152)
        self.assertEqual(session["fire_generation"], 7)
        self.assertEqual(session["agent_id"], "developer")
        self.assertIn("fire_generation", self._session_columns())

    async def test_dedup_marks_second_row_duplicate(self) -> None:
        """Two rows with the same task/fire/agent key spawn only once."""

        self._insert_pending(200, 3, "Developer")
        self._insert_pending(200, 3, "Developer")
        task = {"id": 200, "agent": "Developer", "fire_generation": 3}
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={200: task},
            session_manager=session_manager,
        )

        counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1, "duplicate": 1})
        self.assertEqual(len(session_manager.spawn_calls), 1)
        self.assertEqual(self._pending_statuses(), ["spawned", "duplicate"])

    async def test_superseded_generation_is_skipped(self) -> None:
        """A stale payload is marked superseded after re-fetch."""

        self._insert_pending(300, 1, "Developer")
        task = {"id": 300, "agent": "Developer", "fire_generation": 2}
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={300: task},
            session_manager=session_manager,
        )

        counts = await dispatcher.run_once()

        self.assertEqual(counts, {"superseded": 1})
        self.assertEqual(session_manager.spawn_calls, [])
        self.assertEqual(self._pending_statuses(), ["superseded"])

    async def test_unknown_role_is_rejected_without_spawn(self) -> None:
        """Unknown taskboard roles are marked and logged."""

        self._insert_pending(400, 1, "BogusRole")
        task = {"id": 400, "agent": "BogusRole", "fire_generation": 1}
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={400: task},
            session_manager=session_manager,
        )

        with self.assertLogs("agent.taskboard_dispatcher", level="WARNING") as logs:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"unknown_role": 1})
        self.assertEqual(session_manager.spawn_calls, [])
        self.assertEqual(self._pending_statuses(), ["unknown_role"])
        self.assertIn("taskboard_fire_unknown_role task_id=400", "\n".join(logs.output))

    async def test_backpressure_leaves_overflow_rows_pending(self) -> None:
        """The dispatcher respects the active spawn cap and drains later."""

        tasks = {}
        for offset in range(12):
            task_id = 500 + offset
            self._insert_pending(task_id, 1, "Developer")
            tasks[task_id] = {
                "id": task_id,
                "agent": "Developer",
                "fire_generation": 1,
            }
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks=tasks,
            session_manager=session_manager,
            max_concurrent_spawns=6,
        )

        first_counts = await dispatcher.run_once()
        self.assertEqual(first_counts, {"spawned": 6})
        self.assertEqual(len(session_manager.spawn_calls), 6)
        self.assertEqual(self._pending_count(), 6)

        with self._connect() as conn:
            conn.execute("UPDATE sessions SET status = 'completed'")
        second_counts = await dispatcher.run_once()

        self.assertEqual(second_counts, {"spawned": 6})
        self.assertEqual(len(session_manager.spawn_calls), 12)
        self.assertEqual(self._pending_count(), 0)

    async def test_backpressure_alarm_publishes_nats_alert(self) -> None:
        """Stale pending depth above threshold emits a best-effort alarm."""

        old_received = self._iso(NOW - timedelta(seconds=90))
        for offset in range(11):
            self._insert_pending(600 + offset, 1, "Developer", received_at=old_received)
        bus = _FakeBus()
        dispatcher = self._dispatcher(
            tasks={},
            session_manager=_FakeSessionManager(),
            nats_bus=bus,
            max_concurrent_spawns=0,
        )

        with self.assertLogs("agent.taskboard_dispatcher", level="WARNING") as logs:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {})
        self.assertEqual(len(bus.published), 1)
        subject, payload = bus.published[0]
        self.assertEqual(subject, BACKPRESSURE_SUBJECT)
        self.assertEqual(payload["depth"], 11)
        self.assertIn(
            "taskboard_dispatcher_backpressure_alarm depth=11 threshold=10",
            "\n".join(logs.output),
        )

    async def test_stuck_session_sweep_aborts_and_marks_row(self) -> None:
        """Sessions older than the stuck threshold are aborted."""

        row_id = self._insert_pending(700, 2, "Developer")
        self._create_full_sessions_table()
        stale_created_at = self._iso(NOW - timedelta(minutes=61))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE webhook_pending
                SET processed_at = ?, dispatch_status = ?, session_id = ?
                WHERE id = ?
                """,
                (self._iso(NOW), "spawned", "session-stuck", row_id),
            )
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, taskboard_task_id, fire_generation, agent_id,
                    source, status, webhook_pending_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "session-stuck",
                    700,
                    2,
                    "developer",
                    DISPATCHER_SOURCE,
                    "running",
                    str(row_id),
                    stale_created_at,
                    stale_created_at,
                ),
            )
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            session_manager=session_manager,
        )

        count = await dispatcher.sweep_stuck_sessions()

        self.assertEqual(count, 1)
        self.assertEqual(session_manager.abort_calls, ["session-stuck"])
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "stuck_aborted")
        self.assertEqual(self._session_row()["status"], "aborted")

    async def test_renderer_integration_passes_prompt_verbatim(self) -> None:
        """The renderer output is the exact prompt sent to spawn."""

        self._insert_pending(800, 4, "QA Agent")
        task = {"id": 800, "agent": "QA Agent", "fire_generation": 4}
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={800: task},
            session_manager=session_manager,
        )

        with mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="known prompt body",
        ) as renderer:
            await dispatcher.run_once()

        renderer.assert_called_once_with("QA Agent", task)
        self.assertEqual(session_manager.spawn_calls[0]["prompt"], "known prompt body")

    async def test_tier_mapping_table(self) -> None:
        """Every supported taskboard role maps to the required model tier."""

        expected = {
            "Architect": ("architect", "codex", "xhigh"),
            "Developer": ("developer", "codex", "xhigh"),
            "Code Reviewer": ("code-reviewer", "claude", "high"),
            "Security Auditor": ("security-auditor", "claude", "high"),
            "QA Agent": ("qa-agent", "claude", "high"),
        }
        for role, expected_tuple in expected.items():
            route = resolve_taskboard_role(role)
            self.assertEqual(
                (route.agent_id, route.model, route.profile),
                expected_tuple,
            )

    def _dispatcher(
        self,
        *,
        tasks: dict[int, dict],
        session_manager: _FakeSessionManager,
        nats_bus=None,
        max_concurrent_spawns: int = 6,
    ) -> TaskboardDispatcher:
        return TaskboardDispatcher(
            db_path=self.db_path,
            task_client=_FakeTaskClient(tasks),
            session_manager=session_manager,
            nats_bus=nats_bus,
            max_concurrent_spawns=max_concurrent_spawns,
            clock=lambda: NOW,
        )

    def _create_pending_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE webhook_pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    processed_at TEXT,
                    dispatch_status TEXT,
                    session_id TEXT,
                    last_error TEXT
                )
                """
            )

    def _create_sessions_table_without_fire_generation(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE sessions (
                    session_id TEXT UNIQUE,
                    taskboard_task_id INTEGER,
                    agent_id TEXT,
                    source TEXT,
                    status TEXT,
                    webhook_pending_id TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    aborted_at TEXT
                )
                """
            )

    def _create_full_sessions_table(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT UNIQUE,
                    taskboard_task_id INTEGER,
                    fire_generation INTEGER,
                    agent_id TEXT,
                    source TEXT,
                    status TEXT,
                    webhook_pending_id TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    aborted_at TEXT
                )
                """
            )

    def _insert_pending(
        self,
        task_id: int,
        fire_generation: int,
        agent: str,
        *,
        received_at: str | None = None,
    ) -> int:
        payload = {
            "task_id": task_id,
            "fire_generation": fire_generation,
            "task": {
                "id": task_id,
                "agent": agent,
                "fire_generation": fire_generation,
            },
        }
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO webhook_pending(received_at, payload)
                VALUES (?, ?)
                """,
                (received_at or self._iso(NOW), json.dumps(payload)),
            )
            return int(cursor.lastrowid)

    def _pending_row(self, row_id: int) -> sqlite3.Row:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM webhook_pending WHERE id = ?",
                (row_id,),
            ).fetchone()

    def _pending_statuses(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT dispatch_status FROM webhook_pending ORDER BY id"
            ).fetchall()
            return [str(row["dispatch_status"]) for row in rows]

    def _pending_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM webhook_pending WHERE processed_at IS NULL"
            ).fetchone()
            return int(row["count"])

    def _session_row(self) -> sqlite3.Row:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM sessions LIMIT 1").fetchone()

    def _session_columns(self) -> set[str]:
        with self._connect() as conn:
            return {str(row["name"]) for row in conn.execute("PRAGMA table_info(sessions)")}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00",
            "Z",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

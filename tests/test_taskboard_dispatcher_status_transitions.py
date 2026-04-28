"""Status-transition integration tests for the taskboard dispatcher."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agent.taskboard_dispatcher import TaskboardDispatcher


NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


class _FakeTaskClient:
    def __init__(self, tasks: dict[int, dict]) -> None:
        self.tasks = tasks
        self.fetches: list[int] = []
        self.comments: list[tuple[int, str]] = []

    async def fetch_task(self, task_id: int) -> dict:
        self.fetches.append(task_id)
        return dict(self.tasks[task_id])

    async def post_audit_comment(self, task_id: int, content: str) -> None:
        self.comments.append((task_id, content))


class _FakeSessionSpawner:
    def __init__(self) -> None:
        self.spawn_calls: list[dict] = []

    async def spawn(self, **kwargs):
        self.spawn_calls.append(kwargs)
        return kwargs["session_id"]

    async def abort(self, session_id: str) -> None:
        raise AssertionError(f"unexpected abort: {session_id}")


class TaskboardDispatcherStatusTransitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "daemon.sqlite3"
        self._create_pending_table()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_in_progress_fanout_is_single_developer_role(self) -> None:
        row_id = self._insert_pending(
            self._payload(
                event_id="event-in-progress",
                task_id=101,
                fire_generation=1,
                from_status="Backlog",
                to_status="In Progress",
            )
        )
        spawner = _FakeSessionSpawner()
        dispatcher = self._dispatcher(
            tasks={101: self._task(101, "In Progress", 1)},
            spawner=spawner,
        )

        with self._patched_renderer():
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        self.assertEqual(len(spawner.spawn_calls), 1)
        spawn = spawner.spawn_calls[0]
        self.assertEqual(spawn["role"], "Developer")
        self.assertEqual(spawn["agent_id"], "developer")
        self.assertEqual(spawn["task_id"], 101)
        self.assertEqual(spawn["fire_generation"], 1)
        self.assertEqual(spawn["task"]["id"], 101)
        self.assertEqual(self._session_agent_ids(), ["developer"])
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "spawned")

    async def test_review_fanout_is_three_review_roles(self) -> None:
        self._insert_pending(
            self._payload(
                event_id="event-review",
                task_id=202,
                fire_generation=4,
                from_status="In Progress",
                to_status="Review",
            )
        )
        spawner = _FakeSessionSpawner()
        dispatcher = self._dispatcher(
            tasks={202: self._task(202, "Review", 4)},
            spawner=spawner,
        )

        with self._patched_renderer():
            await dispatcher.run_once()

        self.assertEqual(len(spawner.spawn_calls), 3)
        self.assertEqual(
            {call["role"] for call in spawner.spawn_calls},
            {"Code Reviewer", "Security Auditor", "QA Agent"},
        )
        self.assertEqual(
            {call["task_id"] for call in spawner.spawn_calls},
            {202},
        )
        self.assertEqual(
            {call["fire_generation"] for call in spawner.spawn_calls},
            {4},
        )
        self.assertEqual(
            set(self._session_agent_ids()),
            {"code-reviewer", "security-auditor", "qa-agent"},
        )

    async def test_done_transition_is_noop(self) -> None:
        row_id = self._insert_pending(
            self._payload(
                event_id="event-done",
                task_id=303,
                fire_generation=2,
                from_status="Review",
                to_status="Done",
            )
        )
        spawner = _FakeSessionSpawner()
        dispatcher = self._dispatcher(
            tasks={303: self._task(303, "Done", 2)},
            spawner=spawner,
        )

        counts = await dispatcher.run_once()

        self.assertEqual(counts, {"no_op_transition": 1})
        self.assertEqual(spawner.spawn_calls, [])
        self.assertEqual(self._session_count(), 0)
        row = self._pending_row(row_id)
        self.assertEqual(row["dispatch_status"], "no_op_transition")
        self.assertIsNotNone(row["processed_at"])

    async def test_identity_transition_is_noop(self) -> None:
        row_id = self._insert_pending(
            self._payload(
                event_id="event-identity",
                task_id=404,
                fire_generation=5,
                from_status="Review",
                to_status="Review",
            )
        )
        spawner = _FakeSessionSpawner()
        dispatcher = self._dispatcher(
            tasks={404: self._task(404, "Review", 5)},
            spawner=spawner,
        )

        counts = await dispatcher.run_once()

        self.assertEqual(counts, {"no_op_transition": 1})
        self.assertEqual(spawner.spawn_calls, [])
        self.assertEqual(self._session_count(), 0)
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "no_op_transition")

    async def test_fire_generation_increment_refires_review_roles(self) -> None:
        self._insert_pending(
            self._payload(
                event_id="event-review-gen-1",
                task_id=505,
                fire_generation=1,
                from_status="In Progress",
                to_status="Review",
            )
        )
        self._insert_pending(
            self._payload(
                event_id="event-review-gen-2",
                task_id=505,
                fire_generation=2,
                from_status="In Progress",
                to_status="Review",
            )
        )
        spawner = _FakeSessionSpawner()
        dispatcher = self._dispatcher(
            tasks={505: self._task(505, "Review", 2)},
            spawner=spawner,
        )

        with self._patched_renderer():
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 2})
        self.assertEqual(len(spawner.spawn_calls), 6)
        self.assertEqual(self._session_count(), 6)
        self.assertEqual(
            {call["fire_generation"] for call in spawner.spawn_calls},
            {1, 2},
        )

    async def test_idempotent_redelivery_does_not_duplicate_sessions(self) -> None:
        payload = self._payload(
            event_id="same-event-redelivered",
            task_id=606,
            fire_generation=9,
            from_status="In Progress",
            to_status="Review",
        )
        self._insert_pending(payload)
        self._insert_pending(payload)
        spawner = _FakeSessionSpawner()
        dispatcher = self._dispatcher(
            tasks={606: self._task(606, "Review", 9)},
            spawner=spawner,
        )

        with self._patched_renderer():
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1, "duplicate": 1})
        self.assertEqual(len(spawner.spawn_calls), 3)
        self.assertEqual(self._session_count(), 3)
        self.assertEqual(self._pending_statuses(), ["spawned", "duplicate"])

    def _dispatcher(
        self,
        *,
        tasks: dict[int, dict],
        spawner: _FakeSessionSpawner,
    ) -> TaskboardDispatcher:
        return TaskboardDispatcher(
            db_path=self.db_path,
            task_client=_FakeTaskClient(tasks),
            session_manager=spawner,
            max_concurrent_spawns=6,
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
                    last_error TEXT,
                    audit_posted_at TEXT
                )
                """
            )

    def _insert_pending(self, payload: dict) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO webhook_pending(received_at, payload) VALUES (?, ?)",
                (self._iso(NOW), json.dumps(payload)),
            )
            return int(cursor.lastrowid)

    def _payload(
        self,
        *,
        event_id: str,
        task_id: int,
        fire_generation: int,
        from_status: str | None,
        to_status: str,
    ) -> dict:
        return {
            "event_id": event_id,
            "event_type": "task.status_changed",
            "occurred_at": self._iso(NOW),
            "task_id": task_id,
            "fire_generation": fire_generation,
            "from_status": from_status,
            "to_status": to_status,
            "actor": {
                "type": "operator",
                "agent": "User",
                "principal_id": "operator",
            },
            "task": self._task(task_id, to_status, fire_generation),
        }

    @staticmethod
    def _task(task_id: int, status: str, fire_generation: int) -> dict:
        return {
            "id": task_id,
            "title": f"Task {task_id}",
            "description": "Synthetic task for dispatcher tests.",
            "status": status,
            "agent": "Developer",
            "agent_id": "developer",
            "task_type": "Feature",
            "priority": "High",
            "project_id": 7,
            "epic_id": 10021,
            "source_ref": "test",
            "created_at": "2026-04-29T10:00:00Z",
            "updated_at": "2026-04-29T11:00:00Z",
            "fire_generation": fire_generation,
        }

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

    def _session_agent_ids(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT agent_id FROM sessions ORDER BY agent_id").fetchall()
            return [str(row["agent_id"]) for row in rows]

    def _session_count(self) -> int:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
            ).fetchone()
            if exists is None:
                return 0
            row = conn.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()
            return int(row["count"])

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

    @staticmethod
    def _patched_renderer():
        return mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            side_effect=lambda role, task: f"prompt for {role} #{task['id']}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

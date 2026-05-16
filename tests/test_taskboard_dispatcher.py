"""Tests for the taskboard auto-fire dispatcher."""

from __future__ import annotations

import json
import asyncio
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent.runtime_config_resolver import RoleRuntimeConfig
from agent.taskboard_dispatcher import (
    BACKPRESSURE_SUBJECT,
    DISPATCHER_SOURCE,
    DaemonTaskboardSpawner,
    SELF_MOVE_REASON,
    SPAWN_FAILED_SUBJECT,
    WORKTREE_ISOLATION_ENV,
    RepoRoutingError,
    TaskboardDispatcher,
    resolve_taskboard_role,
)
from agent.taskboard_status_router import route_event


NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


_DEFAULT_REPO_URL = "https://forgejo.example/alpha-tech-org/example.git"


class _FakeTaskClient:
    """In-memory taskboard client for dispatcher tests."""

    def __init__(
        self,
        tasks: dict[int, dict],
        *,
        fail_comments: bool = False,
        default_repo_metadata: bool = True,
    ) -> None:
        self.tasks = tasks
        self.fetches: list[int] = []
        self.comments: list[tuple[int, str]] = []
        self.moves: list[tuple[int, str, str, str]] = []
        self.fail_comments = fail_comments
        self.default_repo_metadata = default_repo_metadata

    async def fetch_task(self, task_id: int) -> dict:
        """Return the configured task for ``task_id``."""

        self.fetches.append(task_id)
        task = dict(self.tasks[task_id])
        if (
            self.default_repo_metadata
            and task.get("agent") == "Developer"
            and not task.get("repo_url")
            and not task.get("project")
        ):
            task["project"] = {"repoUrl": _DEFAULT_REPO_URL}
        return task

    async def post_audit_comment(self, task_id: int, content: str) -> None:
        """Record or reject a taskboard audit comment."""

        if self.fail_comments:
            raise RuntimeError("taskboard comment endpoint failed")
        self.comments.append((task_id, content))

    async def move_task_status(
        self,
        task_id: int,
        status: str,
        *,
        reason: str = "",
        agent: str = "Orchestrator",
    ) -> None:
        self.moves.append((task_id, status, reason, agent))
        self.tasks[task_id] = dict(self.tasks[task_id])
        self.tasks[task_id]["status"] = status


class _FakeSessionManager:
    """Record spawn and abort calls without starting agents."""

    def __init__(self, *, spawn_error: Exception | None = None) -> None:
        self.spawn_calls: list[dict] = []
        self.abort_calls: list[str] = []
        self.spawn_error = spawn_error

    async def spawn(self, **kwargs):
        """Record spawn arguments and return the requested session id."""

        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawn_calls.append(kwargs)
        return kwargs["session_id"]

    async def abort(self, session_id: str) -> None:
        """Record an abort request."""

        self.abort_calls.append(session_id)


class _FakeRuntimeConfigResolver:
    """Return deterministic per-role runtime config for dispatcher tests."""

    def __init__(self) -> None:
        self.roles: list[str] = []

    def resolve_for_role(self, role: str, **_kwargs) -> RoleRuntimeConfig:
        self.roles.append(role)
        return RoleRuntimeConfig(
            role=role,
            forgejo_pat=f"pat-for-{role}",
            forgejo_user=f"user-for-{role}",
            forgejo_base_url="http://forgejo.local",
            taskboard_base_url="",
            taskboard_bearer_token="",
            source="test",
        )

    def log_startup_diagnostics(self) -> None:
        return None


class _FakeBus:
    """Record NATS publish calls."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: dict) -> None:
        """Record the publish request."""

        self.published.append((subject, payload))


class _AttachOrderSession:
    def __init__(self) -> None:
        self.taskboard_context = None
        self.forgejo_context = None
        self.runtime_env = {}
        self.attach_seen: tuple[object, object, dict] | None = None
        self.taskboard_dispatcher = {}

    def attach_runtime(self, **_kwargs):
        self.attach_seen = (
            self.taskboard_context,
            self.forgejo_context,
            dict(self.runtime_env),
        )

    def start_auto_mode(self, **_kwargs):
        return None


class _FakeDaemonServer:
    def __init__(self, session: _AttachOrderSession) -> None:
        self.bus = None
        self.signal_consumer = None
        self.scheduler = None
        self.managed = SimpleNamespace(session=session, current_input_task=None)

    async def get_or_create_session(self, *_args, **_kwargs):
        return self.managed

    async def run_input(self, *_args, **_kwargs):
        return SimpleNamespace(error=None, final_text="done")


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
        self.assertEqual(spawn["session_token"], "")
        self.assertEqual(spawn["runtime_config"].forgejo_pat, "pat-for-developer")
        self.assertEqual(spawn["forgejo_context"].token, "pat-for-developer")
        self.assertEqual(spawn["runtime_env"]["FORGEJO_TOKEN_DEVELOPER"], "pat-for-developer")
        self.assertEqual(spawn["runtime_env"]["FORGEJO_TOKEN"], "pat-for-developer")
        self.assertNotIn("TASKBOARD_BEARER_TOKEN", spawn["runtime_env"])
        self.assertIn(
            "taskboard_fire_spawned task_id=10152 fire_generation=7 "
            "role=Developer route_reason=status_to_in_progress "
            "session_id=taskboard-10152-7-developer",
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

    async def test_happy_path_posts_success_audit_comment_once(self) -> None:
        """A successful spawn records the orchestrator audit comment."""

        row_id = self._insert_pending(10154, 4, "Developer")
        task = {
            "id": 10154,
            "title": "Audit callbacks",
            "agent": "Developer",
            "fire_generation": 4,
        }
        task_client = _FakeTaskClient({10154: task})
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        self.assertEqual(
            task_client.comments,
            [
                (
                    10154,
                    "[Orchestrator] Fired developer agent for #10154 "
                    "(session_id=taskboard-10154-4-developer, "
                    "model=codex, profile=xhigh)",
                )
            ],
        )
        self.assertIsNotNone(self._pending_row(row_id)["audit_posted_at"])

    async def test_spawn_failure_posts_system_comment_and_nats_alert(self) -> None:
        """A spawn exception posts the failure audit and emits an alert."""

        row_id = self._insert_pending(10155, 5, "Developer")
        task = {"id": 10155, "agent": "Developer", "fire_generation": 5}
        task_client = _FakeTaskClient({10155: task})
        bus = _FakeBus()
        session_manager = _FakeSessionManager(
            spawn_error=RuntimeError("executor unavailable")
        )
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
            nats_bus=bus,
        )

        counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawn_failed": 1})
        self.assertEqual(
            task_client.comments,
            [
                (
                    10155,
                    "[System] spawn failed for #10155: executor unavailable; "
                    "retry with agent-ops fire 10155",
                )
            ],
        )
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "spawn_failed")
        self.assertIsNotNone(self._pending_row(row_id)["audit_posted_at"])
        self.assertEqual(len(bus.published), 1)
        subject, payload = bus.published[0]
        self.assertEqual(subject, SPAWN_FAILED_SUBJECT)
        self.assertEqual(payload["task_id"], 10155)
        self.assertEqual(payload["fire_generation"], 5)
        self.assertEqual(payload["role"], "developer")
        self.assertEqual(payload["error_class"], "RuntimeError")
        self.assertEqual(payload["error_message"], "executor unavailable")
        self.assertEqual(payload["ts"], "2026-04-28T12:00:00Z")

    async def test_repo_routing_error_marks_spawn_failed_without_spawn(self) -> None:
        """Repo routing failures fail closed for implementation roles."""

        row_id = self._insert_pending(10156, 6, "Developer")
        task = {"id": 10156, "agent": "Developer", "fire_generation": 6}
        task_client = _FakeTaskClient({10156: task})
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        session_manager.spawn_error = RepoRoutingError(
            "missing repo routing metadata for role=Developer"
        )
        with mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered prompt",
        ):
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawn_failed": 1})
        self.assertEqual(session_manager.spawn_calls, [])
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "spawn_failed")
        self.assertEqual(
            task_client.comments,
            [
                (
                    10156,
                    "[System] spawn failed for #10156: missing repo routing metadata for role=Developer; "
                    "retry with agent-ops fire 10156",
                )
            ],
        )

    async def test_invalid_developer_repo_metadata_marks_spawn_failed(self) -> None:
        """Developer dispatch fails closed before prompt render on invalid repo metadata."""

        row_id = self._insert_pending(10367, 8, "Developer")
        task = {
            "id": 10367,
            "agent": "Developer",
            "fire_generation": 8,
            "project": {"repoUrl": "not-a-repo-target"},
        }
        task_client = _FakeTaskClient({10367: task})
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        with mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered prompt",
        ) as renderer:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawn_failed": 1})
        self.assertEqual(session_manager.spawn_calls, [])
        renderer.assert_not_called()
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "spawn_failed")
        self.assertEqual(
            task_client.comments,
            [
                (
                    10367,
                    "[System] spawn failed for #10367: invalid repo routing metadata for role=Developer: "
                    "'not-a-repo-target'; retry with agent-ops fire 10367",
                )
            ],
        )

    async def test_request_changes_missing_repo_fails_closed_before_spawn(self) -> None:
        """REQUEST_CHANGES Developer fix-loop cannot spawn without repo metadata."""

        row_id = self._insert_pending(
            10446,
            2,
            "Developer",
            event_type="review.verdict_submitted",
            from_status=None,
            to_status=None,
            task_status="Review",
            extra_payload={
                "gate_type": "code",
                "verdict": "REQUEST_CHANGES",
                "review_id": 312,
            },
        )
        task = {
            "id": 10446,
            "agent": "Developer",
            "implementation_agent": "Developer",
            "status": "Review",
            "fire_generation": 2,
        }
        task_client = _FakeTaskClient({10446: task}, default_repo_metadata=False)
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        with mock.patch("agent.taskboard_dispatcher.render_taskboard_fire_prompt") as renderer:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawn_failed": 1})
        self.assertEqual(session_manager.spawn_calls, [])
        renderer.assert_not_called()
        self.assertEqual(task_client.moves, [])
        row = self._pending_row(row_id)
        self.assertEqual(row["dispatch_status"], "spawn_failed")
        self.assertIn("missing repo routing metadata", row["last_error"])

    async def test_request_changes_moves_review_task_to_fixing_before_developer_spawn(self) -> None:
        """Fix-loop verdicts put the task in Fixing before Developer starts."""

        self._insert_pending(
            10447,
            3,
            "Developer",
            event_type="review.verdict_submitted",
            from_status=None,
            to_status=None,
            task_status="Review",
            extra_payload={
                "gate_type": "qa",
                "verdict": "REQUEST_CHANGES",
                "review_id": 314,
            },
        )
        task = {
            "id": 10447,
            "agent": "Developer",
            "implementation_agent": "Developer",
            "status": "Review",
            "fire_generation": 3,
            "project": {"repoUrl": _DEFAULT_REPO_URL},
        }
        task_client = _FakeTaskClient({10447: task})
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        with mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered prompt",
        ) as renderer:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        self.assertEqual(
            task_client.moves,
            [
                (
                    10447,
                    "Fixing",
                    SELF_MOVE_REASON,
                    "User",
                )
            ],
        )
        rendered_task = renderer.call_args.args[1]
        self.assertEqual(rendered_task["status"], "Fixing")
        spawn = session_manager.spawn_calls[0]
        self.assertEqual(spawn["role"], "Developer")
        self.assertEqual(spawn["task"]["status"], "Fixing")

    async def test_request_changes_self_move_status_webhook_does_not_spawn_twice(self) -> None:
        """The dispatcher suppresses the status webhook created by its own move."""

        self._insert_pending(
            10448,
            3,
            "Developer",
            event_type="review.verdict_submitted",
            from_status=None,
            to_status=None,
            task_status="Review",
            extra_payload={
                "gate_type": "qa",
                "verdict": "REQUEST_CHANGES",
                "review_id": 315,
            },
        )
        task = {
            "id": 10448,
            "agent": "Developer",
            "implementation_agent": "Developer",
            "status": "Review",
            "fire_generation": 3,
            "project": {"repoUrl": _DEFAULT_REPO_URL},
        }
        task_client = _FakeTaskClient({10448: task})
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        with mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered prompt",
        ):
            first_counts = await dispatcher.run_once()

        self.assertEqual(first_counts, {"spawned": 1})
        self.assertEqual(len(session_manager.spawn_calls), 1)
        self.assertEqual(task_client.moves[0][2:], (SELF_MOVE_REASON, "User"))

        status_row = self._insert_pending(
            10448,
            4,
            "Developer",
            event_type="task.status_changed",
            from_status="Review",
            to_status="Fixing",
            task_status="Fixing",
            extra_payload={
                "event_id": "self-move-status-10448",
                "actor": {
                    "type": "operator",
                    "agent": "User",
                    "principal_id": None,
                },
            },
        )
        second_counts = await dispatcher.run_once()

        self.assertEqual(second_counts, {"self_move_suppressed": 1})
        self.assertEqual(len(session_manager.spawn_calls), 1)
        self.assertEqual(
            self._pending_row(status_row)["dispatch_status"],
            "self_move_suppressed",
        )

    async def test_architect_request_changes_does_not_auto_move_to_fixing(self) -> None:
        """Architect fix-loops stay in Review until the Architect contract exists."""

        self._insert_pending(
            10449,
            6,
            "Code Reviewer",
            event_type="review.verdict_submitted",
            from_status=None,
            to_status=None,
            task_status="Review",
            extra_payload={
                "gate_type": "code",
                "verdict": "REQUEST_CHANGES",
                "review_id": 316,
            },
        )
        task = {
            "id": 10449,
            "agent": "Code Reviewer",
            "implementation_agent": "Architect",
            "status": "Review",
            "fire_generation": 6,
            "task_type": "design",
        }
        task_client = _FakeTaskClient({10449: task}, default_repo_metadata=False)
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        with mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="architect prompt",
        ):
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        self.assertEqual(task_client.moves, [])
        spawn = session_manager.spawn_calls[0]
        self.assertEqual(spawn["role"], "Architect")
        self.assertEqual(spawn["agent_id"], "architect")
        self.assertEqual(spawn["task"]["status"], "Review")

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

    async def test_event_fire_generation_is_preserved_after_refetch(self) -> None:
        """The event generation remains the spawn key after re-fetch."""

        self._insert_pending(300, 1, "Developer")
        task = {"id": 300, "agent": "Developer", "fire_generation": 2}
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={300: task},
            session_manager=session_manager,
        )

        counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        self.assertEqual(len(session_manager.spawn_calls), 1)
        self.assertEqual(session_manager.spawn_calls[0]["fire_generation"], 1)
        self.assertEqual(self._pending_statuses(), ["spawned"])

    async def test_non_forward_transition_is_noop_without_spawn(self) -> None:
        """Transitions outside the v1 forward path are marked as no-ops."""

        self._insert_pending(
            400,
            1,
            "Developer",
            from_status="Review",
            to_status="Done",
        )
        task = {"id": 400, "agent": "Developer", "fire_generation": 1}
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={400: task},
            session_manager=session_manager,
        )

        counts = await dispatcher.run_once()

        self.assertEqual(counts, {"no_op_transition": 1})
        self.assertEqual(session_manager.spawn_calls, [])
        self.assertEqual(self._pending_statuses(), ["no_op_transition"])

    async def test_process_row_calls_route_event_with_payload_task_and_review_context(self) -> None:
        """Dispatcher passes the new router boundary arguments through."""

        self._insert_pending(401, 2, "Developer")
        task = {
            "id": 401,
            "agent": "Developer",
            "fire_generation": 2,
            "reviews": [{"id": 1}],
            "review_requests": [{"id": 2}],
            "review_phase": "requested",
            "review_status": "pending",
            "review_types": ["code"],
        }
        dispatcher = self._dispatcher(tasks={401: task}, session_manager=_FakeSessionManager())

        with mock.patch(
            "agent.taskboard_dispatcher.route_event",
            wraps=route_event,
        ) as router_mock:
            await dispatcher.run_once()

        router_mock.assert_called_once()
        payload, latest_task, review_context = router_mock.call_args.args
        self.assertEqual(payload["to_status"], "In Progress")
        self.assertEqual(latest_task["id"], 401)
        self.assertEqual(review_context["reviews"], [{"id": 1}])
        self.assertEqual(review_context["review_requests"], [{"id": 2}])
        self.assertEqual(review_context["review_phase"], "requested")
        self.assertEqual(review_context["review_status"], "pending")
        self.assertEqual(review_context["review_types"], ["code"])

    async def test_review_verdict_approved_spawns_next_gate_and_records_queued_run(self) -> None:
        """A code approval verdict routes to Security Auditor without status churn."""

        row_id = self._insert_pending(
            402,
            9,
            "Developer",
            event_type="review.verdict_submitted",
            from_status=None,
            to_status=None,
            task_status="Review",
            extra_payload={
                "review_id": 265,
                "gate_type": "code",
                "verdict": "APPROVED",
                "reviewer_user": "agent-code-reviewer",
                "cycle": 8,
            },
        )
        task = {
            "id": 402,
            "agent": "Developer",
            "implementation_agent": "Developer",
            "status": "Review",
            "fire_generation": 9,
        }
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={402: task},
            session_manager=session_manager,
        )

        with mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered prompt",
        ), mock.patch.object(
            dispatcher,
            "_record_agent_run_queued",
            return_value=321,
        ) as record_queued:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        self.assertEqual(len(session_manager.spawn_calls), 1)
        spawn = session_manager.spawn_calls[0]
        self.assertEqual(spawn["role"], "Security Auditor")
        self.assertEqual(spawn["agent_id"], "security-auditor")
        self.assertEqual(spawn["task_id"], 402)
        self.assertEqual(spawn["fire_generation"], 9)
        record_queued.assert_called_once()
        self.assertEqual(record_queued.call_args.kwargs["role"], "security-auditor")
        self.assertEqual(record_queued.call_args.kwargs["trigger_event_id"], str(row_id))

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

    async def test_recent_tool_progress_prevents_stuck_sweep(self) -> None:
        """A long-running session with recent progress is not stuck."""

        row_id = self._insert_pending(701, 2, "Developer")
        self._create_full_sessions_table()
        stale_created_at = self._iso(NOW - timedelta(minutes=75))
        recent_progress_at = self._iso(NOW - timedelta(minutes=5))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE webhook_pending
                SET processed_at = ?, dispatch_status = ?, session_id = ?
                WHERE id = ?
                """,
                (self._iso(NOW), "spawned", "session-active", row_id),
            )
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, taskboard_task_id, fire_generation, agent_id,
                    source, status, webhook_pending_id, created_at, updated_at,
                    last_progress_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "session-active",
                    701,
                    2,
                    "developer",
                    DISPATCHER_SOURCE,
                    "running",
                    str(row_id),
                    stale_created_at,
                    stale_created_at,
                    recent_progress_at,
                ),
            )
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            session_manager=session_manager,
        )

        count = await dispatcher.sweep_stuck_sessions()

        self.assertEqual(count, 0)
        self.assertEqual(session_manager.abort_calls, [])
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "spawned")
        self.assertEqual(self._session_row()["status"], "running")

    async def test_completed_qa_fire_is_not_marked_stuck_aborted(self) -> None:
        """A finished QA fire must leave the sweeper's active-session set."""

        row_id = self._insert_pending(10413, 11, "QA Agent")
        task = {
            "id": 10413,
            "title": "Cycle 11 QA",
            "agent": "QA Agent",
            "fire_generation": 11,
        }
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={10413: task},
            session_manager=session_manager,
        )

        with mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered qa prompt with verdict tool",
        ):
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        session_id = "taskboard-10413-11-qa-agent"
        dispatcher._store.mark_session_terminal(  # type: ignore[attr-defined]
            session_id=session_id,
            outcome_status="succeeded",
        )
        stale_created_at = self._iso(NOW - timedelta(minutes=61))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET created_at = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (stale_created_at, stale_created_at, session_id),
            )

        count = await dispatcher.sweep_stuck_sessions()

        self.assertEqual(count, 0)
        self.assertEqual(session_manager.abort_calls, [])
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "spawned")
        session = self._session_row()
        self.assertEqual(session["session_id"], session_id)
        self.assertEqual(session["agent_id"], "qa-agent")
        self.assertEqual(session["status"], "completed")

    async def test_stuck_session_sweep_posts_abort_audit_comment(self) -> None:
        """The stuck-session sweeper comments on the source task."""

        row_id = self._insert_pending(10156, 6, "Developer")
        self._create_full_sessions_table()
        stale_created_at = self._iso(NOW - timedelta(minutes=61))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE webhook_pending
                SET processed_at = ?, dispatch_status = ?, session_id = ?
                WHERE id = ?
                """,
                (self._iso(NOW), "spawned", "session-stuck-audit", row_id),
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
                    "session-stuck-audit",
                    10156,
                    6,
                    "developer",
                    DISPATCHER_SOURCE,
                    "running",
                    str(row_id),
                    stale_created_at,
                    stale_created_at,
                ),
            )
        task_client = _FakeTaskClient({})
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=_FakeSessionManager(),
        )

        count = await dispatcher.sweep_stuck_sessions()

        self.assertEqual(count, 1)
        self.assertEqual(
            task_client.comments,
            [
                (
                    10156,
                    "[System] sweeper aborted stuck session for #10156 "
                    "after 60min without progress (session_id=session-stuck-audit)",
                )
            ],
        )
        self.assertIsNotNone(self._pending_row(row_id)["audit_posted_at"])

    async def test_stuck_session_sweep_posts_max_runtime_audit_comment(self) -> None:
        """The absolute runtime ceiling has distinct audit wording."""

        row_id = self._insert_pending(10159, 9, "Developer")
        self._create_full_sessions_table()
        old_created_at = self._iso(NOW - timedelta(hours=4, minutes=1))
        recent_progress_at = self._iso(NOW - timedelta(minutes=5))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE webhook_pending
                SET processed_at = ?, dispatch_status = ?, session_id = ?
                WHERE id = ?
                """,
                (self._iso(NOW), "spawned", "session-max-runtime", row_id),
            )
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, taskboard_task_id, fire_generation, agent_id,
                    source, status, webhook_pending_id, created_at, updated_at,
                    last_progress_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "session-max-runtime",
                    10159,
                    9,
                    "developer",
                    DISPATCHER_SOURCE,
                    "running",
                    str(row_id),
                    old_created_at,
                    recent_progress_at,
                    recent_progress_at,
                ),
            )
        task_client = _FakeTaskClient({})
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        count = await dispatcher.sweep_stuck_sessions()

        self.assertEqual(count, 1)
        self.assertEqual(session_manager.abort_calls, ["session-max-runtime"])
        comment = task_client.comments[0][1]
        self.assertEqual(
            comment,
            "[System] sweeper aborted stuck session for #10159 "
            "after exceeding 240min max runtime (session_id=session-max-runtime)",
        )
        self.assertNotIn("without progress", comment)

    async def test_comment_post_failure_is_non_fatal_and_retryable(self) -> None:
        """Comment failures leave the spawned session intact for retry."""

        row_id = self._insert_pending(10157, 7, "Developer")
        task = {"id": 10157, "agent": "Developer", "fire_generation": 7}
        task_client = _FakeTaskClient({10157: task}, fail_comments=True)
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        with self.assertLogs("agent.taskboard_dispatcher", level="WARNING") as logs:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        self.assertEqual(len(session_manager.spawn_calls), 1)
        row = self._pending_row(row_id)
        self.assertEqual(row["dispatch_status"], "spawned")
        self.assertIsNone(row["audit_posted_at"])
        self.assertIn("taskboard_audit_comment_failed", "\n".join(logs.output))

    async def test_restart_retries_missing_audit_without_respawning(self) -> None:
        """A spawned row with no audit timestamp retries only the comment."""

        row_id = self._insert_pending(10158, 8, "Developer")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE webhook_pending
                SET processed_at = ?, dispatch_status = ?, session_id = ?
                WHERE id = ?
                """,
                (self._iso(NOW), "spawned", "session-already-running", row_id),
            )
        task = {"id": 10158, "agent": "Developer", "fire_generation": 8}
        task_client = _FakeTaskClient({10158: task})
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
        )

        counts = await dispatcher.run_once()

        self.assertEqual(counts, {"audit_posted": 1})
        self.assertEqual(session_manager.spawn_calls, [])
        self.assertEqual(
            task_client.comments,
            [
                (
                    10158,
                    "[Orchestrator] Fired developer agent for #10158 "
                    "(session_id=session-already-running, "
                    "model=codex, profile=xhigh)",
                )
            ],
        )
        self.assertIsNotNone(self._pending_row(row_id)["audit_posted_at"])

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

        # Phase 0 follow-up (#10247) — dispatcher now passes session_token +
        # session_generation kwargs (empty in tests when the mint endpoint
        # is unreachable, which is the case here since there's no real
        # taskboard).
        # Router v2 #10258: Backlog -> In Progress now routes by task.agent,
        # so a QA Agent task fires QA Agent (not Developer).
        renderer.assert_called_once_with(
            "QA Agent",
            task,
            session_token="",
            session_generation=None,
        )
        self.assertEqual(session_manager.spawn_calls[0]["prompt"], "known prompt body")

    async def test_daemon_spawner_sets_contexts_before_attach_runtime(self) -> None:
        """Session taskboard/Forgejo contexts are visible during tool attach."""

        session = _AttachOrderSession()
        daemon = _FakeDaemonServer(session)
        runtime_config = RoleRuntimeConfig(
            role="developer",
            forgejo_pat="role-pat",
            forgejo_user="agent-developer",
            forgejo_base_url="http://forgejo.local",
            taskboard_base_url="http://taskboard.local",
            taskboard_bearer_token="bearer-token",
            taskboard_session_token="session-token",
            taskboard_session_generation=11,
            taskboard_agent_name="developer",
            source="test",
        )

        spawner = DaemonTaskboardSpawner(daemon)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            primary_repo = temp_root / "primary"
            worktree = temp_root / "worktree"
            manifest = worktree / ".kai" / "workspace-manifest.json"
            with mock.patch(
                "agent.taskboard_dispatcher._multi_repo_routing_enabled",
                return_value=True,
            ), mock.patch(
                "agent.taskboard_dispatcher.WorktreeManager.ensure_repo_clone",
                return_value=primary_repo,
            ), mock.patch(
                "agent.taskboard_dispatcher.WorktreeManager.create",
                return_value=worktree,
            ), mock.patch(
                "agent.taskboard_dispatcher.WorktreeManager.write_workspace_manifest",
                return_value=manifest,
            ):
                session_id = await spawner.spawn(
                    session_id="session-1",
                    task_id=1,
                    fire_generation=11,
                    role="Developer",
                    agent_id="developer",
                    model="codex",
                    profile="xhigh",
                    prompt="prompt",
                    task={
                        "id": 1,
                        "agent": "Developer",
                        "fire_generation": 11,
                        "project": {"repoUrl": _DEFAULT_REPO_URL},
                    },
                    session_token="session-token",
                    session_generation=11,
                    taskboard_base_url="http://taskboard.local",
                    taskboard_bearer_token="bearer-token",
                    runtime_config=runtime_config,
                    runtime_env=runtime_config.env_overlay(),
                )
                await daemon.managed.current_input_task

        self.assertEqual(session_id, "session-1")
        self.assertIsNotNone(session.attach_seen)
        taskboard_context, forgejo_context, runtime_env = session.attach_seen
        self.assertEqual(taskboard_context.session_token, "session-token")
        self.assertEqual(taskboard_context.session_generation, 11)
        self.assertEqual(forgejo_context.token, "role-pat")
        self.assertEqual(runtime_env["FORGEJO_TOKEN_DEVELOPER"], "role-pat")

    async def test_developer_forced_isolation_cleans_worktree_when_global_flag_off(self) -> None:
        """Developer worktrees are cleaned after finalization even when global isolation is off."""

        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir) / "repo"
            repo_root.mkdir()
            subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "checkout", "-b", "main"], cwd=repo_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "kai-test@example.invalid"], cwd=repo_root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "KAI Test"], cwd=repo_root, check=True, capture_output=True, text=True)
            (repo_root / "README.md").write_text("test repo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo_root, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-m", "initial"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )

            session_id = f"m2-cleanup-{os.getpid()}-{id(self)}"
            session_path = Path("/tmp/kai/sessions") / session_id
            shutil.rmtree(session_path, ignore_errors=True)
            session = _AttachOrderSession()
            daemon = _FakeDaemonServer(session)
            spawner = DaemonTaskboardSpawner(daemon, repo_root=repo_root)
            daemon.taskboard_dispatcher = SimpleNamespace(session_manager=spawner)

            try:
                with mock.patch.dict("os.environ", {WORKTREE_ISOLATION_ENV: "0"}, clear=False), \
                     mock.patch(
                         "agent.taskboard_dispatcher.WorktreeManager.ensure_repo_clone",
                         return_value=repo_root,
                     ), mock.patch(
                         "agent.taskboard_dispatcher._resolve_max_iterations_for_role",
                         return_value=1,
                     ), mock.patch(
                         "agent.agent_runs_client.AgentRunsClient.from_env",
                         return_value=mock.Mock(enabled=False),
                     ):
                    created_session = await spawner.spawn(
                        session_id=session_id,
                        task_id=10450,
                        fire_generation=17,
                        role="Developer",
                        agent_id="developer",
                        model="codex",
                        profile="xhigh",
                        prompt="prompt",
                        task={
                            "id": 10450,
                            "agent": "Developer",
                            "fire_generation": 17,
                            "project": {
                                "repoUrl": str(repo_root),
                                "defaultBranch": "main",
                            },
                        },
                        session_token="session-token",
                        session_generation=17,
                    )
                    self.assertEqual(created_session, session_id)
                    self.assertTrue(session_path.exists())
                    await daemon.managed.current_input_task
                    await asyncio.sleep(0)

                self.assertFalse(session_path.exists())
            finally:
                shutil.rmtree(session_path, ignore_errors=True)

    async def test_daemon_spawner_resolves_role_config_before_clone_with_auth_env(self) -> None:
        """Worktree clone receives the resolver auth env after role resolution."""

        events: list[str] = []

        class _OrderedResolver:
            def resolve_for_role(self, role: str, **_kwargs) -> RoleRuntimeConfig:
                events.append(f"resolve:{role}")
                return RoleRuntimeConfig(
                    role=role,
                    forgejo_pat="resolved-pat",
                    forgejo_user="agent-developer",
                    forgejo_base_url="http://forgejo.local",
                    taskboard_base_url="http://taskboard.local",
                    taskboard_bearer_token="resolved-bearer",
                    source="test",
                )

        session = _AttachOrderSession()
        daemon = _FakeDaemonServer(session)
        spawner = DaemonTaskboardSpawner(
            daemon,
            runtime_config_resolver=_OrderedResolver(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            primary_repo = temp_root / "primary"
            worktree = temp_root / "worktree"

            def ensure_repo_clone(_repo_url, **kwargs):
                events.append("clone")
                self.assertEqual(kwargs["auth_env"]["FORGEJO_TOKEN"], "resolved-pat")
                self.assertEqual(
                    kwargs["auth_env"]["FORGEJO_TOKEN_DEVELOPER"],
                    "resolved-pat",
                )
                self.assertEqual(
                    kwargs["auth_env"]["TASKBOARD_BEARER_TOKEN"],
                    "resolved-bearer",
                )
                self.assertNotIn("extra_env", kwargs)
                primary_repo.mkdir(parents=True, exist_ok=True)
                return primary_repo

            with mock.patch(
                "agent.taskboard_dispatcher._worktree_isolation_enabled",
                return_value=True,
            ), mock.patch(
                "agent.taskboard_dispatcher._multi_repo_routing_enabled",
                return_value=True,
            ), mock.patch(
                "agent.taskboard_dispatcher.WorktreeManager.ensure_repo_clone",
                side_effect=ensure_repo_clone,
            ), mock.patch(
                "agent.taskboard_dispatcher.WorktreeManager.create",
                return_value=worktree,
            ), mock.patch(
                "agent.taskboard_dispatcher._resolve_max_iterations_for_role",
                return_value=5,
            ):
                session_id = await spawner.spawn(
                    session_id="session-2",
                    task_id=2,
                    fire_generation=12,
                    role="Developer",
                    agent_id="developer",
                    model="codex",
                    profile="xhigh",
                    prompt="prompt",
                    task={
                        "id": 2,
                        "agent": "Developer",
                        "fire_generation": 12,
                        "default_branch": "main",
                        "project": {
                            "repoUrl": "https://forgejo.example/openclawdev/taskboard.git"
                        },
                    },
                    session_token="session-token",
                    session_generation=12,
                )
                await daemon.managed.current_input_task

        self.assertEqual(session_id, "session-2")
        self.assertEqual(events[:2], ["resolve:developer", "clone"])
        self.assertEqual(session.runtime_env["FORGEJO_TOKEN"], "resolved-pat")

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
        task_client: _FakeTaskClient | None = None,
        session_manager: _FakeSessionManager,
        nats_bus=None,
        max_concurrent_spawns: int = 6,
    ) -> TaskboardDispatcher:
        return TaskboardDispatcher(
            db_path=self.db_path,
            task_client=task_client or _FakeTaskClient(tasks),
            session_manager=session_manager,
            nats_bus=nats_bus,
            max_concurrent_spawns=max_concurrent_spawns,
            clock=lambda: NOW,
            agent_runs_client=mock.Mock(enabled=False),
            runtime_config_resolver=_FakeRuntimeConfigResolver(),
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
                    last_progress_at TEXT,
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
                    last_progress_at TEXT,
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
        from_status: str | None = "Backlog",
        to_status: str | None = "In Progress",
        event_type: str = "task.status_changed",
        task_status: str | None = None,
        extra_payload: dict | None = None,
    ) -> int:
        payload = {
            "event_id": f"event-{task_id}-{fire_generation}-{agent}",
            "event_type": event_type,
            "task_id": task_id,
            "fire_generation": fire_generation,
            "task": {
                "id": task_id,
                "agent": agent,
                "fire_generation": fire_generation,
                "status": task_status or to_status or "Review",
            },
        }
        if from_status is not None:
            payload["from_status"] = from_status
        if to_status is not None:
            payload["to_status"] = to_status
        if extra_payload:
            payload.update(extra_payload)
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

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
    SPAWN_FAILED_SUBJECT,
    DaemonTaskboardSpawner,
    TaskboardDispatcher,
    _normalize_task_metadata,
    resolve_taskboard_role,
)


NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


class _FakeTaskClient:
    """In-memory taskboard client for dispatcher tests."""

    def __init__(self, tasks: dict[int, dict], *, fail_comments: bool = False) -> None:
        self.tasks = tasks
        self.fetches: list[int] = []
        self.comments: list[tuple[int, str]] = []
        self.fail_comments = fail_comments

    async def fetch_task(self, task_id: int) -> dict:
        """Return the configured task for ``task_id``."""

        self.fetches.append(task_id)
        return dict(self.tasks[task_id])

    async def post_audit_comment(self, task_id: int, content: str) -> None:
        """Record or reject a taskboard audit comment."""

        if self.fail_comments:
            raise RuntimeError("taskboard comment endpoint failed")
        self.comments.append((task_id, content))


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


class _DisabledAgentRunsClient:
    """Disable live agent_runs capacity checks in dispatcher unit tests."""

    enabled = False


class _FakeBus:
    """Record NATS publish calls."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: dict) -> None:
        """Record the publish request."""

        self.published.append((subject, payload))


class _FakeDaemonSession:
    def __init__(self) -> None:
        self.taskboard_dispatcher = None
        self.auto_mode_calls: list[dict] = []

    def attach_runtime(self, **_kwargs) -> None:
        return None

    def start_auto_mode(self, **kwargs) -> None:
        self.auto_mode_calls.append(kwargs)


class _FakeManagedSession:
    def __init__(self) -> None:
        self.session = _FakeDaemonSession()
        self.current_input_task = None


class _FakeDaemonServer:
    def __init__(self) -> None:
        self.bus = None
        self.signal_consumer = None
        self.scheduler = None
        self.managed = _FakeManagedSession()
        self.run_inputs: list[tuple[object, str, str, str]] = []

    async def get_or_create_session(self, *_args, **_kwargs):
        return self.managed

    async def run_input(self, managed, prompt, *, source, job_id):
        self.run_inputs.append((managed, prompt, source, job_id))


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

    async def test_fetch_latest_task_flattens_nested_project_epic_and_repo(self) -> None:
        """Latest task fetch returns structured project, epic, and repo metadata."""

        task = {
            "id": 10281,
            "title": "Workspace metadata",
            "project": {
                "slug": "agent-kai",
                "repoUrl": "https://github.com/agent-kai-bot/agent-kai.git",
                "defaultBranch": "main",
            },
            "epic": {"id": 10032, "title": "Router Architecture v2"},
        }
        dispatcher = self._dispatcher(
            tasks={10281: {"body": {"task": task}}},
            session_manager=_FakeSessionManager(),
        )

        latest = await dispatcher._fetch_latest_task(10281)

        self.assertEqual(latest["project_slug"], "agent-kai")
        self.assertEqual(
            latest["repo_url"],
            "https://github.com/agent-kai-bot/agent-kai.git",
        )
        self.assertEqual(latest["default_branch"], "main")
        self.assertEqual(latest["epic_id"], 10032)
        self.assertEqual(len(latest["repositories"]), 1)
        repo = latest["repositories"][0]
        self.assertEqual(repo["repo_key"], "github.com__agent-kai-bot__agent-kai")
        self.assertTrue(repo["primary"])
        self.assertEqual(repo["push_policy"], "never")

    async def test_missing_project_slug_fails_only_when_workspace_enabled(self) -> None:
        """Legacy task payloads remain usable until ticket workspaces are enabled."""

        task = {
            "id": 10282,
            "title": "Legacy payload",
            "project": {"name": "Agent KAI"},
        }
        dispatcher = self._dispatcher(
            tasks={10282: task},
            session_manager=_FakeSessionManager(),
        )

        with mock.patch.dict("os.environ", {}, clear=False):
            latest = await dispatcher._fetch_latest_task(10282)
        self.assertNotIn("project_slug", latest)

        with mock.patch.dict("os.environ", {"KAI_TICKET_WORKSPACE_ENABLED": "1"}):
            with self.assertRaisesRegex(ValueError, "missing project.slug"):
                await dispatcher._fetch_latest_task(10282)

    def test_multi_repo_payload_produces_ordered_repo_manifest(self) -> None:
        """Structured repositories[] are preserved in taskboard order."""

        normalized = _normalize_task_metadata(
            {
                "id": 10283,
                "project": {"slug": "multi", "defaultBranch": "main"},
                "repositories": [
                    {
                        "repoUrl": (
                            "https://forgejo.local/atcsecure/"
                            "openclawdev-taskboard.git"
                        ),
                        "defaultBranch": "main",
                    },
                    {
                        "repoUrl": "git@github.com:agent-kai-bot/agent-kai.git",
                        "defaultBranch": "feat/workspaces",
                    },
                ],
            }
        )

        repos = normalized["repositories"]
        self.assertEqual(
            [repo["repo_key"] for repo in repos],
            [
                "forgejo.local__atcsecure__openclawdev-taskboard",
                "github.com__agent-kai-bot__agent-kai",
            ],
        )
        self.assertEqual([repo["order"] for repo in repos], [0, 1])
        self.assertTrue(repos[0]["primary"])
        self.assertFalse(repos[1]["primary"])
        self.assertEqual(repos[0]["push_policy"], "allowed")
        self.assertEqual(repos[1]["push_policy"], "never")
        self.assertEqual(normalized["primary_repository"], repos[0])

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

    async def test_workspace_enabled_prepares_before_render_and_passes_spawn_metadata(
        self,
    ) -> None:
        """Reserved sessions prepare workspaces before rendering and spawning."""

        row_id = self._insert_pending(10283, 1, "Developer")
        task = {
            "id": 10283,
            "title": "Workspace spawn",
            "agent": "Developer",
            "fire_generation": 1,
            "project_slug": "agent-kai",
            "epic_id": 10032,
            "repo_url": "https://forgejo.local/agent/agent-kai.git",
            "default_branch": "main",
        }
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={10283: task},
            session_manager=session_manager,
        )
        metadata = {
            "workspace_path": "/tmp/kai/task-10283",
            "worktree_path": "/tmp/kai/task-10283/developer/repos/repo",
            "primary_repo_path": "/tmp/kai/task-10283/developer/repos/repo",
            "workspace_manifest_path": (
                "/tmp/kai/task-10283/shared/workspace-manifest.json"
            ),
        }
        observed_prompt_tasks: list[dict] = []

        def _render(_role, prompt_task, **_kwargs):
            observed_prompt_tasks.append(dict(prompt_task))
            return "rendered prompt"

        with mock.patch.dict(
            "os.environ", {"KAI_TICKET_WORKSPACE_ENABLED": "1"}
        ), mock.patch.object(
            dispatcher,
            "_prepare_role_workspace",
            return_value=metadata,
        ) as prepare, mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            side_effect=_render,
        ):
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        prepare.assert_called_once()
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "spawned")
        self.assertEqual(
            observed_prompt_tasks[0]["workspace_path"], metadata["workspace_path"]
        )
        spawn = session_manager.spawn_calls[0]
        self.assertEqual(spawn["workspace_metadata"], metadata)
        self.assertEqual(spawn["prompt"], "rendered prompt")

    async def test_workspace_prepare_failure_marks_failed_and_posts_audit(
        self,
    ) -> None:
        """Workspace errors fail the reserved session without spawning."""

        row_id = self._insert_pending(10284, 1, "Developer")
        task = {
            "id": 10284,
            "agent": "Developer",
            "fire_generation": 1,
            "project_slug": "agent-kai",
            "repo_url": "https://forgejo.local/agent/agent-kai.git",
        }
        task_client = _FakeTaskClient({10284: task})
        bus = _FakeBus()
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={},
            task_client=task_client,
            session_manager=session_manager,
            nats_bus=bus,
        )

        with mock.patch.dict(
            "os.environ", {"KAI_TICKET_WORKSPACE_ENABLED": "1"}
        ), mock.patch.object(
            dispatcher,
            "_prepare_role_workspace",
            side_effect=RuntimeError("disk full"),
        ):
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawn_failed": 1})
        self.assertEqual(session_manager.spawn_calls, [])
        self.assertEqual(self._pending_row(row_id)["dispatch_status"], "spawn_failed")
        session = self._session_row()
        self.assertEqual(session["status"], "failed")
        self.assertEqual(
            task_client.comments,
            [
                (
                    10284,
                    "[System] spawn failed for #10284: disk full; "
                    "retry with agent-ops fire 10284",
                )
            ],
        )
        self.assertEqual(len(bus.published), 1)

    async def test_duplicate_row_does_not_prepare_workspace(self) -> None:
        """Duplicate webhook rows are rejected before workspace creation."""

        self._insert_pending(10285, 1, "Developer")
        self._insert_pending(10285, 1, "Developer")
        task = {
            "id": 10285,
            "agent": "Developer",
            "fire_generation": 1,
            "project_slug": "agent-kai",
            "repo_url": "https://forgejo.local/agent/agent-kai.git",
        }
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={10285: task},
            session_manager=session_manager,
        )

        with mock.patch.dict(
            "os.environ", {"KAI_TICKET_WORKSPACE_ENABLED": "1"}
        ), mock.patch.object(
            dispatcher,
            "_prepare_role_workspace",
            return_value={"workspace_path": "/tmp/task-10285"},
        ) as prepare:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1, "duplicate": 1})
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(len(session_manager.spawn_calls), 1)

    async def test_workspace_flag_off_does_not_prepare_or_pass_workspace_metadata(self) -> None:
        """Default flag-off path preserves legacy spawn arguments."""

        self._insert_pending(10286, 1, "Developer")
        task = {
            "id": 10286,
            "agent": "Developer",
            "fire_generation": 1,
            "project_slug": "agent-kai",
            "repo_url": "https://forgejo.local/agent/agent-kai.git",
        }
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={10286: task},
            session_manager=session_manager,
        )

        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            dispatcher,
            "_prepare_role_workspace",
            wraps=dispatcher._prepare_role_workspace,
        ) as prepare:
            counts = await dispatcher.run_once()

        self.assertEqual(counts, {"spawned": 1})
        prepare.assert_called_once()
        self.assertIsNone(session_manager.spawn_calls[0]["workspace_metadata"])

    async def test_daemon_spawner_stores_workspace_metadata_on_session(self) -> None:
        """Daemon spawn persists workspace metadata for agent tools."""

        server = _FakeDaemonServer()
        spawner = DaemonTaskboardSpawner(server)
        metadata = {
            "workspace_path": "/tmp/kai/task-10283",
            "worktree_path": "/tmp/kai/task-10283/developer/repos/repo",
            "primary_repo_path": "/tmp/kai/task-10283/developer/repos/repo",
            "workspace_manifest_path": (
                "/tmp/kai/task-10283/shared/workspace-manifest.json"
            ),
            "branch": "task-10283",
        }

        session_id = await spawner.spawn(
            session_id="taskboard-10283-1-developer",
            task_id=10283,
            fire_generation=1,
            role="Developer",
            agent_id="developer",
            model="codex",
            profile="xhigh",
            prompt="rendered prompt",
            task={"id": 10283},
            workspace_metadata=metadata,
        )

        self.assertEqual(session_id, "taskboard-10283-1-developer")
        stored = server.managed.session.taskboard_dispatcher
        self.assertEqual(stored["workspace_path"], metadata["workspace_path"])
        self.assertEqual(stored["worktree_path"], metadata["worktree_path"])
        self.assertEqual(stored["primary_repo_path"], metadata["primary_repo_path"])
        self.assertEqual(stored["workspace_metadata"], metadata)

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
                    "after 60min (session_id=session-stuck-audit)",
                )
            ],
        )
        self.assertIsNotNone(self._pending_row(row_id)["audit_posted_at"])

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
        renderer.assert_called_once_with(
            "Developer",
            task,
            session_token="",
            session_generation=None,
        )
        self.assertEqual(session_manager.spawn_calls[0]["prompt"], "known prompt body")

    async def test_workspace_enabled_dispatcher_renders_concrete_paths(self) -> None:
        """Workspace-enabled dispatcher enriches prompt input with path context."""

        self._insert_pending(10282, 4, "Developer")
        task = {
            "id": 10282,
            "title": "Workspace prompt context",
            "agent": "Developer",
            "fire_generation": 4,
            "project": {
                "slug": "agent-kai",
                "repoUrl": "https://github.com/agent-kai-bot/agent-kai.git",
                "defaultBranch": "main",
            },
            "epic": {"id": 10032, "title": "Router Architecture v2"},
        }
        session_manager = _FakeSessionManager()
        dispatcher = self._dispatcher(
            tasks={10282: task},
            session_manager=session_manager,
        )

        with mock.patch.dict(
            "os.environ",
            {
                "KAI_TICKET_WORKSPACE_ENABLED": "1",
                "KAI_TICKET_WORKSPACE_ROOT": str(Path(self.temp_dir.name) / "tickets"),
            },
        ):
            await dispatcher.run_once()

        prompt = session_manager.spawn_calls[0]["prompt"]
        expected_task_dir = (
            Path(self.temp_dir.name)
            / "tickets"
            / "agent-kai"
            / "epic-10032"
            / "task-10282"
        ).resolve(strict=False)
        expected_repo = (
            expected_task_dir
            / "developer"
            / "repos"
            / "github.com__agent-kai-bot__agent-kai"
        )
        self.assertIn(f"- Workspace path: {expected_task_dir}", prompt)
        self.assertIn(f"- Worktree path: {expected_repo}", prompt)
        self.assertIn(f"- Primary repo path: {expected_repo}", prompt)
        self.assertIn(
            f"- Workspace manifest path: {expected_task_dir / 'shared' / 'workspace-manifest.json'}",
            prompt,
        )

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
            agent_runs_client=_DisabledAgentRunsClient(),
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
        from_status: str | None = "Backlog",
        to_status: str = "In Progress",
    ) -> int:
        payload = {
            "event_id": f"event-{task_id}-{fire_generation}-{agent}",
            "event_type": "task.status_changed",
            "task_id": task_id,
            "fire_generation": fire_generation,
            "from_status": from_status,
            "to_status": to_status,
            "task": {
                "id": task_id,
                "agent": agent,
                "fire_generation": fire_generation,
                "status": to_status,
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

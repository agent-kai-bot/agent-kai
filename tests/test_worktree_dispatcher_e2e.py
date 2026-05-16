from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.taskboard_dispatcher import (
    DaemonTaskboardSpawner,
    RepoRoutingError,
    _finalize_dispatcher_inprocess_run,
)


class _FakeSession:
    def __init__(self) -> None:
        self.taskboard_dispatcher = {}
        self.taskboard_context = None
        self.started = []

    def attach_runtime(self, **kwargs):
        return object()

    def start_auto_mode(
        self,
        *,
        max_iterations: int,
        readonly: bool,
        heartbeat_subscribed: bool | None = None,
    ):
        self.started.append((max_iterations, readonly, heartbeat_subscribed))


class _FakeManaged:
    def __init__(self) -> None:
        self.session = _FakeSession()
        self.current_input_task = None


class _FakeDaemon:
    def __init__(self) -> None:
        self.bus = object()
        self.signal_consumer = object()
        self.scheduler = object()
        self.taskboard_dispatcher = None
        self.sessions = {}
        self.managed = _FakeManaged()
        self.last_prompt = None

    async def get_or_create_session(self, session_id, create_if_missing=True):
        self.sessions[session_id] = self.managed
        return self.managed

    async def run_input(self, managed, prompt, source="user", job_id=None):
        self.last_prompt = prompt

        class _Result:
            final_text = "done"
            error = None
            auto_stopped_reason = None
        return _Result()

    async def stop_session_run(self, session_id):
        return None


class WorktreeDispatcherE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_creates_and_finalize_cleans_worktree_when_enabled(self) -> None:
        daemon = _FakeDaemon()
        spawner = DaemonTaskboardSpawner(daemon_server=daemon, repo_root=Path.cwd())
        cleanup_calls: list[str] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "workspace-manifest.json"

            def write_manifest(*args, **kwargs):
                manifest_path.write_text('{"repo":{"routing_mode":"explicit"}}')
                return manifest_path

            with mock.patch("agent.taskboard_dispatcher._worktree_isolation_enabled", return_value=True), \
                mock.patch("agent.taskboard_dispatcher._multi_repo_routing_enabled", return_value=True), \
                mock.patch("agent.taskboard_dispatcher.WorktreeManager.ensure_repo_clone", return_value=Path("/tmp/kai/taskboard-repos/taskboard")), \
                mock.patch("agent.taskboard_dispatcher.WorktreeManager.create", return_value=Path("/tmp/kai/sessions/sess-e2e")), \
                mock.patch("agent.taskboard_dispatcher.WorktreeManager.write_workspace_manifest", side_effect=write_manifest), \
                mock.patch("agent.taskboard_dispatcher.WorktreeManager.cleanup", side_effect=lambda self, sid: cleanup_calls.append(sid)), \
                mock.patch("agent.taskboard_dispatcher._resolve_max_iterations_for_role", return_value=5), \
                mock.patch("agent.agent_runs_client.AgentRunsClient.from_env") as from_env:
                client = mock.Mock(enabled=True)
                client.list_for_task.return_value = [{"id": 7, "session_id": "sess-e2e", "status": "spawning"}]
                client.patch.return_value = None
                from_env.return_value = client
                session_id = await spawner.spawn(
                    session_id="sess-e2e",
                    task_id=10255,
                    fire_generation=1,
                    role="Developer",
                    agent_id="developer",
                    model="codex",
                    profile="xhigh",
                    prompt="orig prompt",
                    task={
                        "id": 10255,
                        "default_branch": "main",
                        "project": {"repoUrl": "https://forgejo.example/openclawdev/taskboard.git"},
                    },
                    session_token="",
                    session_generation=None,
                )
                self.assertEqual(session_id, "sess-e2e")
                await daemon.managed.current_input_task
                started_prompt = daemon.last_prompt
                self.assertIsNotNone(started_prompt)
                self.assertIn("Target repo URL: https://forgejo.example/openclawdev/taskboard.git", started_prompt)
                self.assertIn("Primary repo path: /tmp/kai/taskboard-repos/taskboard", started_prompt)
                self.assertIn("Worktree path: /tmp/kai/sessions/sess-e2e", started_prompt)
                self.assertIn(f"Workspace manifest path: {manifest_path}", started_prompt)
                self.assertEqual(
                    daemon.managed.session.taskboard_dispatcher["worktree_path"],
                    "/tmp/kai/sessions/sess-e2e",
                )
                self.assertEqual(
                    daemon.managed.session.taskboard_dispatcher["primary_repo_path"],
                    "/tmp/kai/taskboard-repos/taskboard",
                )
                self.assertEqual(
                    daemon.managed.session.taskboard_dispatcher["workspace_manifest_path"],
                    str(manifest_path),
                )
                manifest = json.loads(manifest_path.read_text())
                self.assertEqual(manifest["repo"]["routing_mode"], "explicit")
                self.assertEqual(daemon.managed.session.taskboard_context.agent_name, "developer")
                with mock.patch("agent.taskboard_dispatcher._cleanup_dispatcher_worktree", side_effect=lambda daemon_server, session_id: cleanup_calls.append(session_id)):
                    task = asyncio.get_running_loop().create_task(asyncio.sleep(0))
                    await task
                    _finalize_dispatcher_inprocess_run(task, daemon, "sess-e2e", 10255, "developer")

        self.assertIn("sess-e2e", cleanup_calls)

    async def test_developer_spawn_uses_explicit_repo_when_multi_repo_flag_disabled(self) -> None:
        daemon = _FakeDaemon()
        spawner = DaemonTaskboardSpawner(daemon_server=daemon, repo_root=Path("/srv/local/kai"))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            primary_repo = temp_root / "primary"
            worktree = temp_root / "worktree"
            manifest = worktree / ".kai" / "workspace-manifest.json"
            with mock.patch("agent.taskboard_dispatcher._worktree_isolation_enabled", return_value=True), \
                mock.patch("agent.taskboard_dispatcher._multi_repo_routing_enabled", return_value=False), \
                mock.patch("agent.taskboard_dispatcher.WorktreeManager.ensure_repo_clone", return_value=primary_repo) as ensure_repo_clone, \
                mock.patch("agent.taskboard_dispatcher.WorktreeManager.create", return_value=worktree) as create_worktree, \
                mock.patch("agent.taskboard_dispatcher.WorktreeManager.write_workspace_manifest", return_value=manifest) as write_manifest, \
                mock.patch("agent.taskboard_dispatcher._resolve_max_iterations_for_role", return_value=5), \
                mock.patch("agent.agent_runs_client.AgentRunsClient.from_env") as from_env:
                client = mock.Mock(enabled=True)
                client.list_for_task.return_value = [{"id": 8, "session_id": "sess-local", "status": "spawning"}]
                client.patch.return_value = None
                from_env.return_value = client
                session_id = await spawner.spawn(
                    session_id="sess-local",
                    task_id=10367,
                    fire_generation=2,
                    role="Developer",
                    agent_id="developer",
                    model="codex",
                    profile="xhigh",
                    prompt="orig prompt",
                    task={
                        "id": 10367,
                        "default_branch": "main",
                        "project": {"repoUrl": "https://forgejo.example/openclawdev/taskboard.git"},
                    },
                    session_token="",
                    session_generation=None,
                )
                await daemon.managed.current_input_task

        self.assertEqual(session_id, "sess-local")
        ensure_repo_clone.assert_called_once()
        create_worktree.assert_called_once()
        write_manifest.assert_called_once()
        self.assertIn(f"Worktree path: {worktree}", daemon.last_prompt)

    async def test_developer_spawn_fails_closed_without_repo_when_isolation_off(self) -> None:
        daemon = _FakeDaemon()
        spawner = DaemonTaskboardSpawner(
            daemon_server=daemon,
            repo_root=Path("/srv/local/kai"),
        )

        with mock.patch(
            "agent.taskboard_dispatcher._worktree_isolation_enabled",
            return_value=False,
        ), mock.patch(
            "agent.taskboard_dispatcher.WorktreeManager.create"
        ) as create_worktree:
            with self.assertRaises(RepoRoutingError) as err:
                await spawner.spawn(
                    session_id="sess-missing-repo",
                    task_id=10446,
                    fire_generation=2,
                    role="Developer",
                    agent_id="developer",
                    model="codex",
                    profile="xhigh",
                    prompt="orig prompt",
                    task={"id": 10446, "agent": "Developer", "fire_generation": 2},
                    session_token="",
                    session_generation=None,
                )

        self.assertIn("missing repo routing metadata", str(err.exception))
        create_worktree.assert_not_called()
        self.assertEqual(daemon.last_prompt, None)
        self.assertEqual(daemon.sessions, {})

    async def test_developer_spawn_isolates_worktree_when_global_isolation_off(self) -> None:
        daemon = _FakeDaemon()
        spawner = DaemonTaskboardSpawner(
            daemon_server=daemon,
            repo_root=Path("/srv/local/kai"),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            primary_repo = temp_root / "primary"
            worktree = temp_root / "worktree"
            manifest = worktree / ".kai" / "workspace-manifest.json"
            with mock.patch(
                "agent.taskboard_dispatcher._worktree_isolation_enabled",
                return_value=False,
            ), mock.patch(
                "agent.taskboard_dispatcher._multi_repo_routing_enabled",
                return_value=False,
            ), mock.patch(
                "agent.taskboard_dispatcher.WorktreeManager.ensure_repo_clone",
                return_value=primary_repo,
            ), mock.patch(
                "agent.taskboard_dispatcher.WorktreeManager.create",
                return_value=worktree,
            ), mock.patch(
                "agent.taskboard_dispatcher.WorktreeManager.write_workspace_manifest",
                return_value=manifest,
            ), mock.patch(
                "agent.taskboard_dispatcher._resolve_max_iterations_for_role",
                return_value=5,
            ):
                session_id = await spawner.spawn(
                    session_id="sess-isolated-dev",
                    task_id=10447,
                    fire_generation=3,
                    role="Developer",
                    agent_id="developer",
                    model="codex",
                    profile="xhigh",
                    prompt="orig prompt",
                    task={
                        "id": 10447,
                        "agent": "Developer",
                        "fire_generation": 3,
                        "project": {
                            "repoUrl": "https://forgejo.example/openclawdev/taskboard.git"
                        },
                    },
                    session_token="tok",
                    session_generation=3,
                )
                await daemon.managed.current_input_task

        self.assertEqual(session_id, "sess-isolated-dev")
        self.assertIn(f"Worktree path: {worktree}", daemon.last_prompt)
        self.assertEqual(
            daemon.managed.session.taskboard_dispatcher["worktree_path"],
            str(worktree),
        )

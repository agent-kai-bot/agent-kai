from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.taskboard_dispatcher import (
    RepoRoutingError,
    RepoTarget,
    _cleanup_dispatcher_worktree,
    _multi_repo_routing_enabled,
    _resolve_repo_target,
)
from agent.worktree_manager import WorktreeManager


class RepoWorkspaceRoutingTests(unittest.TestCase):
    def test_resolve_repo_target_uses_project_repo_url(self) -> None:
        target = _resolve_repo_target(
            {
                "project": {
                    "repoUrl": "https://forgejo.example/openclawdev/taskboard.git",
                    "defaultBranch": "develop",
                    "slug": "taskboard",
                }
            },
            fallback_repo_root=Path("/srv/local/kai"),
            role="Developer",
        )
        self.assertEqual(
            target,
            RepoTarget(
                repo_key="forgejo.example-openclawdev-taskboard",
                repo_url="https://forgejo.example/openclawdev/taskboard.git",
                default_branch="develop",
                source="task.project.repoUrl",
                routing_mode="explicit",
                display_name="taskboard",
            ),
        )

    def test_resolve_repo_target_falls_back_to_local_repo(self) -> None:
        target = _resolve_repo_target({}, fallback_repo_root=Path("/srv/local/kai"), role="Architect")
        self.assertEqual(target.routing_mode, "fallback_local")
        self.assertEqual(target.repo_url, "/srv/local/kai")
        self.assertEqual(target.default_branch, "main")

    def test_write_workspace_manifest_records_repo_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir) / "session"
            worktree.mkdir(parents=True)
            primary = Path(temp_dir) / "primary"
            primary.mkdir()
            manifest_path = WorktreeManager.write_workspace_manifest(
                worktree,
                task_id=10367,
                session_id="taskboard-10367-1-developer",
                fire_generation=1,
                agent_id="developer",
                role="Developer",
                primary_repo_path=primary,
                repo_url="https://forgejo.example/openclawdev/taskboard.git",
                default_branch="main",
                repo_routing_mode="explicit",
                source="task.project.repoUrl",
                repo_key="taskboard",
            )
            payload = json.loads(manifest_path.read_text())
        self.assertEqual(payload["repo"]["routing_mode"], "explicit")
        self.assertEqual(payload["repo"]["repo_url"], "https://forgejo.example/openclawdev/taskboard.git")
        self.assertEqual(payload["paths"]["primary_repo_path"], str(primary))
        self.assertEqual(payload["paths"]["worktree_path"], str(worktree))

    def test_resolve_repo_target_rejects_missing_repo_for_developer(self) -> None:
        with self.assertRaises(RepoRoutingError):
            _resolve_repo_target({}, fallback_repo_root=Path("/srv/local/kai"), role="Developer")

    def test_resolve_repo_target_rejects_malformed_repo_for_developer(self) -> None:
        with self.assertRaises(RepoRoutingError):
            _resolve_repo_target(
                {"project": {"repoUrl": "not a repo url"}},
                fallback_repo_root=Path("/srv/local/kai"),
                role="Developer",
            )

    def test_resolve_repo_target_falls_back_for_malformed_repo_for_architect(self) -> None:
        target = _resolve_repo_target(
            {"project": {"repoUrl": "not a repo url"}},
            fallback_repo_root=Path("/srv/local/kai"),
            role="Architect",
        )
        self.assertEqual(target.routing_mode, "fallback_local")

    def test_multi_repo_routing_flag_defaults_off(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertFalse(_multi_repo_routing_enabled())

    def test_multi_repo_routing_flag_accepts_enabled_values(self) -> None:
        with mock.patch.dict("os.environ", {"TASKBOARD_MULTI_REPO_ROUTING": "1"}, clear=False):
            self.assertTrue(_multi_repo_routing_enabled())

    def test_cleanup_uses_session_repo_root_when_available(self) -> None:
        daemon_server = mock.Mock()
        daemon_server.taskboard_dispatcher = mock.Mock(_session_repo_roots={"sess-1": Path("/tmp/foreign")})
        with mock.patch("agent.taskboard_dispatcher._worktree_isolation_enabled", return_value=True), \
             mock.patch("agent.taskboard_dispatcher.WorktreeManager.cleanup") as cleanup:
            _cleanup_dispatcher_worktree(daemon_server, "sess-1")
        cleanup.assert_called_once_with("sess-1")

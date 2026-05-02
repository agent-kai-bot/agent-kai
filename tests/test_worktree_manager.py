from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.worktree_manager import WorktreeManager


class WorktreeManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name) / "repo"
        self.repo_root.mkdir()
        self.manager = WorktreeManager(self.repo_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_runs_git_worktree_add(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, check, capture_output, text):
            calls.append(cmd)
            return mock.Mock(stdout="", stderr="")

        with mock.patch("agent.worktree_manager.subprocess.run", side_effect=fake_run):
            path = self.manager.create("sess-1", "task-1-dev-1")

        self.assertEqual(path, Path("/tmp/kai/sessions/sess-1"))
        self.assertEqual(
            calls[0],
            [
                "git",
                "-C",
                str(self.repo_root),
                "worktree",
                "add",
                "/tmp/kai/sessions/sess-1",
                "-b",
                "task-1-dev-1",
                "main",
            ],
        )

    def test_create_retries_after_prune_on_collision(self) -> None:
        calls: list[list[str]] = []
        boom = subprocess.CalledProcessError(1, ["git", "worktree", "add"])

        def fake_run(cmd, check, capture_output, text):
            calls.append(cmd)
            if len(calls) == 1:
                raise boom
            return mock.Mock(stdout="", stderr="")

        with mock.patch("agent.worktree_manager.subprocess.run", side_effect=fake_run):
            path = self.manager.create("sess-2", "task-2-dev-1")

        self.assertEqual(path, Path("/tmp/kai/sessions/sess-2"))
        self.assertEqual(calls[1], ["git", "-C", str(self.repo_root), "worktree", "prune"])
        self.assertEqual(calls[2][0:4], ["git", "-C", str(self.repo_root), "worktree"])

    def test_cleanup_removes_worktree_and_branch(self) -> None:
        session_path = Path("/tmp/kai/sessions/sess-3")
        session_path.mkdir(parents=True, exist_ok=True)
        calls: list[list[str]] = []

        def fake_run(cmd, check, capture_output, text):
            calls.append(cmd)
            if cmd[-2:] == ["list", "--porcelain"]:
                return mock.Mock(
                    stdout=(
                        f"worktree {session_path}\n"
                        "HEAD abcdef\n"
                        "branch refs/heads/task-3-dev-1\n"
                    ),
                    stderr="",
                )
            if len(cmd) >= 2 and cmd[-2] == "--abbrev-ref":
                raise subprocess.CalledProcessError(1, cmd)
            return mock.Mock(stdout="", stderr="")

        with mock.patch("agent.worktree_manager.subprocess.run", side_effect=fake_run):
            self.manager.cleanup("sess-3")

        self.assertIn(
            ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(session_path)],
            calls,
        )
        self.assertIn(
            ["git", "-C", str(self.repo_root), "branch", "-D", "task-3-dev-1"],
            calls,
        )

    def test_cleanup_skips_branch_delete_when_upstream_exists(self) -> None:
        session_path = Path("/tmp/kai/sessions/sess-5")
        calls: list[list[str]] = []

        def fake_run(cmd, check, capture_output, text):
            calls.append(cmd)
            if cmd[-2:] == ["list", "--porcelain"]:
                return mock.Mock(
                    stdout=(
                        f"worktree {session_path}\n"
                        "HEAD abcdef\n"
                        "branch refs/heads/task-5-dev-1\n"
                    ),
                    stderr="",
                )
            return mock.Mock(stdout="origin/task-5-dev-1\n", stderr="")

        with mock.patch("agent.worktree_manager.subprocess.run", side_effect=fake_run):
            self.manager.cleanup("sess-5")

        self.assertNotIn(
            ["git", "-C", str(self.repo_root), "branch", "-D", "task-5-dev-1"],
            calls,
        )

    def test_cleanup_is_best_effort_on_failures(self) -> None:
        def fake_run(cmd, check, capture_output, text):
            if cmd[-2:] == ["list", "--porcelain"]:
                raise subprocess.CalledProcessError(1, cmd)
            raise subprocess.CalledProcessError(1, cmd)

        with mock.patch("agent.worktree_manager.subprocess.run", side_effect=fake_run):
            self.manager.cleanup("sess-4")

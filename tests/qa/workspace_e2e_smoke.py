#!/usr/bin/env python3
"""End-to-end smoke harness for taskboard ticket workspace isolation.

The smoke runs entirely against temporary local git repositories. It exercises
workspace preparation for a developer and reviewer without touching the operator
checkout, then exercises terminal cleanup so live role worktrees are removed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.taskboard_workspace import RepoRef, TicketWorkspaceManager, TicketWorkspacePaths  # noqa: E402


@dataclass(frozen=True)
class SmokeResult:
    operator_repo: Path
    operator_head_before: str
    operator_head_after: str
    developer_path: Path
    reviewer_path: Path
    developer_commit: str
    reviewer_commit: str
    task_dir_removed: bool
    pruned_repo_keys: tuple[str, ...]


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr or result.stdout}"
        )
    return result.stdout.strip()


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_operator_repo(root: Path) -> Path:
    repo = root / "operator-clone"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "smoke@example.invalid")
    git(repo, "config", "user.name", "Workspace Smoke")
    write(repo / "README.md", "initial\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_smoke(tmp_root: Path) -> SmokeResult:
    operator_repo = create_operator_repo(tmp_root)
    operator_head_before = git(operator_repo, "rev-parse", "HEAD")

    workspace_root = tmp_root / "ticket-workspaces"
    repo = RepoRef(
        host="local.test",
        owner="smoke",
        repo="operator-clone",
        url=str(operator_repo),
        default_branch="main",
    )
    manager = TicketWorkspaceManager(
        TicketWorkspacePaths(
            root=workspace_root,
            project_slug="agent-kai",
            epic_id=10032,
            task_id=10287,
        )
    )

    developer = manager.prepare_role_workspace(
        role="developer",
        repo=repo,
        branch="task-10287-ws-9-smoke",
    )
    git(developer.worktree_path, "config", "user.email", "developer@example.invalid")
    git(developer.worktree_path, "config", "user.name", "Smoke Developer")
    write(developer.worktree_path / "smoke.txt", "developer change visible to reviewers\n")
    git(developer.worktree_path, "add", "smoke.txt")
    git(developer.worktree_path, "commit", "-m", "test: smoke developer commit")
    developer_commit = git(developer.worktree_path, "rev-parse", "HEAD")

    reviewer = manager.prepare_role_workspace(
        role="code-reviewer",
        repo=repo,
        branch="task-10287-ws-9-smoke",
        developer_commit=developer_commit,
    )
    reviewer_commit = git(reviewer.worktree_path, "rev-parse", "HEAD")

    operator_head_after = git(operator_repo, "rev-parse", "HEAD")

    assert_true(
        operator_head_before == operator_head_after,
        "operator repo HEAD changed during workspace smoke",
    )
    assert_true(
        developer.worktree_path != reviewer.worktree_path,
        "developer and reviewer paths must differ",
    )
    assert_true(
        reviewer_commit == developer_commit,
        "reviewer did not see the developer commit",
    )
    assert_true(
        (reviewer.worktree_path / "smoke.txt").read_text(encoding="utf-8")
        == "developer change visible to reviewers\n",
        "reviewer worktree is missing developer file contents",
    )

    cleanup_result = manager.cleanup_cancelled_workspace(
        now=datetime(2026, 5, 3, tzinfo=timezone.utc)
    )
    assert_true(cleanup_result.deleted, "cleanup did not report deletion")
    assert_true(not developer.worktree_path.exists(), "developer live worktree still exists")
    assert_true(not reviewer.worktree_path.exists(), "reviewer live worktree still exists")
    assert_true(not manager.paths.task_dir.exists(), "task workspace directory still exists")

    return SmokeResult(
        operator_repo=operator_repo,
        operator_head_before=operator_head_before,
        operator_head_after=operator_head_after,
        developer_path=developer.worktree_path,
        reviewer_path=reviewer.worktree_path,
        developer_commit=developer_commit,
        reviewer_commit=reviewer_commit,
        task_dir_removed=not manager.paths.task_dir.exists(),
        pruned_repo_keys=cleanup_result.pruned_repo_keys,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="leave the temporary smoke directory on disk for debugging",
    )
    parser.add_argument(
        "--tmp-root",
        type=Path,
        default=None,
        help="use an existing/created temp root instead of mkdtemp",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    created_tmp = args.tmp_root is None
    tmp_root = args.tmp_root or Path(tempfile.mkdtemp(prefix="kai-workspace-e2e-"))
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        result = run_smoke(tmp_root)
        print("workspace e2e smoke: PASS")
        print(f"operator_repo={result.operator_repo}")
        print(f"operator_head_before={result.operator_head_before}")
        print(f"operator_head_after={result.operator_head_after}")
        print(f"developer_path={result.developer_path}")
        print(f"reviewer_path={result.reviewer_path}")
        print(f"developer_commit={result.developer_commit}")
        print(f"reviewer_commit={result.reviewer_commit}")
        print(f"task_dir_removed={result.task_dir_removed}")
        print(f"pruned_repo_keys={','.join(result.pruned_repo_keys)}")
        return 0
    finally:
        if created_tmp and not args.keep_temp:
            shutil.rmtree(tmp_root, ignore_errors=True)
        elif args.keep_temp:
            print(f"kept_tmp_root={tmp_root}")


if __name__ == "__main__":
    raise SystemExit(main())

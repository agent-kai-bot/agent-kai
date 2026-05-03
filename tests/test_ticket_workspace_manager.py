"""Tests for git-backed taskboard ticket workspace preparation."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from agent.taskboard_workspace import (
    WorkspaceGitCommandError,
    RepoRef,
    WorkspaceDirtyWrongBranchError,
    TicketWorkspaceManager,
    TicketWorkspacePaths,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def source_repo(tmp_path: Path) -> RepoRef:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return RepoRef(
        host="local.test",
        owner="fixtures",
        repo="source",
        url=str(repo),
        default_branch="main",
    )


def test_first_fire_creates_git_cache_and_developer_worktree(tmp_path: Path, source_repo: RepoRef) -> None:
    manager = TicketWorkspaceManager(TicketWorkspacePaths(root=tmp_path / "tickets", project_slug="agent-kai", task_id=10280))

    prepared = manager.prepare_role_workspace(
        role="Developer",
        repo=source_repo,
        branch="task-10280-ws-2",
    )

    assert prepared.cache_path == tmp_path / "tickets" / "_git-cache" / f"{source_repo.key}.git"
    assert prepared.cache_path.exists()
    assert prepared.worktree_path.exists()
    assert _git(prepared.worktree_path, "branch", "--show-current") == "task-10280-ws-2"
    assert (prepared.manifest.shared_dir and Path(prepared.manifest.shared_dir) / "workspace-manifest.json").exists()


def test_refire_reuses_existing_developer_worktree(tmp_path: Path, source_repo: RepoRef) -> None:
    manager = TicketWorkspaceManager(TicketWorkspacePaths(root=tmp_path / "tickets", project_slug="agent-kai", task_id=10280))
    kwargs = dict(
        role="developer",
        repo=source_repo,
        branch="task-10280-ws-2",
    )
    first = manager.prepare_role_workspace(**kwargs)
    marker = first.worktree_path / "local.txt"
    marker.write_text("uncommitted but same branch\n", encoding="utf-8")

    second = manager.prepare_role_workspace(**kwargs)

    assert second.worktree_path == first.worktree_path
    assert marker.read_text(encoding="utf-8") == "uncommitted but same branch\n"


def test_dirty_wrong_branch_developer_worktree_fails_typed(tmp_path: Path, source_repo: RepoRef) -> None:
    manager = TicketWorkspaceManager(TicketWorkspacePaths(root=tmp_path / "tickets", project_slug="agent-kai", task_id=10280))
    prepared = manager.prepare_role_workspace(
        role="developer",
        repo=source_repo,
        branch="task-10280-a",
    )
    _git(prepared.worktree_path, "checkout", "-b", "wrong-branch")
    (prepared.worktree_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(WorkspaceDirtyWrongBranchError):
        manager.prepare_role_workspace(
            role="developer",
            repo=source_repo,
            branch="task-10280-a",
        )


def test_concurrent_prepares_serialize_for_same_repo(tmp_path: Path, source_repo: RepoRef) -> None:
    manager = TicketWorkspaceManager(TicketWorkspacePaths(root=tmp_path / "tickets", project_slug="agent-kai", task_id=10280))
    errors: list[BaseException] = []

    def prepare(index: int) -> None:
        try:
            local_manager = TicketWorkspaceManager(
                TicketWorkspacePaths(
                    root=tmp_path / "tickets",
                    project_slug="agent-kai",
                    task_id=10280 + index,
                )
            )
            local_manager.prepare_role_workspace(
                role="developer",
                repo=source_repo,
                branch=f"task-{10280 + index}",
            )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=prepare, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert (tmp_path / "tickets" / "_locks" / "repo" / f"{source_repo.key}.lock").exists()
    for index in range(4):
        assert (
            tmp_path
            / "tickets"
            / "agent-kai"
            / "no-epic"
            / f"task-{10280 + index}"
            / "developer"
            / "repos"
            / source_repo.key
        ).exists()


def test_reviewer_gets_detached_worktree_at_developer_commit(tmp_path: Path, source_repo: RepoRef) -> None:
    manager = TicketWorkspaceManager(TicketWorkspacePaths(root=tmp_path / "tickets", project_slug="agent-kai", task_id=10280))
    developer = manager.prepare_role_workspace(
        role="developer",
        repo=source_repo,
        branch="task-10280-ws-2",
    )

    reviewer = manager.prepare_role_workspace(
        role="code-reviewer",
        repo=source_repo,
        branch="task-10280-ws-2",
        developer_commit=developer.commit,
    )

    assert reviewer.detached is True
    assert _git(reviewer.worktree_path, "branch", "--show-current") == ""
    assert reviewer.commit == developer.commit


def test_repo_ref_strips_url_userinfo_from_stored_url() -> None:
    repo = RepoRef.from_url(
        "https://user:super-secret@example.com/owner/repo.git",
        default_branch="main",
    )

    assert repo.url == "https://[REDACTED]@example.com/owner/repo.git"
    assert "super-secret" not in repo.url


def test_git_command_error_redacts_url_userinfo() -> None:
    error = WorkspaceGitCommandError(
        ["clone", "https://user:super-secret@example.com/owner/repo.git"],
        cwd=None,
        stderr=(
            "fatal: could not read from "
            "https://user:super-secret@example.com/owner/repo.git"
        ),
    )

    message = str(error)
    assert "super-secret" not in message
    assert "https://[REDACTED]@example.com/owner/repo.git" in message

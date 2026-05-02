"""Tests for taskboard ticket workspace path modeling."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.taskboard_workspace import (
    DEFAULT_TICKET_WORKSPACE_ROOT,
    RepoRef,
    RepoWorkspaceManifest,
    TaskboardWorkspaceConfig,
    TicketWorkspaceManifest,
    TicketWorkspacePaths,
    WorkspacePathError,
    parse_repo_url,
    repo_key,
    role_slug,
    safe_manifest_path_under,
    safe_slug,
)


def test_default_workspace_root_is_stable() -> None:
    """The default root matches the Phase 1 workspace design."""

    config = TaskboardWorkspaceConfig.from_sources(env={}, config={})

    assert config.root == DEFAULT_TICKET_WORKSPACE_ROOT
    assert config.root == Path("/home/atc/workspaces/kai-tickets")


def test_config_workspace_root_is_used_when_env_absent(tmp_path: Path) -> None:
    """agent-config.json taskboard_workspace.root is the second source."""

    configured = tmp_path / "configured-root"

    config = TaskboardWorkspaceConfig.from_sources(
        env={},
        config={"taskboard_workspace": {"root": str(configured)}},
    )

    assert config.root == configured.resolve(strict=False)


def test_env_workspace_root_overrides_config(tmp_path: Path) -> None:
    """KAI_TICKET_WORKSPACE_ROOT wins over agent-config.json."""

    env_root = tmp_path / "env-root"
    config_root = tmp_path / "config-root"

    config = TaskboardWorkspaceConfig.from_sources(
        env={"KAI_TICKET_WORKSPACE_ROOT": str(env_root)},
        config={"taskboard_workspace": {"root": str(config_root)}},
    )

    assert config.root == env_root.resolve(strict=False)


@pytest.mark.parametrize(
    "slug",
    ["Agent-KAI", "agent.kai", "../agent-kai", "agent/kai", "agent kai", "-agent", ""],
)
def test_unsafe_slugs_are_rejected(slug: str) -> None:
    """Unsafe slugs are rejected rather than normalized into paths."""

    with pytest.raises(WorkspacePathError):
        safe_slug(slug, "project_slug")


@pytest.mark.parametrize(
    "root",
    ["relative/root", "/tmp/kai/../escape"],
)
def test_unsafe_workspace_roots_are_rejected(root: str) -> None:
    """Relative roots and traversal-bearing roots are rejected."""

    with pytest.raises(WorkspacePathError):
        TaskboardWorkspaceConfig.from_sources(
            env={"KAI_TICKET_WORKSPACE_ROOT": root},
            config={},
        )


def test_project_epic_task_role_and_repo_paths(tmp_path: Path) -> None:
    """The path model covers project, epic, task, role, shared, and repo paths."""

    paths = TicketWorkspacePaths(
        root=tmp_path,
        project_slug="agent-kai",
        epic_id=10032,
        task_id=10279,
    )
    repo = RepoRef.from_url(
        "https://github.com/agent-kai-bot/agent-kai.git",
        default_branch="main",
    )

    assert paths.project_dir == tmp_path / "agent-kai"
    assert paths.epic_dir == tmp_path / "agent-kai" / "epic-10032"
    assert paths.task_dir == tmp_path / "agent-kai" / "epic-10032" / "task-10279"
    assert paths.shared_dir == paths.task_dir / "shared"
    assert paths.role_dir("Developer") == paths.task_dir / "developer"
    assert repo.key == "github.com__agent-kai-bot__agent-kai"
    assert paths.repo_dir("Developer", repo) == (
        paths.task_dir
        / "developer"
        / "repos"
        / "github.com__agent-kai-bot__agent-kai"
    )


def test_no_epic_path_uses_no_epic_directory(tmp_path: Path) -> None:
    """Tickets without epics use a stable no-epic directory."""

    paths = TicketWorkspacePaths(root=tmp_path, project_slug="agent-kai", task_id=10279)

    assert paths.epic_dir == tmp_path / "agent-kai" / "no-epic"
    assert paths.task_dir == tmp_path / "agent-kai" / "no-epic" / "task-10279"


def test_path_traversal_is_rejected_for_model_inputs(tmp_path: Path) -> None:
    """Traversal attempts in project, role, and repo inputs cannot form paths."""

    with pytest.raises(WorkspacePathError):
        TicketWorkspacePaths(
            root=tmp_path,
            project_slug="../agent-kai",
            task_id=10279,
        )

    paths = TicketWorkspacePaths(root=tmp_path, project_slug="agent-kai", task_id=10279)
    with pytest.raises(WorkspacePathError):
        paths.role_dir("../developer")
    with pytest.raises(WorkspacePathError):
        paths.repo_dir("developer", "github.com__owner__../repo")


def test_role_slug_is_lowercase_and_path_safe() -> None:
    """Role names normalize to canonical lowercase path components."""

    assert role_slug("Code-Reviewer") == "code-reviewer"
    assert role_slug("security_auditor") == "security_auditor"
    with pytest.raises(WorkspacePathError):
        role_slug("code/reviewer")


def test_repo_key_helpers_support_https_and_ssh_urls() -> None:
    """Repo keys use host__owner__repo for supported remote URL forms."""

    assert repo_key("GitHub.com", "Agent-KAI-Bot", "agent-kai.git") == (
        "github.com__agent-kai-bot__agent-kai"
    )
    assert parse_repo_url("git@github.com:agent-kai-bot/agent-kai.git") == (
        "github.com",
        "agent-kai-bot",
        "agent-kai",
    )
    assert parse_repo_url("ssh://git@forgejo.local/atcsecure/openclaw-gateway.git") == (
        "forgejo.local",
        "atcsecure",
        "openclaw-gateway",
    )


@pytest.mark.parametrize(
    "url",
    [
        "not a url",
        "https://github.com/owner/../repo.git",
        "file:///tmp/repo",
        "https://bad..host/owner/repo",
    ],
)
def test_unsafe_repo_urls_are_rejected(url: str) -> None:
    """Repo URL parsing rejects traversal and unsupported schemes."""

    with pytest.raises(WorkspacePathError):
        parse_repo_url(url)


def test_manifest_dataclasses_validate_and_serialize_paths(tmp_path: Path) -> None:
    """Manifest dataclasses capture and canonicalize safe workspace metadata."""

    paths = TicketWorkspacePaths(
        root=tmp_path,
        project_slug="agent-kai",
        epic_id=10032,
        task_id=10279,
    )
    repo_manifest = RepoWorkspaceManifest(
        repo_key="github.com__agent-kai-bot__agent-kai",
        path=str(paths.repo_dir("developer", "github.com__agent-kai-bot__agent-kai")),
        repo_url="https://github.com/agent-kai-bot/agent-kai.git",
        default_branch="main",
        branch="task-10279-ws-1",
        role="Developer",
    )

    manifest = TicketWorkspaceManifest.from_paths(paths, repos=[repo_manifest])

    assert manifest.version == 1
    assert manifest.project_slug == "agent-kai"
    assert manifest.epic_id == 10032
    assert manifest.task_id == 10279
    assert manifest.task_dir == str(paths.task_dir)
    assert manifest.shared_dir == str(paths.shared_dir)
    assert manifest.repos == [repo_manifest]
    assert repo_manifest.repo_key == "github.com__agent-kai-bot__agent-kai"
    assert repo_manifest.path == str(paths.repo_dir("developer", "github.com__agent-kai-bot__agent-kai"))
    assert repo_manifest.role == "developer"


@pytest.mark.parametrize("bad_path", ["../etc/passwd", "../../escape"])
def test_repo_manifest_rejects_relative_traversal_paths(bad_path: str) -> None:
    """Repo manifests reject relative paths containing traversal up front."""

    with pytest.raises(WorkspacePathError):
        RepoWorkspaceManifest(
            repo_key="github.com__agent-kai-bot__agent-kai",
            path=bad_path,
            role="developer",
        )


def test_ticket_manifest_rejects_relative_task_dir(tmp_path: Path) -> None:
    """Ticket manifest task_dir must be absolute under the resolved root."""

    with pytest.raises(WorkspacePathError):
        TicketWorkspaceManifest(
            version=1,
            project_slug="agent-kai",
            task_id=10279,
            epic_id=None,
            root=str(tmp_path),
            task_dir="../etc/passwd",
            shared_dir=str(tmp_path / "agent-kai" / "no-epic" / "task-10279" / "shared"),
        )


def test_ticket_manifest_rejects_paths_outside_root(tmp_path: Path) -> None:
    """Ticket manifests cannot trust root/task/shared paths that escape root."""

    with pytest.raises(WorkspacePathError):
        TicketWorkspaceManifest(
            version=1,
            project_slug="agent-kai",
            task_id=10279,
            epic_id=None,
            root=str(tmp_path),
            task_dir="/etc",
            shared_dir=str(tmp_path / "agent-kai" / "no-epic" / "task-10279" / "shared"),
        )


def test_ticket_manifest_rejects_shared_dir_outside_task_dir(tmp_path: Path) -> None:
    """shared_dir must stay inside the ticket task directory or be canonical shared."""

    task_dir = tmp_path / "agent-kai" / "no-epic" / "task-10279"
    with pytest.raises(WorkspacePathError):
        TicketWorkspaceManifest(
            version=1,
            project_slug="agent-kai",
            task_id=10279,
            epic_id=None,
            root=str(tmp_path),
            task_dir=str(task_dir),
            shared_dir=str(tmp_path / "agent-kai" / "no-epic" / "shared"),
        )


def test_ticket_manifest_rejects_repo_paths_outside_task_dir(tmp_path: Path) -> None:
    """Repo paths embedded in a ticket manifest must stay under the task dir."""

    paths = TicketWorkspacePaths(root=tmp_path, project_slug="agent-kai", task_id=10279)
    repo_manifest = RepoWorkspaceManifest(
        repo_key="github.com__agent-kai-bot__agent-kai",
        path="/etc",
        role="developer",
    )

    with pytest.raises(WorkspacePathError):
        TicketWorkspaceManifest.from_paths(paths, repos=[repo_manifest])


def test_safe_manifest_path_under_rejects_traversal_and_escape(tmp_path: Path) -> None:
    """The manifest path helper rejects ../../escape and absolute escapes."""

    with pytest.raises(WorkspacePathError):
        safe_manifest_path_under(tmp_path, "../../escape", "repo path")
    with pytest.raises(WorkspacePathError):
        safe_manifest_path_under(tmp_path, "/etc", "repo path")

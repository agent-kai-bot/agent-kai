"""Path-safe taskboard ticket workspace model.

This module resolves the configured workspace root, validates filesystem-safe
path components, derives repo keys, describes the on-disk layout, and prepares
git-cache backed role worktrees for taskboard tickets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_TICKET_WORKSPACE_ROOT = Path("/home/atc/workspaces/kai-tickets")
WORKSPACE_ROOT_ENV = "KAI_TICKET_WORKSPACE_ROOT"
DISK_WATERMARK_ENV = "KAI_TICKET_WORKSPACE_DISK_WATERMARK_PERCENT"
DEFAULT_DISK_WATERMARK_PERCENT = 85.0
ARCHIVE_EXCLUDES = (
    "node_modules",
    ".venv",
    "dist",
    "build",
    ".next",
    ".pytest_cache",
    "__pycache__",
)

_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_HOST_PART_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_REPO_URL_SUFFIX_RE = re.compile(r"\.git/?$")


class WorkspacePathError(ValueError):
    """Raised when workspace configuration or path input is unsafe."""


class WorkspaceGitError(RuntimeError):
    """Raised when git-backed workspace preparation fails."""


class WorkspaceDiskWatermarkError(WorkspaceGitError):
    """Raised when workspace creation is blocked by high disk usage."""


class WorkspaceGitCommandError(WorkspaceGitError):
    """Raised when an invoked git command fails."""

    def __init__(self, command: list[str], cwd: Path | None, stderr: str) -> None:
        self.command = command
        self.cwd = cwd
        self.stderr = redact_url_userinfo(stderr.strip())
        safe_command = [redact_url_userinfo(part) for part in command]
        location = f" in {cwd}" if cwd else ""
        message = f"git command failed{location}: {' '.join(safe_command)}: {self.stderr}"
        super().__init__(message)


class WorkspaceDirtyWrongBranchError(WorkspaceGitError):
    """Raised when an existing developer worktree is dirty on an unexpected branch."""

    def __init__(self, path: Path, expected_branch: str, actual_branch: str) -> None:
        self.path = path
        self.expected_branch = expected_branch
        self.actual_branch = actual_branch
        super().__init__(
            f"dirty developer worktree at {path} is on {actual_branch!r}; "
            f"expected {expected_branch!r}"
        )


@dataclass(frozen=True)
class PreparedRoleWorkspace:
    """Result returned by ``TicketWorkspaceManager.prepare_role_workspace``."""

    role: str
    repo: RepoRef
    repo_key: str
    cache_path: Path
    worktree_path: Path
    branch: str
    commit: str
    detached: bool
    reused: bool
    manifest: TicketWorkspaceManifest


@dataclass(frozen=True)
class WorkspaceLifecycleResult:
    """Result of archiving/deleting a task workspace."""

    task_dir: Path
    archived: bool
    deleted: bool
    archive_path: Path | None = None
    dirty: bool = False
    local_commits: bool = False
    pruned_repo_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskboardWorkspaceConfig:
    """Resolved taskboard workspace configuration."""

    root: Path = DEFAULT_TICKET_WORKSPACE_ROOT

    @classmethod
    def from_sources(
        cls,
        *,
        env: dict[str, str] | None = None,
        config: dict[str, Any] | None = None,
        config_path: str | Path | None = None,
    ) -> "TaskboardWorkspaceConfig":
        """Resolve config using env > agent-config.json > default order.

        Args:
            env: Environment mapping. Defaults to ``os.environ``.
            config: Already-loaded agent config dictionary.
            config_path: JSON config path used only when ``config`` is omitted.

        Returns:
            A workspace config with an absolute, normalized, traversal-safe root.
        """

        env_map = os.environ if env is None else env
        root_value = (env_map.get(WORKSPACE_ROOT_ENV) or "").strip()
        if not root_value:
            raw_config = config if config is not None else _load_json_config(config_path)
            taskboard_workspace = raw_config.get("taskboard_workspace", {})
            if isinstance(taskboard_workspace, dict):
                root_value = str(taskboard_workspace.get("root") or "").strip()
        root = Path(root_value) if root_value else DEFAULT_TICKET_WORKSPACE_ROOT
        return cls(root=safe_root_path(root))


@dataclass(frozen=True)
class RepoRef:
    """A repository identity resolved into a stable workspace key."""

    host: str
    owner: str
    repo: str
    url: str = ""
    default_branch: str = ""

    @property
    def key(self) -> str:
        """Return the path-safe ``host__owner__repo`` repo key."""

        return repo_key(self.host, self.owner, self.repo)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        default_branch: str = "",
    ) -> "RepoRef":
        """Parse common HTTPS/SSH git URLs into a repo reference."""

        host, owner, repo = parse_repo_url(url)
        return cls(
            host=host,
            owner=owner,
            repo=repo,
            url=redact_url_userinfo(url.strip()),
            default_branch=default_branch,
        )


@dataclass(frozen=True)
class TicketWorkspacePaths:
    """Canonical path model for one taskboard ticket workspace."""

    root: Path
    project_slug: str
    task_id: int
    epic_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", safe_root_path(self.root))
        object.__setattr__(self, "project_slug", safe_slug(self.project_slug, "project_slug"))
        object.__setattr__(self, "task_id", positive_int(self.task_id, "task_id"))
        if self.epic_id is not None:
            object.__setattr__(self, "epic_id", positive_int(self.epic_id, "epic_id"))

    @property
    def project_dir(self) -> Path:
        """Return ``<root>/<project-slug>``."""

        return safe_join(self.root, self.project_slug)

    @property
    def epic_dir(self) -> Path:
        """Return ``epic-<id>`` or ``no-epic`` below the project directory."""

        epic_component = f"epic-{self.epic_id}" if self.epic_id is not None else "no-epic"
        return safe_join(self.project_dir, epic_component)

    @property
    def task_dir(self) -> Path:
        """Return ``task-<id>`` below the epic directory."""

        return safe_join(self.epic_dir, f"task-{self.task_id}")

    @property
    def shared_dir(self) -> Path:
        """Return the shared handoff/artifact directory for this ticket."""

        return safe_join(self.task_dir, "shared")

    def role_dir(self, role: str) -> Path:
        """Return the canonical role directory below this ticket."""

        return safe_join(self.task_dir, role_slug(role))

    def repo_dir(self, role: str, repo: RepoRef | str) -> Path:
        """Return a role-scoped repo directory for ``repo``.

        ``repo`` may be a ``RepoRef`` or an already-derived repo key.
        """

        key = repo.key if isinstance(repo, RepoRef) else safe_repo_key(repo)
        return safe_join(self.role_dir(role), "repos", key)


@dataclass(frozen=True)
class RepoWorkspaceManifest:
    """Serializable manifest entry for a role-scoped repository workspace."""

    repo_key: str
    path: str
    repo_url: str = ""
    default_branch: str = ""
    branch: str = ""
    role: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_key", safe_repo_key(self.repo_key))
        object.__setattr__(self, "path", str(safe_manifest_repo_path(self.path, "repo path")))
        if self.role:
            object.__setattr__(self, "role", role_slug(self.role))


@dataclass(frozen=True)
class TicketWorkspaceManifest:
    """Serializable manifest for a taskboard ticket workspace."""

    version: int
    project_slug: str
    task_id: int
    epic_id: int | None
    root: str
    task_dir: str
    shared_dir: str
    repos: list[RepoWorkspaceManifest] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.version < 1:
            raise WorkspacePathError("manifest version must be positive")
        object.__setattr__(self, "project_slug", safe_slug(self.project_slug, "project_slug"))
        object.__setattr__(self, "task_id", positive_int(self.task_id, "task_id"))
        if self.epic_id is not None:
            object.__setattr__(self, "epic_id", positive_int(self.epic_id, "epic_id"))
        root = safe_root_path(self.root)
        task_dir = safe_manifest_path_under(root, self.task_dir, "task_dir")
        shared_dir = safe_manifest_path_under(root, self.shared_dir, "shared_dir")
        canonical_shared_dir = safe_join(task_dir, "shared")
        if shared_dir != canonical_shared_dir and task_dir not in shared_dir.parents:
            raise WorkspacePathError("shared_dir must be under task_dir or the canonical shared path")
        object.__setattr__(self, "root", str(root))
        object.__setattr__(self, "task_dir", str(task_dir))
        object.__setattr__(self, "shared_dir", str(shared_dir))
        object.__setattr__(
            self,
            "repos",
            [_validate_repo_manifest_under_task(repo, task_dir) for repo in self.repos],
        )

    @classmethod
    def from_paths(
        cls,
        paths: TicketWorkspacePaths,
        *,
        repos: list[RepoWorkspaceManifest] | None = None,
    ) -> "TicketWorkspaceManifest":
        """Build a manifest from a resolved path model."""

        return cls(
            version=1,
            project_slug=paths.project_slug,
            task_id=paths.task_id,
            epic_id=paths.epic_id,
            root=str(paths.root),
            task_dir=str(paths.task_dir),
            shared_dir=str(paths.shared_dir),
            repos=list(repos or []),
        )


class TicketWorkspaceManager:
    """Prepare git-backed, role-scoped taskboard workspaces.

    The manager uses one bare cache per repository under
    ``<root>/_git-cache/<repo-key>.git`` and role worktrees under
    ``<task>/<role>/repos/<repo-key>``.  Repository operations are serialized by
    an OS ``flock`` at ``<root>/_locks/repo/<repo-key>.lock``.
    """

    def __init__(self, paths: TicketWorkspacePaths) -> None:
        self.paths = paths

    def prepare_role_workspace(
        self,
        *,
        role: str,
        repo: RepoRef,
        branch: str,
        developer_commit: str | None = None,
        base_ref: str | None = None,
    ) -> PreparedRoleWorkspace:
        """Create or reuse a role worktree for ``repo``.

        Developer roles get a normal branch worktree and own ``branch``.
        Non-developer roles get a detached worktree at ``developer_commit``;
        when no explicit commit is provided they detach at the current branch
        tip from the shared cache.  Existing developer worktrees are idempotently
        reused if already on ``branch``.  A dirty existing developer worktree on
        any other branch fails with ``WorkspaceDirtyWrongBranchError``.
        """

        role_name = role_slug(role)
        if not repo.url:
            raise WorkspaceGitError("repo.url is required to prepare a git workspace")
        ensure_disk_below_watermark(self.paths.root)
        branch_name = _safe_git_ref_name(branch, "branch")
        repo_key_value = repo.key
        cache_path = safe_join(
            self.paths.root, "_git-cache", f"{repo_key_value}.git"
        )
        lock_path = safe_join(
            self.paths.root, "_locks", "repo", f"{repo_key_value}.lock"
        )
        worktree_path = self.paths.repo_dir(role_name, repo)
        is_developer = role_name == "developer"

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self.paths.shared_dir.mkdir(parents=True, exist_ok=True)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_cache(repo, cache_path)

            if is_developer:
                reused = self._prepare_developer_worktree(
                    cache_path=cache_path,
                    worktree_path=worktree_path,
                    branch=branch_name,
                    base_ref=base_ref or repo.default_branch or "main",
                )
                detached = False
            else:
                checkout_ref = developer_commit or branch_name
                _safe_git_ref_name(checkout_ref, "developer commit")
                reused = self._prepare_detached_worktree(
                    cache_path=cache_path,
                    worktree_path=worktree_path,
                    checkout_ref=checkout_ref,
                )
                detached = True

            commit = _git_stdout(["rev-parse", "HEAD"], cwd=worktree_path)
            manifest = self._write_manifest(
                RepoWorkspaceManifest(
                    repo_key=repo_key_value,
                    path=str(worktree_path),
                    repo_url=repo.url,
                    default_branch=repo.default_branch,
                    branch=branch_name,
                    role=role_name,
                )
            )
            return PreparedRoleWorkspace(
                role=role_name,
                repo=repo,
                repo_key=repo_key_value,
                cache_path=cache_path,
                worktree_path=worktree_path,
                branch=branch_name,
                commit=commit,
                detached=detached,
                reused=reused,
                manifest=manifest,
            )

    def _ensure_cache(self, repo: RepoRef, cache_path: Path) -> None:
        if (cache_path / "HEAD").exists():
            _git(
                ["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"],
                cwd=cache_path,
            )
            return
        if cache_path.exists() and any(cache_path.iterdir()):
            raise WorkspaceGitError(
                f"git cache path exists but is not a bare repository: {cache_path}"
            )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--bare", repo.url, str(cache_path)], cwd=None)
        _git(
            ["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"],
            cwd=cache_path,
        )

    def _prepare_developer_worktree(
        self,
        *,
        cache_path: Path,
        worktree_path: Path,
        branch: str,
        base_ref: str,
    ) -> bool:
        if (worktree_path / ".git").exists():
            actual_branch = _git_stdout(
                ["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path
            )
            if actual_branch == branch:
                return True
            if _is_dirty(worktree_path):
                raise WorkspaceDirtyWrongBranchError(worktree_path, branch, actual_branch)
            _git(["checkout", branch], cwd=worktree_path)
            return True

        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        start_ref = _resolve_cache_ref(cache_path, branch) or _resolve_cache_ref(
            cache_path, base_ref
        )
        if start_ref is None:
            start_ref = (
                _resolve_cache_ref(cache_path, f"origin/{base_ref}") or "HEAD"
            )
        _git(
            ["worktree", "add", "-B", branch, str(worktree_path), start_ref],
            cwd=cache_path,
        )
        return False

    def _prepare_detached_worktree(
        self,
        *,
        cache_path: Path,
        worktree_path: Path,
        checkout_ref: str,
    ) -> bool:
        resolved = _resolve_cache_ref(cache_path, checkout_ref) or checkout_ref
        if (worktree_path / ".git").exists():
            current = _git_stdout(["rev-parse", "HEAD"], cwd=worktree_path)
            target = _git_stdout(["rev-parse", resolved], cwd=cache_path)
            if current == target and not _is_dirty(worktree_path):
                return True
            if _is_dirty(worktree_path):
                raise WorkspaceGitError(
                    f"existing reviewer worktree is dirty: {worktree_path}"
                )
            _git(["checkout", "--detach", target], cwd=worktree_path)
            return True

        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        _git(
            ["worktree", "add", "--detach", str(worktree_path), resolved],
            cwd=cache_path,
        )
        return False

    def _write_manifest(self, repo_manifest: RepoWorkspaceManifest) -> TicketWorkspaceManifest:
        manifest = TicketWorkspaceManifest.from_paths(self.paths, repos=[repo_manifest])
        manifest_path = safe_join(self.paths.shared_dir, "workspace-manifest.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_manifest(manifest_path)
        repos = {repo.repo_key: repo for repo in (existing.repos if existing else [])}
        repos[repo_manifest.repo_key] = repo_manifest
        manifest = TicketWorkspaceManifest.from_paths(
            self.paths, repos=list(repos.values())
        )
        data = asdict(manifest)
        manifest_path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return manifest

    def archive_done_workspace(
        self, *, now: datetime | None = None
    ) -> WorkspaceLifecycleResult:
        """Archive a Done task workspace without deleting the live task directory."""

        archive_path = archive_task_workspace(self.paths, now=now)
        return WorkspaceLifecycleResult(
            task_dir=self.paths.task_dir,
            archived=True,
            deleted=False,
            archive_path=archive_path,
            dirty=workspace_has_dirty_changes(self.paths.task_dir),
            local_commits=workspace_has_local_commits(self.paths.task_dir),
        )

    def cleanup_cancelled_workspace(
        self, *, now: datetime | None = None
    ) -> WorkspaceLifecycleResult:
        """Delete a Cancelled task workspace, archiving first when it has local state."""

        task_dir = self.paths.task_dir
        dirty = workspace_has_dirty_changes(task_dir)
        local_commits = workspace_has_local_commits(task_dir)
        archive_path = (
            archive_task_workspace(self.paths, now=now)
            if (dirty or local_commits) and task_dir.exists()
            else None
        )
        repo_keys = tuple(sorted(discover_workspace_repo_keys(task_dir)))
        if task_dir.exists():
            shutil.rmtree(task_dir)
        pruned = prune_workspace_repos(self.paths.root, repo_keys)
        return WorkspaceLifecycleResult(
            task_dir=task_dir,
            archived=archive_path is not None,
            deleted=True,
            archive_path=archive_path,
            dirty=dirty,
            local_commits=local_commits,
            pruned_repo_keys=tuple(sorted(pruned)),
        )


def _git(command: list[str], *, cwd: Path | None) -> None:
    full_command = ["git", *command]
    result = subprocess.run(
        full_command,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceGitCommandError(full_command, cwd, result.stderr or result.stdout)


def _git_stdout(command: list[str], *, cwd: Path | None) -> str:
    full_command = ["git", *command]
    result = subprocess.run(
        full_command,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceGitCommandError(full_command, cwd, result.stderr or result.stdout)
    return result.stdout.strip()


def _is_dirty(worktree_path: Path) -> bool:
    return bool(_git_stdout(["status", "--porcelain"], cwd=worktree_path))


def _resolve_cache_ref(cache_path: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=str(cache_path),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _safe_git_ref_name(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceGitError(f"{field_name} is required")
    ref = value.strip()
    if (
        ref.startswith("-")
        or ".." in ref
        or "\x00" in ref
        or any(ch.isspace() for ch in ref)
    ):
        raise WorkspaceGitError(f"unsafe {field_name}: {value!r}")
    return ref


def _load_json_config(config_path: str | Path | None) -> dict[str, Any]:
    if config_path is None:
        config_path = Path(__file__).resolve().parents[1] / "agent-config.json"
    path = Path(config_path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise WorkspacePathError("agent config must be a JSON object")
    return loaded


def disk_watermark_percent(env: dict[str, str] | None = None) -> float:
    env_map = os.environ if env is None else env
    raw = str(env_map.get(DISK_WATERMARK_ENV, "")).strip()
    if not raw:
        return DEFAULT_DISK_WATERMARK_PERCENT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DISK_WATERMARK_PERCENT
    return value if 0 < value <= 100 else DEFAULT_DISK_WATERMARK_PERCENT


def ensure_disk_below_watermark(root: str | Path, *, threshold_percent: float | None = None) -> None:
    safe_root = safe_root_path(root)
    probe = safe_root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    used_percent = (usage.used / usage.total) * 100 if usage.total else 100.0
    threshold = disk_watermark_percent() if threshold_percent is None else float(threshold_percent)
    if used_percent >= threshold:
        raise WorkspaceDiskWatermarkError(
            f"ticket workspace disk usage {used_percent:.1f}% is at/above "
            f"{threshold:.1f}% watermark for {safe_root}"
        )


def archive_task_workspace(paths: TicketWorkspacePaths, *, now: datetime | None = None) -> Path:
    if not paths.task_dir.exists():
        raise WorkspacePathError(f"task workspace does not exist: {paths.task_dir}")
    timestamp = now or datetime.now(timezone.utc)
    archive_dir = safe_join(paths.root, "_archive", f"{timestamp.year:04d}", f"{timestamp.month:02d}", paths.project_slug)
    archive_dir.mkdir(parents=True, exist_ok=True)
    zstd_path = archive_dir / f"task-{paths.task_id}.tar.zst"
    gzip_path = archive_dir / f"task-{paths.task_id}.tar.gz"
    if shutil.which("zstd") and shutil.which("tar"):
        _archive_with_tar_zstd(paths.task_dir, zstd_path)
        return zstd_path
    _archive_with_tarfile_gzip(paths.task_dir, gzip_path)
    return gzip_path


def _archive_with_tar_zstd(task_dir: Path, archive_path: Path) -> None:
    excludes = [f"--exclude={name}" for name in ARCHIVE_EXCLUDES]
    tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    command = [
        "tar",
        "--zstd",
        *excludes,
        "-cf",
        str(tmp_path),
        "-C",
        str(task_dir.parent),
        task_dir.name,
    ]
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise WorkspaceGitError(f"archive command failed: {result.stderr or result.stdout}")
    tmp_path.replace(archive_path)


def _archive_with_tarfile_gzip(task_dir: Path, archive_path: Path) -> None:
    tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    with tarfile.open(tmp_path, "w:gz") as tar:
        tar.add(task_dir, arcname=task_dir.name, filter=_tar_exclude_filter)
    tmp_path.replace(archive_path)


def _tar_exclude_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if any(part in ARCHIVE_EXCLUDES for part in Path(info.name).parts):
        return None
    return info


def workspace_has_dirty_changes(task_dir: Path) -> bool:
    return any(
        _repo_dirty(repo_path) for repo_path in discover_workspace_repo_paths(task_dir)
    )


def workspace_has_local_commits(task_dir: Path) -> bool:
    return any(
        _repo_has_local_commits(repo_path)
        for repo_path in discover_workspace_repo_paths(task_dir)
    )


def discover_workspace_repo_paths(task_dir: Path) -> list[Path]:
    if not task_dir.exists():
        return []
    return sorted(path for path in task_dir.glob("*/repos/*") if (path / ".git").exists())


def discover_workspace_repo_keys(task_dir: Path) -> set[str]:
    keys: set[str] = set()
    for repo_path in discover_workspace_repo_paths(task_dir):
        try:
            keys.add(safe_repo_key(repo_path.name))
        except WorkspacePathError:
            continue
    manifest = _read_manifest(task_dir / "shared" / "workspace-manifest.json")
    if manifest:
        keys.update(repo.repo_key for repo in manifest.repos)
    return keys


def prune_workspace_repos(root: Path, repo_keys: tuple[str, ...]) -> set[str]:
    pruned: set[str] = set()
    for key in repo_keys:
        lock_path = safe_join(root, "_locks", "repo", key + ".lock")
        cache_path = safe_join(root, "_git-cache", key + ".git")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if cache_path.exists():
                _git(["worktree", "prune"], cwd=cache_path)
                pruned.add(key)
    return pruned


def _repo_dirty(repo_path: Path) -> bool:
    try:
        return bool(_git_stdout(["status", "--porcelain"], cwd=repo_path))
    except WorkspaceGitError:
        return True


def _repo_has_local_commits(repo_path: Path) -> bool:
    try:
        current = _git_stdout(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
        if current == "HEAD":
            return False
        upstream = _git_stdout(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=repo_path,
        )
        return bool(
            _git_stdout(["log", "--format=%H", f"{upstream}..HEAD", "--"], cwd=repo_path)
        )
    except WorkspaceGitError:
        try:
            return bool(
                _git_stdout(
                    ["log", "--format=%H", "--branches", "--not", "--remotes", "--"],
                    cwd=repo_path,
                )
            )
        except WorkspaceGitError:
            return False


def _read_manifest(manifest_path: Path) -> TicketWorkspaceManifest | None:
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        repos = [RepoWorkspaceManifest(**repo) for repo in data.get("repos", [])]
        return TicketWorkspaceManifest(
            version=int(data["version"]),
            project_slug=str(data["project_slug"]),
            task_id=int(data["task_id"]),
            epic_id=data.get("epic_id"),
            root=str(data["root"]),
            task_dir=str(data["task_dir"]),
            shared_dir=str(data["shared_dir"]),
            repos=repos,
        )
    except Exception as exc:  # noqa: BLE001
        raise WorkspacePathError(f"invalid workspace manifest: {manifest_path}: {exc}") from exc


def positive_int(value: int, name: str) -> int:
    """Return a positive integer or raise ``WorkspacePathError``."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WorkspacePathError(f"{name} must be a positive integer")
    return value


def safe_root_path(path: str | Path) -> Path:
    """Normalize and validate a configured absolute workspace root."""

    if path is None:
        raise WorkspacePathError("workspace root is required")
    raw = str(path).strip()
    if not raw:
        raise WorkspacePathError("workspace root is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise WorkspacePathError("workspace root must be absolute")
    if any(part in ("", ".", "..") for part in candidate.parts[1:]):
        raise WorkspacePathError("workspace root must not contain traversal segments")
    return candidate.resolve(strict=False)


def safe_manifest_path(path: str | Path, field_name: str = "manifest path") -> Path:
    """Normalize an absolute manifest path and reject traversal inputs."""

    if path is None:
        raise WorkspacePathError(f"{field_name} is required")
    raw = str(path).strip()
    if not raw:
        raise WorkspacePathError(f"{field_name} is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise WorkspacePathError(f"{field_name} must be absolute")
    if any(part in ("", ".", "..") for part in candidate.parts[1:]):
        raise WorkspacePathError(f"{field_name} must not contain traversal segments")
    return candidate.resolve(strict=False)


def safe_manifest_path_under(root: str | Path, path: str | Path, field_name: str) -> Path:
    """Normalize an absolute manifest path and require it below ``root``."""

    safe_root = safe_root_path(root)
    candidate = safe_manifest_path(path, field_name)
    if candidate != safe_root and safe_root not in candidate.parents:
        raise WorkspacePathError(f"{field_name} escapes manifest root")
    return candidate


def safe_manifest_repo_path(path: str | Path, field_name: str = "repo path") -> Path:
    """Normalize a repo manifest path, rejecting relative traversal."""

    if path is None:
        raise WorkspacePathError(f"{field_name} is required")
    raw = str(path).strip()
    if not raw:
        raise WorkspacePathError(f"{field_name} is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() and any(part in ("", ".", "..") for part in candidate.parts):
        raise WorkspacePathError(f"{field_name} must not contain traversal segments")
    return candidate.resolve(strict=False) if candidate.is_absolute() else candidate


def _validate_repo_manifest_under_task(
    repo: RepoWorkspaceManifest,
    task_dir: Path,
) -> RepoWorkspaceManifest:
    """Require repo manifest paths to stay below the ticket task directory."""

    repo_path = safe_manifest_path_under(task_dir, repo.path, "repo path")
    if repo.role:
        role_repo_root = safe_join(task_dir, repo.role, "repos")
        if role_repo_root not in repo_path.parents:
            raise WorkspacePathError("repo path must be under role repos directory")
    return RepoWorkspaceManifest(
        repo_key=repo.repo_key,
        path=str(repo_path),
        repo_url=repo.repo_url,
        default_branch=repo.default_branch,
        branch=repo.branch,
        role=repo.role,
    )


def safe_slug(value: str, field_name: str = "slug") -> str:
    """Validate a lowercase path component slug.

    Accepted slugs contain only ``a-z``, ``0-9``, ``_`` and ``-``; they must
    start with an alphanumeric character.  Separators, dots, empty values and
    traversal segments are rejected instead of normalized.
    """

    if not isinstance(value, str):
        raise WorkspacePathError(f"{field_name} must be a string")
    slug = value.strip()
    if not _SAFE_SLUG_RE.fullmatch(slug):
        raise WorkspacePathError(f"{field_name} is not a safe slug: {value!r}")
    if slug in {".", ".."} or "/" in slug or "\\" in slug:
        raise WorkspacePathError(f"{field_name} must be a single path component")
    return slug


def role_slug(value: str) -> str:
    """Validate and return a canonical lower-case role slug."""

    if not isinstance(value, str):
        raise WorkspacePathError("role must be a string")
    return safe_slug(value.strip().lower(), "role")


def repo_key(host: str, owner: str, repo: str) -> str:
    """Return a stable path-safe ``host__owner__repo`` key."""

    safe_host = _safe_host(host)
    safe_owner = safe_slug(owner.lower(), "repo owner")
    safe_repo = safe_slug(_REPO_URL_SUFFIX_RE.sub("", repo.lower()), "repo name")
    return f"{safe_host}__{safe_owner}__{safe_repo}"


def safe_repo_key(value: str) -> str:
    """Validate an existing ``host__owner__repo`` key."""

    if not isinstance(value, str):
        raise WorkspacePathError("repo key must be a string")
    parts = value.split("__")
    if len(parts) != 3:
        raise WorkspacePathError("repo key must use host__owner__repo format")
    return repo_key(parts[0], parts[1], parts[2])


def redact_url_userinfo(value: str) -> str:
    """Return ``value`` with URL username/password userinfo removed."""

    text = str(value)
    return re.sub(
        r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^\s/@]+@)",
        r"\g<scheme>[REDACTED]@",
        text,
        flags=re.IGNORECASE,
    )


def parse_repo_url(url: str) -> tuple[str, str, str]:
    """Parse a git remote URL into ``(host, owner, repo)``.

    Supports HTTPS URLs, ``git@host:owner/repo.git`` SCP-like SSH URLs, and
    ``ssh://git@host/owner/repo.git`` URLs.
    """

    if not isinstance(url, str) or not url.strip():
        raise WorkspacePathError("repo URL is required")
    raw = url.strip()

    scp_match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):([^/]+)/([^/]+)", raw)
    if scp_match and "://" not in raw:
        host, owner, repo = scp_match.groups()
        return _safe_host(host), safe_slug(owner.lower(), "repo owner"), safe_slug(
            _REPO_URL_SUFFIX_RE.sub("", repo.lower()), "repo name"
        )

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
        raise WorkspacePathError("unsupported repo URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise WorkspacePathError("repo URL path must include owner and repo")
    owner, repo = parts[-2], parts[-1]
    return _safe_host(parsed.hostname), safe_slug(owner.lower(), "repo owner"), safe_slug(
        _REPO_URL_SUFFIX_RE.sub("", repo.lower()), "repo name"
    )


def safe_join(root: Path, *components: str) -> Path:
    """Join validated components and guarantee the result remains under root."""

    safe_root = safe_root_path(root)
    candidate = safe_root.joinpath(*components).resolve(strict=False)
    if candidate != safe_root and safe_root not in candidate.parents:
        raise WorkspacePathError("resolved path escapes workspace root")
    return candidate


def _safe_host(value: str) -> str:
    if not isinstance(value, str):
        raise WorkspacePathError("repo host must be a string")
    host = value.strip().lower()
    if not _HOST_PART_RE.fullmatch(host) or ".." in host:
        raise WorkspacePathError(f"repo host is not safe: {value!r}")
    return host

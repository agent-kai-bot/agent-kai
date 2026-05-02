"""Path-safe taskboard ticket workspace model.

This module intentionally performs no git operations.  It only resolves the
configured workspace root, validates filesystem-safe path components, derives
repo keys, and describes the on-disk layout used by later workspace lifecycle
code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_TICKET_WORKSPACE_ROOT = Path("/home/atc/workspaces/kai-tickets")
WORKSPACE_ROOT_ENV = "KAI_TICKET_WORKSPACE_ROOT"

_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_HOST_PART_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_REPO_URL_SUFFIX_RE = re.compile(r"\.git/?$")


class WorkspacePathError(ValueError):
    """Raised when workspace configuration or path input is unsafe."""


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
            url=url,
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

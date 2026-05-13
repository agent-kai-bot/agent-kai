"""Per-session git worktree management for taskboard-spawned runs."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import fcntl

LOGGER = logging.getLogger(__name__)
SESSIONS_ROOT = Path("/tmp/kai/sessions")
REPO_CACHE_ROOT = Path("/tmp/kai/taskboard-repos")
REPO_CACHE_LOCK_ROOT = REPO_CACHE_ROOT / ".locks"


class WorktreeManager:
    """Create and clean up isolated git worktrees for agent sessions."""

    def __init__(
        self,
        repo_root: Path,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.extra_env = dict(extra_env or {})

    def create(
        self,
        session_id: str,
        branch_name: str,
        base_branch: str = "main",
        *,
        extra_env: dict[str, str] | None = None,
    ) -> Path:
        """Create an isolated worktree for ``session_id``.

        Retries once with ``git worktree prune`` when the initial add fails,
        which makes stale registration collisions self-heal.
        """

        path = self.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "git",
            "-C",
            str(self.repo_root),
            "worktree",
            "add",
            str(path),
            "-b",
            branch_name,
            base_branch,
        ]
        try:
            self._run(cmd, extra_env=extra_env)
        except subprocess.CalledProcessError:
            self._run(
                ["git", "-C", str(self.repo_root), "worktree", "prune"],
                extra_env=extra_env,
            )
            self._run(cmd, extra_env=extra_env)
        return path

    def cleanup(self, session_id: str) -> None:
        """Best-effort removal of a session worktree and its local branch."""

        path = self.path_for(session_id)
        branch_name = self._branch_name_for_path(path)
        self._best_effort_run(
            ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(path)]
        )
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        if branch_name and not self._branch_has_upstream(branch_name):
            self._best_effort_run(
                ["git", "-C", str(self.repo_root), "branch", "-D", branch_name]
            )

    def path_for(self, session_id: str) -> Path:
        """Return the stable filesystem path for ``session_id``."""

        return SESSIONS_ROOT / str(session_id)

    @classmethod
    def ensure_repo_clone(
        cls,
        repo_url: str,
        *,
        repo_key: str | None = None,
        default_branch: str = "main",
        extra_env: dict[str, str] | None = None,
    ) -> Path:
        """Ensure a dispatcher-managed primary clone exists for ``repo_url``."""

        sanitized_key = cls.repo_key_for_url(repo_url, fallback=repo_key or "repo")
        repo_path = REPO_CACHE_ROOT / sanitized_key
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        with cls._repo_lock(sanitized_key):
            if not (repo_path / ".git").exists():
                if repo_path.exists():
                    shutil.rmtree(repo_path, ignore_errors=True)
                cls._run_static(
                    ["git", "clone", repo_url, str(repo_path)],
                    extra_env=extra_env,
                )
            else:
                cls._run_static(
                    ["git", "-C", str(repo_path), "fetch", "--prune", "origin"],
                    extra_env=extra_env,
                )

            base_branch = (default_branch or "main").strip() or "main"
            cls._run_static(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "fetch",
                    "origin",
                    base_branch,
                ],
                extra_env=extra_env,
            )
        return repo_path

    @classmethod
    def write_workspace_manifest(
        cls,
        worktree_path: Path,
        *,
        task_id: int | str | None,
        session_id: str,
        fire_generation: int | None,
        agent_id: str,
        role: str,
        primary_repo_path: Path,
        repo_url: str,
        default_branch: str,
        repo_routing_mode: str,
        source: str = "",
        repo_key: str = "",
    ) -> Path:
        """Write a per-session workspace manifest and return its path."""

        manifest_path = Path(worktree_path) / ".kai" / "workspace-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "task_id": task_id,
            "session_id": session_id,
            "fire_generation": fire_generation,
            "agent_id": agent_id,
            "role": role,
            "repo": {
                "repo_key": repo_key or cls.repo_key_for_url(repo_url, fallback="repo"),
                "repo_url": repo_url,
                "default_branch": default_branch,
                "source": source,
                "routing_mode": repo_routing_mode,
            },
            "paths": {
                "primary_repo_path": str(primary_repo_path),
                "worktree_path": str(worktree_path),
            },
        }
        manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return manifest_path

    @staticmethod
    def repo_key_for_url(repo_url: str, *, fallback: str = "repo") -> str:
        """Derive a stable filesystem-safe key from a repository URL."""

        value = (repo_url or "").strip()
        if not value:
            return fallback
        normalized = value
        if ":" in value and "://" not in value and "@" in value:
            _, tail = value.split(":", 1)
            normalized = tail
        else:
            split = urlsplit(value)
            normalized = f"{split.netloc}{split.path}" if split.netloc else split.path or value
        normalized = normalized.strip().rstrip("/")
        if normalized.endswith(".git"):
            normalized = normalized[:-4]
        key = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-._").lower()
        return key or fallback

    @classmethod
    @contextmanager
    def _repo_lock(cls, repo_key: str):
        """Serialize clone-cache bootstrap/fetch work per repo key."""

        lock_root = REPO_CACHE_LOCK_ROOT
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / f"{repo_key}.lock"
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _run(
        self,
        cmd: list[str],
        *,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        overlay = dict(self.extra_env)
        overlay.update(extra_env or {})
        self._run_static(cmd, extra_env=overlay)

    @staticmethod
    def _run_static(
        cmd: list[str],
        *,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "check": True,
            "capture_output": True,
            "text": True,
        }
        overlay = {str(key): str(value) for key, value in dict(extra_env or {}).items()}
        if overlay:
            env = os.environ.copy()
            env.update(overlay)
            kwargs["env"] = env
        subprocess.run(cmd, **kwargs)

    def _best_effort_run(self, cmd: list[str]) -> None:
        try:
            self._run(cmd)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("worktree cleanup command failed cmd=%s error=%s", cmd, exc)

    def _branch_name_for_path(self, path: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo_root), "worktree", "list", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
                **(
                    {"env": {**os.environ, **self.extra_env}}
                    if self.extra_env
                    else {}
                ),
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("worktree list failed path=%s error=%s", path, exc)
            return None

        target = str(path)
        current_path: str | None = None
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("worktree "):
                current_path = line.removeprefix("worktree ").strip()
                continue
            if current_path == target and line.startswith("branch refs/heads/"):
                return line.removeprefix("branch refs/heads/").strip() or None
        return None

    def _branch_has_upstream(self, branch_name: str) -> bool:
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "rev-parse",
                    "--abbrev-ref",
                    f"{branch_name}@{{upstream}}",
                ],
                check=True,
                capture_output=True,
                text=True,
                **(
                    {"env": {**os.environ, **self.extra_env}}
                    if self.extra_env
                    else {}
                ),
            )
        except Exception:
            return False
        return True

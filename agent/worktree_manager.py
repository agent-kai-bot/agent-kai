"""Per-session git worktree management for taskboard-spawned runs."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

LOGGER = logging.getLogger(__name__)
SESSIONS_ROOT = Path("/tmp/kai/sessions")


class WorktreeManager:
    """Create and clean up isolated git worktrees for agent sessions."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)

    def create(
        self,
        session_id: str,
        branch_name: str,
        base_branch: str = "main",
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
            self._run(cmd)
        except subprocess.CalledProcessError:
            self._run(["git", "-C", str(self.repo_root), "worktree", "prune"])
            self._run(cmd)
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

    def _run(self, cmd: list[str]) -> None:
        subprocess.run(cmd, check=True, capture_output=True, text=True)

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
            )
        except Exception:
            return False
        return True

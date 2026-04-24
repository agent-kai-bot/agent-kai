"""Typed Git and Forgejo tools for taskboard-driven SDLC work."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from langchain_core.tools import StructuredTool


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_RUN_ROOT = "/tmp/taskboard-agent-runs"


def _run_root() -> Path:
    """Return the root directory for task workspaces.

    Returns:
        Configured run workspace root.
    """

    return Path(os.getenv("TASKBOARD_RUN_ROOT", DEFAULT_RUN_ROOT)).expanduser()


def _forgejo_base_url() -> str:
    """Return the configured Forgejo base URL.

    Returns:
        Base URL without trailing slash.
    """

    return os.getenv("FORGEJO_URL", os.getenv("GITEA_URL", "")).rstrip("/")


def _forgejo_token() -> str:
    """Return the configured Forgejo token.

    Returns:
        Token string, or an empty string when not configured.
    """

    return (
        os.getenv("FORGEJO_TOKEN", "").strip()
        or os.getenv("GITEA_TOKEN", "").strip()
    )


def _redact(text: str) -> str:
    """Redact configured secrets from output.

    Args:
        text: Candidate output text.

    Returns:
        Redacted text.
    """

    redacted = text
    for secret in (_forgejo_token(),):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _slug(value: str) -> str:
    """Convert a string into a filesystem-safe slug.

    Args:
        value: Candidate slug text.

    Returns:
        Filesystem-safe slug.
    """

    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "task"


def _safe_workspace_path(path: str | Path) -> Path:
    """Validate that a workspace path stays under the run root.

    Args:
        path: Candidate path.

    Returns:
        Resolved path.

    Raises:
        ValueError: If the path escapes the run root.
    """

    root = _run_root().resolve()
    candidate = Path(path).expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path is outside task run root: {candidate}") from exc
    return candidate


def _format_result(
    *,
    ok: bool,
    data: dict[str, Any] | None = None,
    error: str | None = None,
) -> str:
    """Format a tool result as redacted JSON.

    Args:
        ok: Whether the operation succeeded.
        data: Optional success payload.
        error: Optional error string.

    Returns:
        Redacted JSON result.
    """

    payload: dict[str, Any] = {"ok": ok}
    if data:
        payload.update(data)
    if error:
        payload["error"] = error
    return _redact(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _run_git(args: list[str], cwd: str | Path) -> str:
    """Run a git command in a validated workspace.

    Args:
        args: Git subcommand arguments.
        cwd: Workspace path under the run root.

    Returns:
        Redacted JSON command result.
    """

    try:
        workspace = _safe_workspace_path(cwd)
        result = subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return _format_result(ok=False, error=str(exc))

    return _format_result(
        ok=result.returncode == 0,
        data={
            "returncode": result.returncode,
            "stdout": result.stdout[-10_000:],
            "stderr": result.stderr[-10_000:],
        },
    )


def git_prepare_task_workspace(
    repo_url: str,
    task_id: int,
    branch_name: str,
    base_branch: str = "main",
    run_id: str = "",
) -> str:
    """Clone a repo into a guarded task workspace and create a branch.

    Args:
        repo_url: Git clone URL.
        task_id: Taskboard task id.
        branch_name: New branch name to create.
        base_branch: Base branch to check out first.
        run_id: Optional gateway run id for workspace uniqueness.

    Returns:
        Redacted JSON result containing the workspace path.
    """

    if not repo_url.strip():
        return _format_result(ok=False, error="repo_url is required")
    if not branch_name.strip():
        return _format_result(ok=False, error="branch_name is required")
    if branch_name in {"main", "master"}:
        return _format_result(ok=False, error="refusing direct protected branch work")

    root = _run_root()
    root.mkdir(parents=True, exist_ok=True)
    suffix = _slug(run_id or branch_name)
    workspace = root / f"task-{int(task_id)}-{suffix}"

    if workspace.exists():
        return _format_result(ok=False, error=f"workspace already exists: {workspace}")

    try:
        clone = subprocess.run(
            ["git", "clone", "--branch", base_branch, repo_url, str(workspace)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if clone.returncode != 0:
            return _format_result(
                ok=False,
                data={
                    "returncode": clone.returncode,
                    "stdout": clone.stdout[-10_000:],
                    "stderr": clone.stderr[-10_000:],
                },
            )
        branch = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
        return _format_result(
            ok=branch.returncode == 0,
            data={
                "workspace": str(workspace),
                "branch": branch_name,
                "returncode": branch.returncode,
                "stdout": branch.stdout[-10_000:],
                "stderr": branch.stderr[-10_000:],
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _format_result(ok=False, error=str(exc))


def git_status(workspace: str) -> str:
    """Return git status for a task workspace.

    Args:
        workspace: Task workspace path under the run root.

    Returns:
        Redacted JSON result.
    """

    return _run_git(["status", "--short"], workspace)


def git_diff_summary(workspace: str) -> str:
    """Return a compact diff summary for a task workspace.

    Args:
        workspace: Task workspace path under the run root.

    Returns:
        Redacted JSON result.
    """

    return _run_git(["diff", "--stat"], workspace)


def git_commit(workspace: str, message: str) -> str:
    """Commit all staged and unstaged workspace changes.

    Args:
        workspace: Task workspace path under the run root.
        message: Commit message.

    Returns:
        Redacted JSON result.
    """

    if not message.strip():
        return _format_result(ok=False, error="commit message is required")
    try:
        workspace_path = _safe_workspace_path(workspace)
        add_result = subprocess.run(
            ["git", "add", "-A"],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return _format_result(ok=False, error=str(exc))
    if add_result.returncode != 0:
        return _format_result(
            ok=False,
            data={
                "returncode": add_result.returncode,
                "stdout": add_result.stdout,
                "stderr": add_result.stderr,
            },
        )
    return _run_git(["commit", "-m", message], workspace_path)


def git_push_branch(workspace: str, branch_name: str) -> str:
    """Push a task branch to origin.

    Args:
        workspace: Task workspace path under the run root.
        branch_name: Branch to push.

    Returns:
        Redacted JSON result.
    """

    if branch_name in {"main", "master"}:
        return _format_result(ok=False, error="refusing to push protected branch")
    return _run_git(["push", "-u", "origin", branch_name], workspace)


def _forgejo_request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> str:
    """Send a Forgejo API request.

    Args:
        method: HTTP method.
        path: API path beginning with ``/``.
        json_body: Optional JSON body.

    Returns:
        Redacted JSON result.
    """

    base_url = _forgejo_base_url()
    token = _forgejo_token()
    if not base_url:
        return _format_result(ok=False, error="FORGEJO_URL is not configured")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        response = requests.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            json=json_body,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        return _format_result(
            ok=200 <= response.status_code < 300,
            data={"status_code": response.status_code, "body": body},
        )
    except requests.RequestException as exc:
        return _format_result(ok=False, error=str(exc))


def forgejo_create_pr(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str = "main",
    body: str = "",
) -> str:
    """Create a Forgejo pull request.

    Args:
        owner: Repository owner.
        repo: Repository name.
        title: Pull request title.
        head: Head branch.
        base: Base branch.
        body: Pull request body.

    Returns:
        Redacted JSON result.
    """

    path = f"/api/v1/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls"
    return _forgejo_request(
        "POST",
        path,
        json_body={"title": title, "head": head, "base": base, "body": body},
    )


def forgejo_find_pr_for_branch(owner: str, repo: str, branch: str) -> str:
    """Find an open Forgejo PR by head branch.

    Args:
        owner: Repository owner.
        repo: Repository name.
        branch: Head branch.

    Returns:
        Redacted JSON result containing matching PRs.
    """

    path = (
        f"/api/v1/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        "/pulls?state=open"
    )
    raw = _forgejo_request("GET", path)
    payload = json.loads(raw)
    body = payload.get("body")
    matches = []
    if isinstance(body, list):
        for item in body:
            head = item.get("head") if isinstance(item, dict) else None
            if isinstance(head, dict) and head.get("ref") == branch:
                matches.append(item)
    return _format_result(ok=bool(payload.get("ok")), data={"matches": matches})


def forgejo_submit_review(
    owner: str,
    repo: str,
    pr_number: int,
    event: str,
    body: str,
) -> str:
    """Submit a formal Forgejo pull-request review.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_number: Pull request number.
        event: Review event: ``APPROVED`` or ``REQUEST_CHANGES``.
        body: Review body.

    Returns:
        Redacted JSON result.
    """

    normalized = event.strip().upper()
    if normalized not in {"APPROVED", "REQUEST_CHANGES"}:
        return _format_result(
            ok=False,
            error="event must be APPROVED or REQUEST_CHANGES",
        )
    path = (
        f"/api/v1/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/{int(pr_number)}/reviews"
    )
    return _forgejo_request(
        "POST",
        path,
        json_body={"event": normalized, "body": body},
    )


def create_forgejo_tools() -> list[StructuredTool]:
    """Create Git and Forgejo structured tools.

    Returns:
        List of LangChain tools for guarded git and Forgejo operations.
    """

    return [
        StructuredTool.from_function(
            func=git_prepare_task_workspace,
            name="git_prepare_task_workspace",
            description=(
                "Clone a repo under the task run root and create a task branch. "
                "Inputs: repo_url, task_id, branch_name, base_branch, run_id."
            ),
        ),
        StructuredTool.from_function(
            func=git_status,
            name="git_status",
            description="Return git status for a guarded task workspace.",
        ),
        StructuredTool.from_function(
            func=git_diff_summary,
            name="git_diff_summary",
            description="Return git diff --stat for a guarded task workspace.",
        ),
        StructuredTool.from_function(
            func=git_commit,
            name="git_commit",
            description="Git add and commit all changes in a task workspace.",
        ),
        StructuredTool.from_function(
            func=git_push_branch,
            name="git_push_branch",
            description="Push a non-protected task branch to origin.",
        ),
        StructuredTool.from_function(
            func=forgejo_create_pr,
            name="forgejo_create_pr",
            description="Create a Forgejo pull request.",
        ),
        StructuredTool.from_function(
            func=forgejo_find_pr_for_branch,
            name="forgejo_find_pr_for_branch",
            description="Find open Forgejo pull requests by branch.",
        ),
        StructuredTool.from_function(
            func=forgejo_submit_review,
            name="forgejo_submit_review",
            description="Submit a formal Forgejo pull-request review.",
        ),
    ]

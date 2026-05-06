"""Render deterministic auto-fire prompts for KAI agent sessions."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "prompts"
TASKBOARD_PROMPT_ROOT = PROMPTS_ROOT / "taskboard-fire"
FORGEJO_PR_PROMPT_ROOT = PROMPTS_ROOT / "forgejo-pr-fire"
PROMPT_ROOT = TASKBOARD_PROMPT_ROOT
DEFAULT_TEMPLATE = "default.md.tmpl"
MAX_RENDERED_PROMPT_CHARS = 60_000

_ROLE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SECRET_KEY_PARTS = (
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
)


class PromptRenderError(RuntimeError):
    """Raised when an auto-fire prompt cannot be rendered.

    Args:
        *args: Error details forwarded to ``RuntimeError``.
    """


class _SafeFormatDict(dict[str, str]):
    """Format mapping that renders unknown placeholders as empty strings."""

    def __missing__(self, key: str) -> str:
        return ""


def render_taskboard_fire_prompt(
    role: str,
    task: dict[str, Any],
    *,
    session_token: str = "",
    session_generation: int | None = None,
    worktree_path: str = "",
    primary_repo_path: str = "",
    workspace_manifest_path: str = "",
    repo_routing_mode: str = "",
) -> str:
    """Render a role-specific taskboard auto-fire prompt.

    Args:
        role: Taskboard agent role name, such as ``developer`` or
            ``code-reviewer``. Unknown roles fall back to the default template.
        task: Task payload from the taskboard API or dispatcher. Nested
            ``project`` and ``epic`` dictionaries are flattened into template
            substitutions.
        session_token: Phase 0 follow-up (#10247): bound agent_session token
            the spawned agent must use for taskboard writes (start-work,
            comment, move-status, stop-work). Empty string when none was
            minted (degraded mode — agent will 409 on writes).
        session_generation: Phase 0 follow-up (#10247): session generation
            paired with ``session_token``. Required by the taskboard's
            session-token validator alongside the token.

    Returns:
        Rendered prompt string ready for the KAI session spawn surface.

    Raises:
        PromptRenderError: If the selected template cannot be read, contains
            malformed format syntax, or the rendered prompt cannot be capped.

    Example:
        >>> task = {"id": 123, "title": "Fix login bug", "description": "..."}
        >>> prompt = render_taskboard_fire_prompt("developer", task)
        >>> "Fix login bug" in prompt
        True
    """

    substitutions = _SafeFormatDict(_extract_taskboard_substitutions(task, role=role))
    substitutions["role"] = _normalize_role(role) or "default"
    if session_token:
        substitutions["session_token"] = session_token
    if session_generation is not None:
        substitutions["session_generation"] = str(session_generation)
    if worktree_path:
        substitutions["worktree_path"] = worktree_path
    if primary_repo_path:
        substitutions["primary_repo_path"] = primary_repo_path
    if workspace_manifest_path:
        substitutions["workspace_manifest_path"] = workspace_manifest_path
    if repo_routing_mode:
        substitutions["repo_routing_mode"] = repo_routing_mode
    return _render_prompt(
        TASKBOARD_PROMPT_ROOT,
        role,
        substitutions,
        prompt_kind="taskboard fire",
        stop_marker="TASKBOARD_FIRE_PROMPT_END",
    )


def render_forgejo_pr_fire_prompt(role: str, pr: dict[str, Any]) -> str:
    """Render a role-specific Forgejo pull-request auto-fire prompt.

    Args:
        role: Reviewer role name, such as ``code-reviewer``,
            ``security-auditor``, or ``qa-agent``. Unknown roles fall back to
            the default Forgejo PR template.
        pr: Pull-request context from the Forgejo dispatcher. Flat dispatcher
            fields and common Forgejo webhook/API nesting are accepted.

    Returns:
        Rendered prompt string ready for the KAI session spawn surface.

    Raises:
        PromptRenderError: If the selected template cannot be read, contains
            malformed format syntax, or the rendered prompt cannot be capped.

    Example:
        >>> pr = {"repo": "owner/repo", "pr_number": 7, "title": "Fix bug"}
        >>> prompt = render_forgejo_pr_fire_prompt("code-reviewer", pr)
        >>> "owner/repo" in prompt
        True
    """

    normalized_role = _normalize_role(role)
    substitutions = _SafeFormatDict(_extract_pr_substitutions(pr))
    substitutions["role"] = normalized_role or "default"
    if not substitutions.get("output_target"):
        substitutions["output_target"] = _forgejo_output_target(
            normalized_role,
            substitutions,
        )
    return _render_prompt(
        FORGEJO_PR_PROMPT_ROOT,
        role,
        substitutions,
        prompt_kind="Forgejo PR fire",
        stop_marker="FORGEJO_PR_FIRE_PROMPT_END",
    )


def _extract_substitutions(
    task: Mapping[str, Any],
    *,
    role: str = "",
) -> dict[str, str]:
    """Compatibility wrapper for taskboard template substitutions.

    Args:
        task: Taskboard task payload. Common snake_case and camelCase field
            names are accepted.
        role: Role used to derive fallback agent and output target values.

    Returns:
        Mapping of taskboard template variable names to string values.
    """

    return _extract_taskboard_substitutions(task, role=role)


def _extract_taskboard_substitutions(
    task: Mapping[str, Any],
    *,
    role: str = "",
) -> dict[str, str]:
    """Flatten taskboard task fields into template substitutions.

    Args:
        task: Taskboard task payload. Common snake_case and camelCase field
            names are accepted.
        role: Role used to derive fallback agent and output target values.

    Returns:
        Mapping of prompt template variable names to string values. Missing
        optional fields are rendered as empty strings, and any non-empty task
        receives a stable ``task_id`` value.
    """

    task_mapping: Mapping[str, Any] = task if isinstance(task, Mapping) else {}
    project = _mapping_value(task_mapping, "project")
    epic = _mapping_value(task_mapping, "epic")
    role_name = _normalize_role(role)
    agent = _field(task_mapping, "agent", "agent_name", "agentName") or role_name

    task_id = _stringify(
        _field(task_mapping, "task_id", "taskId", "id", "number")
    ).strip()
    if not task_id and task_mapping:
        task_id = "unknown"

    title = _stringify(_field(task_mapping, "title", "name", "summary")).strip()
    task_type = _stringify(
        _field(task_mapping, "task_type", "taskType", "type", "kind")
    ).strip()
    agent_id = _stringify(
        _field(task_mapping, "agent_id", "agentId", "agent_slug", "agentSlug")
    ).strip() or role_name

    branch_name = _stringify(
        _field(
            task_mapping,
            "branch_name",
            "branchName",
            "branch",
            "branch_name_suggestion",
            "branchNameSuggestion",
        )
    ).strip()
    branch_name_suggestion = branch_name or _branch_name_suggestion(task_id, title)

    output_target = _stringify(
        _field(task_mapping, "output_target", "outputTarget")
    ).strip()
    if not output_target:
        role_slug = _slugify(agent_id or _stringify(agent), fallback="agent")
        output_task_id = task_id or "task"
        output_target = f"{role_slug}/claude/artifacts/{output_task_id}-final.txt"

    return {
        "task_id": task_id,
        "title": title,
        "description": _stringify(
            _field(task_mapping, "description", "body", "content")
        ),
        "agent": _stringify(agent),
        "agent_id": agent_id,
        "task_type": task_type,
        "priority": _stringify(_field(task_mapping, "priority", "severity")),
        "project_id": _stringify(_field(task_mapping, "project_id", "projectId")),
        "project_name": _stringify(
            _field(task_mapping, "project_name", "projectName")
            or _field(project, "name", "title")
        ),
        "project_slug": _stringify(
            _field(task_mapping, "project_slug", "projectSlug")
            or _field(project, "slug")
        ),
        "repo_url": _stringify(
            _field(
                task_mapping,
                "repo_url",
                "repoUrl",
                "repository_url",
                "repositoryUrl",
            )
            or _field(
                project,
                "repo_url",
                "repoUrl",
                "repository_url",
                "repositoryUrl",
            )
        ),
        "default_branch": _stringify(
            _field(task_mapping, "default_branch", "defaultBranch")
            or _field(project, "default_branch", "defaultBranch")
        ),
        "epic_id": _stringify(
            _field(task_mapping, "epic_id", "epicId") or _field(epic, "id")
        ),
        "epic_title": _stringify(
            _field(task_mapping, "epic_title", "epicTitle")
            or _field(epic, "title", "name")
        ),
        "source_ref": _stringify(
            _field(task_mapping, "source_ref", "sourceRef", "source", "reference")
        ),
        "branch_name": branch_name_suggestion,
        "branch_name_suggestion": branch_name_suggestion,
        "task_url": _stringify(_field(task_mapping, "task_url", "taskUrl", "url")),
        "comments_url": _stringify(
            _field(task_mapping, "comments_url", "commentsUrl")
        ),
        "fire_generation": _stringify(
            _field(task_mapping, "fire_generation", "fireGeneration")
        ),
        "session_token": _stringify(
            _field(task_mapping, "session_token", "sessionToken")
        ),
        "session_generation": _stringify(
            _field(task_mapping, "session_generation", "sessionGeneration")
        ),
        "worktree_path": _stringify(
            _field(task_mapping, "worktree_path", "worktreePath")
        ),
        "primary_repo_path": _stringify(
            _field(task_mapping, "primary_repo_path", "primaryRepoPath")
        ),
        "workspace_manifest_path": _stringify(
            _field(task_mapping, "workspace_manifest_path", "workspaceManifestPath")
        ),
        "repo_routing_mode": _stringify(
            _field(task_mapping, "repo_routing_mode", "repoRoutingMode")
        ),
        "output_target": output_target,
        "comments_context": _stringify(
            _field(task_mapping, "comments_context", "commentsContext", "comments")
        ),
        "project_context": _stringify(
            _field(task_mapping, "project_context", "projectContext")
            or _redacted_json(project)
        ),
        "epic_context": _stringify(
            _field(task_mapping, "epic_context", "epicContext") or _redacted_json(epic)
        ),
    }


def _extract_pr_substitutions(pr: Mapping[str, Any]) -> dict[str, str]:
    """Flatten Forgejo pull-request fields into template substitutions.

    Args:
        pr: Pull-request payload from the dispatcher or Forgejo webhook/API.

    Returns:
        Mapping of Forgejo PR template variable names to string values. Missing
        optional fields are returned as empty strings, and ``diff_summary`` is
        derived from ``files_changed`` when changed-file metadata is present.
    """

    pr_mapping: Mapping[str, Any] = pr if isinstance(pr, Mapping) else {}
    pull_request = _mapping_value(pr_mapping, "pull_request") or _mapping_value(
        pr_mapping,
        "pullRequest",
    )
    repository = _mapping_value(pr_mapping, "repository") or _mapping_value(
        pull_request,
        "repository",
    )
    head = (
        _mapping_value(pr_mapping, "head")
        or _mapping_value(pull_request, "head")
        or _mapping_value(pull_request, "head_branch")
    )

    repo = (
        _field(
            pr_mapping,
            "repo",
            "repository_full_name",
            "repositoryFullName",
            "full_name",
            "fullName",
        )
        or _field(repository, "full_name", "fullName")
        or _repository_name(repository)
    )
    files_value = _field(
        pr_mapping,
        "files_changed",
        "filesChanged",
        "changed_files",
        "changedFiles",
        "files",
    ) or _field(
        pull_request,
        "files_changed",
        "filesChanged",
        "changed_files",
        "changedFiles",
        "files",
    )
    diff_summary = _build_diff_summary(
        files_value,
        explicit=_stringify(_field(pr_mapping, "diff_summary", "diffSummary")),
    )

    return {
        "repo": _stringify(repo),
        "pr_number": _stringify(
            _field(pr_mapping, "pr_number", "prNumber", "number", "id")
            or _field(pull_request, "number", "id")
        ),
        "branch": _stringify(
            _field(pr_mapping, "branch", "head_ref", "headRef")
            or _field(head, "ref", "label", "name")
        ),
        "title": _stringify(
            _field(pr_mapping, "title") or _field(pull_request, "title")
        ),
        "body": _stringify(
            _field(pr_mapping, "body", "description")
            or _field(pull_request, "body", "description")
        ),
        "files_changed": _format_files_changed(files_value),
        "diff_summary": diff_summary,
        "head_sha": _stringify(
            _field(pr_mapping, "head_sha", "headSha", "sha")
            or _field(head, "sha")
        ),
        "pr_url": _stringify(
            _field(pr_mapping, "pr_url", "prUrl", "html_url", "htmlUrl", "url")
            or _field(pull_request, "html_url", "htmlUrl", "url")
        ),
        "taskboard_task_id": _stringify(
            _field(
                pr_mapping,
                "taskboard_task_id",
                "taskboardTaskId",
                "linked_taskboard_task_id",
                "linkedTaskboardTaskId",
            )
        ),
        "output_target": _stringify(
            _field(pr_mapping, "output_target", "outputTarget")
        ),
    }


def _render_prompt(
    prompt_root: Path,
    role: str,
    substitutions: _SafeFormatDict,
    *,
    prompt_kind: str,
    stop_marker: str,
) -> str:
    """Render a prompt template under a constrained root directory."""

    template_path = _resolve_template_path(prompt_root, role)
    try:
        template = template_path.read_text(encoding="utf-8")
        rendered = template.format_map(substitutions)
    except (OSError, ValueError) as exc:
        raise PromptRenderError(
            f"failed to render {prompt_kind} prompt from {template_path.name}: {exc}"
        ) from exc

    return _cap_prompt(rendered, stop_marker=stop_marker)


def _resolve_template_path(prompt_root: Path | str, role: str | None = None) -> Path:
    """Resolve a role template path while keeping reads under prompt_root.

    Args:
        prompt_root: Template directory, or a role name for compatibility
            with the original taskboard-only helper.
        role: Role name. When omitted, ``prompt_root`` is treated as a
            taskboard role and :data:`TASKBOARD_PROMPT_ROOT` is used.

    Returns:
        Resolved template path under the selected prompt root.
    """

    if role is None:
        root = TASKBOARD_PROMPT_ROOT.resolve()
        normalized_role = _normalize_role(str(prompt_root))
    else:
        root = Path(prompt_root).resolve()
        normalized_role = _normalize_role(role)

    default_path = (root / DEFAULT_TEMPLATE).resolve()
    candidate = default_path
    if normalized_role and _ROLE_RE.fullmatch(normalized_role):
        role_candidate = (root / f"{normalized_role}.md.tmpl").resolve()
        if root in role_candidate.parents and role_candidate.is_file():
            candidate = role_candidate

    resolved = candidate.resolve()
    if root not in resolved.parents:
        return default_path
    if resolved.is_file():
        return resolved
    return default_path


def _normalize_role(role: str) -> str:
    """Normalize a role to a template filename stem."""

    return str(role or "").strip().lower()


def _mapping_value(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return a nested dictionary field as a mapping."""

    value = mapping.get(key) if isinstance(mapping, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _field(mapping: Mapping[str, Any], *names: str) -> Any:
    """Return the first non-empty value matching one of the supplied names."""

    if not isinstance(mapping, Mapping):
        return ""
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return ""


def _repository_name(repository: Mapping[str, Any]) -> str:
    """Build a repository full name from nested Forgejo repository fields."""

    name = _field(repository, "name")
    owner = _mapping_value(repository, "owner")
    owner_name = _field(owner, "login", "username", "name")
    if owner_name and name:
        return f"{owner_name}/{name}"
    return _stringify(name)


def _format_files_changed(files_changed: Any) -> str:
    """Format changed-file metadata as deterministic prompt text."""

    files = _files_sequence(files_changed)
    if files is None:
        return _stringify(files_changed)
    if not files:
        return ""

    lines: list[str] = []
    for file_change in files:
        if isinstance(file_change, Mapping):
            status = _stringify(
                _field(file_change, "status", "change_type", "changeType", "type")
                or "changed"
            )
            filename = _stringify(
                _field(file_change, "filename", "path", "new_path", "newPath", "name")
            )
            previous = _stringify(
                _field(file_change, "previous_filename", "old_path", "oldPath")
            )
            if previous and filename and previous != filename:
                file_label = f"{previous} -> {filename}"
            else:
                file_label = filename or _stringify(file_change)
            lines.append(
                f"- {status} {file_label}{_format_file_churn(file_change)}".rstrip()
            )
        else:
            lines.append(f"- {_stringify(file_change)}")
    return "\n".join(lines)


def _build_diff_summary(files_changed: Any, *, explicit: str = "") -> str:
    """Build a compact diff summary from changed-file metadata."""

    files = _files_sequence(files_changed)
    if files is None:
        if explicit:
            return explicit
        if isinstance(files_changed, int):
            return f"{files_changed} files changed"
        return _stringify(files_changed)
    if not files:
        return explicit

    additions = 0
    deletions = 0
    has_churn = False
    for file_change in files:
        if not isinstance(file_change, Mapping):
            continue
        file_additions = _int_or_none(_field(file_change, "additions", "added"))
        file_deletions = _int_or_none(_field(file_change, "deletions", "deleted"))
        if file_additions is not None:
            additions += file_additions
            has_churn = True
        if file_deletions is not None:
            deletions += file_deletions
            has_churn = True

    file_word = "file" if len(files) == 1 else "files"
    header = f"{len(files)} {file_word} changed"
    if has_churn:
        header = f"{header}: {additions} additions, {deletions} deletions"
    formatted_files = _format_files_changed(files)
    if not formatted_files:
        return header
    return f"{header}\n{formatted_files}"


def _files_sequence(files_changed: Any) -> list[Any] | None:
    """Return changed-file metadata as a list when it is sequence-like."""

    if isinstance(files_changed, str) or isinstance(files_changed, bytes):
        return None
    if isinstance(files_changed, Sequence):
        return list(files_changed)
    return None


def _format_file_churn(file_change: Mapping[str, Any]) -> str:
    """Format additions, deletions, or total changes for one file."""

    additions = _int_or_none(_field(file_change, "additions", "added"))
    deletions = _int_or_none(_field(file_change, "deletions", "deleted"))
    changes = _int_or_none(_field(file_change, "changes", "total_changes"))
    if additions is not None or deletions is not None:
        return f" (+{additions or 0}/-{deletions or 0})"
    if changes is not None:
        return f" ({changes} changes)"
    return ""


def _int_or_none(value: Any) -> int | None:
    """Convert an integer-like value to int when possible."""

    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stringify(value: Any) -> str:
    """Convert values into prompt-safe text with secret-like fields redacted."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(_redact_value(value), ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _redacted_json(value: Mapping[str, Any]) -> str:
    """Serialize nested context while redacting secret-like fields."""

    if not value:
        return ""
    return json.dumps(_redact_value(value), ensure_ascii=False, sort_keys=True)


def _redact_value(value: Any) -> Any:
    """Recursively redact values for keys that appear to contain secrets."""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_secret_key(str(key)) else _redact_value(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _is_secret_key(key: str) -> bool:
    """Return whether a field name appears to contain secret material."""

    lowered = key.lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def _forgejo_output_target(role: str, substitutions: Mapping[str, str]) -> str:
    """Build a deterministic output path for a Forgejo PR review prompt."""

    role_slug = _slugify(role, fallback="reviewer")
    repo_slug = _slugify(substitutions.get("repo", ""), fallback="repo")
    pr_number = _slugify(substitutions.get("pr_number", ""), fallback="pr")
    head_sha = _slugify(substitutions.get("head_sha", "")[:12], fallback="head")
    return (
        f"{role_slug}/claude/artifacts/"
        f"forgejo-pr-{repo_slug}-{pr_number}-{head_sha}-final.txt"
    )


def _branch_name_suggestion(task_id: str, title: str) -> str:
    """Build a deterministic branch suggestion from task id and title."""

    prefix = f"task-{task_id or 'unknown'}"
    slug = _slugify(title, fallback="")
    if not slug:
        return prefix
    branch = f"{prefix}-{slug}"
    return branch[:96].rstrip("-")


def _slugify(value: str, *, fallback: str) -> str:
    """Convert display text into a lowercase filename-safe slug."""

    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or fallback


def _cap_prompt(
    rendered: str,
    *,
    stop_marker: str = "TASKBOARD_FIRE_PROMPT_END",
) -> str:
    """Cap rendered prompt size without cutting the STOP marker first."""

    if len(rendered) <= MAX_RENDERED_PROMPT_CHARS:
        return rendered
    marker = f"\nSTOP: {stop_marker}"
    budget = MAX_RENDERED_PROMPT_CHARS - len(marker) - 64
    if budget <= 0:
        raise PromptRenderError("rendered prompt cap is too small")
    return rendered[:budget].rstrip() + "\n\n[Prompt truncated by renderer]\n" + marker

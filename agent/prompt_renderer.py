"""Render deterministic taskboard auto-fire prompts."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any


PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompts" / "taskboard-fire"
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
    """Raised when a taskboard auto-fire prompt cannot be rendered.

    Args:
        *args: Error details forwarded to ``RuntimeError``.
    """


class _SafeFormatDict(dict[str, str]):
    """Format mapping that renders unknown placeholders as empty strings."""

    def __missing__(self, key: str) -> str:
        return ""


def render_taskboard_fire_prompt(role: str, task: dict[str, Any]) -> str:
    """Render a role-specific taskboard auto-fire prompt.

    Args:
        role: Taskboard agent role name, such as ``developer`` or
            ``code-reviewer``. Unknown roles fall back to the default template.
        task: Task payload from the taskboard API or dispatcher. Nested
            ``project`` and ``epic`` dictionaries are flattened into template
            substitutions.

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

    template_path = _resolve_template_path(role)
    substitutions = _SafeFormatDict(_extract_substitutions(task, role=role))
    substitutions["role"] = _normalize_role(role) or "default"

    try:
        template = template_path.read_text(encoding="utf-8")
        rendered = template.format_map(substitutions)
    except (OSError, ValueError) as exc:
        raise PromptRenderError(
            f"failed to render taskboard fire prompt from {template_path.name}: {exc}"
        ) from exc

    return _cap_prompt(rendered)


def _extract_substitutions(
    task: Mapping[str, Any],
    *,
    role: str = "",
) -> dict[str, str]:
    """Flatten taskboard task fields into template substitutions.

    Args:
        task: Taskboard task payload. Common snake_case and camelCase field
            names are accepted.

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

    substitutions = {
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
    return substitutions


def _resolve_template_path(role: str) -> Path:
    """Resolve a template path while keeping reads under the prompt root."""

    root = PROMPT_ROOT.resolve()
    normalized_role = _normalize_role(role)
    candidate = root / DEFAULT_TEMPLATE
    if normalized_role and _ROLE_RE.fullmatch(normalized_role):
        role_candidate = (root / f"{normalized_role}.md.tmpl").resolve()
        if root in role_candidate.parents and role_candidate.is_file():
            candidate = role_candidate

    default_path = (root / DEFAULT_TEMPLATE).resolve()
    resolved = candidate.resolve()
    if root not in resolved.parents:
        return default_path
    if resolved.is_file():
        return resolved
    return default_path


def _normalize_role(role: str) -> str:
    """Normalize a taskboard role to a template filename stem."""

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


def _stringify(value: Any) -> str:
    """Convert taskboard values into prompt-safe text."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _redacted_json(value: Mapping[str, Any]) -> str:
    """Serialize nested context while redacting secret-like fields."""

    if not value:
        return ""
    redacted = {
        str(key): "[REDACTED]" if _is_secret_key(str(key)) else val
        for key, val in value.items()
    }
    return json.dumps(redacted, ensure_ascii=False, sort_keys=True)


def _is_secret_key(key: str) -> bool:
    """Return whether a field name appears to contain secret material."""

    lowered = key.lower()
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def _branch_name_suggestion(task_id: str, title: str) -> str:
    """Build a deterministic branch suggestion from task id and title."""

    prefix = f"task-{task_id or 'unknown'}"
    slug = _slugify(title, fallback="")
    if not slug:
        return prefix
    branch = f"{prefix}-{slug}"
    return branch[:96].rstrip("-")


def _slugify(value: str, *, fallback: str) -> str:
    """Convert display text into a lowercase branch-safe slug."""

    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug or fallback


def _cap_prompt(rendered: str) -> str:
    """Cap rendered prompt size without cutting the STOP marker first."""

    if len(rendered) <= MAX_RENDERED_PROMPT_CHARS:
        return rendered
    marker = "\nSTOP: TASKBOARD_FIRE_PROMPT_END"
    budget = MAX_RENDERED_PROMPT_CHARS - len(marker) - 64
    if budget <= 0:
        raise PromptRenderError("rendered prompt cap is too small")
    return rendered[:budget].rstrip() + "\n\n[Prompt truncated by renderer]\n" + marker

"""Procedural memory — agent-authored skill files.

Phase 2 of the self-improving-agent epic. Where ``MemoryStore`` holds
flat facts that get injected into every system prompt, ``SkillStore``
holds full recipes the agent has earned through trial and error:
"when you see X, do these exact steps, here are the pitfalls". Skills
are **loaded on demand** rather than always-present so they don't eat
context budget.

Adapted from the Hermes skills system (``tools/skill_manager_tool.py``
and ``tools/skills_tool.py``) with the following simplifications for
our smaller agent set:

- **Flat layout.** Hermes uses ``~/.hermes/skills/<category>/<name>/SKILL.md``
  with optional reference files and scripts alongside. We use a single
  file per skill: ``workspaces/<role>/skills/<name>.md`` containing the
  YAML frontmatter + the body. Adds simplicity at the cost of
  multi-file skills — if we ever need scripts or reference templates
  we can add a subdir convention later without breaking anything.
- **Per-agent skills.** Each sub-agent's ``skills/`` directory is
  isolated from the others. The trader's playbooks don't pollute the
  analyst's skill index.
- **Same security gauntlet as memory.** Every write goes through
  ``_scan_memory_content`` so an agent can't talk itself into
  persisting a prompt-injection payload.

Progressive disclosure (matches Hermes):

  list() → [{name, description, category}, ...]   minimal catalog
  view(name) → full file content                  only when needed

The LLM should use ``list`` to know what's available, ``view`` only
when a named skill looks relevant to the current task, and
``manage(create/patch/edit/delete)`` after a successful trial-and-error
run that's worth capturing.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_store import _scan_memory_content

logger = logging.getLogger(__name__)


# ── Size caps ────────────────────────────────────────────────
#
# A skill is free-form markdown, but we don't want an agent to
# persist a 200 KB wall of text. Typical skills are 500-5000 chars
# (Hermes's own bundled skills average ~3 KB), so 20 KB is generous
# headroom. Larger material belongs in workspace files, not a skill.

MAX_SKILL_CHARS = 20_000

# Validate the slug used as the filename. Matches Hermes: lowercase
# letters, digits, hyphens, underscores, max 64 chars. No slashes,
# dots, or whitespace.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


# ── Frontmatter parsing ──────────────────────────────────────
#
# SKILL.md files have a YAML-ish frontmatter block between ``---``
# fences at the very top, followed by a markdown body. We parse a
# tiny subset of YAML by hand (key: value, indented lists, nested
# dict) so we don't pick up a PyYAML dependency that the rest of
# the agent module doesn't need. Only the top-level ``name`` and
# ``description`` keys are required; everything else is opaque
# metadata we preserve through round-trips without interpreting.


@dataclass
class SkillMeta:
    """Structured view of a skill's frontmatter, for listings."""

    name: str
    description: str
    category: str = ""
    tags: List[str] | None = None


def _parse_frontmatter(content: str) -> tuple[Dict[str, Any], str]:
    """Split SKILL.md content into (frontmatter dict, body).

    Very lenient: if the file doesn't start with ``---`` we treat the
    whole thing as body with an empty frontmatter dict. If parsing
    the YAML-ish block fails we keep what we extracted and log the
    error — skills should never crash the whole listing.
    """
    if not content.startswith("---"):
        return {}, content

    lines = content.split("\n")
    if len(lines) < 3:
        return {}, content

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return {}, content

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")

    meta: Dict[str, Any] = {}
    current_list_key: Optional[str] = None
    for raw in fm_lines:
        if not raw.strip():
            current_list_key = None
            continue
        # List continuation: "  - item"
        stripped = raw.lstrip()
        if stripped.startswith("- ") and current_list_key:
            meta.setdefault(current_list_key, []).append(stripped[2:].strip())
            continue
        if ":" not in raw:
            current_list_key = None
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            # Expect a list on following lines
            current_list_key = key
            meta[key] = []
            continue
        current_list_key = None
        # Strip optional quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        meta[key] = value

    return meta, body


def _render_frontmatter(meta: Dict[str, Any]) -> str:
    """Render a simple YAML-ish frontmatter block from a dict.

    Only used when the caller modifies a skill's metadata through
    structured APIs. For create/edit/patch we don't rewrite
    frontmatter — we just preserve whatever the agent wrote as-is.
    Included for completeness in case a future tool wants to tweak
    metadata without the agent authoring the full block.
    """
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for k, v in value.items():
                lines.append(f"  {k}: {v}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


# ── Validation ───────────────────────────────────────────────


def _validate_name(name: str) -> Optional[str]:
    """Return an error string if the skill name is invalid."""
    if not name or not isinstance(name, str):
        return "Skill name is required and must be a string."
    if not _SKILL_NAME_RE.fullmatch(name):
        return (
            "Skill name must be lowercase letters/digits/hyphens/underscores, "
            "start with a letter or digit, and be 1-64 chars long."
        )
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    """Return an error string if frontmatter is missing required keys."""
    meta, _ = _parse_frontmatter(content)
    if not meta:
        return (
            "Skill content must begin with a '---' YAML frontmatter block "
            "containing at least 'name:' and 'description:' keys."
        )
    if not meta.get("name"):
        return "Skill frontmatter is missing the required 'name' key."
    if not meta.get("description"):
        return "Skill frontmatter is missing the required 'description' key."
    return None


def _validate_size(content: str) -> Optional[str]:
    if len(content) > MAX_SKILL_CHARS:
        return (
            f"Skill content is {len(content):,} chars, exceeds the "
            f"{MAX_SKILL_CHARS:,} char cap. Trim references or move "
            "large material into workspace files."
        )
    return None


# ── Atomic write + file helpers (same pattern as memory_store) ─


def _atomic_write(path: Path, content: str) -> None:
    """Atomic write via tempfile.mkstemp + os.replace in the same dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".skill_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── SkillStore ───────────────────────────────────────────────


class SkillStore:
    """Per-agent skill library with create/list/view/patch/edit/delete.

    One instance per agent, constructed with the agent's skills dir.
    Operations scan the directory on every call rather than caching —
    the skill count is small enough that the read cost is negligible
    and it avoids cache-invalidation complexity when two processes
    share the same agent workspace (cron runs + interactive runs).
    """

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)

    # ── Filesystem layout ───────────────────────────────────

    def _path_for(self, name: str) -> Path:
        return self.skills_dir / f"{name}.md"

    def _ensure_dir(self) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    # ── List ────────────────────────────────────────────────

    def list_skills(self) -> List[Dict[str, str]]:
        """Return a lightweight catalog of available skills.

        Each entry is ``{name, description, category}``. The body of
        each skill is NOT loaded — that's what ``view`` is for. This
        keeps the ``skills_list`` tool response small so the LLM can
        afford to call it at the start of any session without eating
        context budget.
        """
        if not self.skills_dir.is_dir():
            return []
        out: List[Dict[str, str]] = []
        for path in sorted(self.skills_dir.glob("*.md")):
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("skill read failed %s: %s", path, exc)
                continue
            meta, _ = _parse_frontmatter(raw)
            name = meta.get("name") or path.stem
            out.append(
                {
                    "name": name,
                    "description": meta.get("description", "(no description)"),
                    "category": meta.get("category", ""),
                }
            )
        return out

    # ── View ────────────────────────────────────────────────

    def view(self, name: str) -> Dict[str, Any]:
        """Return the full content of a named skill."""
        err = _validate_name(name)
        if err:
            return {"success": False, "error": err}
        path = self._path_for(name)
        if not path.is_file():
            return {"success": False, "error": f"Skill '{name}' not found."}
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"success": False, "error": f"Could not read skill '{name}': {exc}"}
        meta, _ = _parse_frontmatter(content)
        return {
            "success": True,
            "name": meta.get("name") or name,
            "description": meta.get("description", ""),
            "category": meta.get("category", ""),
            "content": content,
            "size": len(content),
        }

    # ── Create / edit / patch / delete ─────────────────────

    def create(self, name: str, content: str) -> Dict[str, Any]:
        """Author a new skill. Rejects overwrites — use edit/patch instead."""
        err = _validate_name(name)
        if err:
            return {"success": False, "error": err}
        err = _validate_frontmatter(content)
        if err:
            return {"success": False, "error": err}
        err = _validate_size(content)
        if err:
            return {"success": False, "error": err}
        scan = _scan_memory_content(content)
        if scan:
            return {"success": False, "error": scan}

        path = self._path_for(name)
        if path.exists():
            return {
                "success": False,
                "error": (
                    f"Skill '{name}' already exists. Use edit to replace the "
                    "whole file, or patch to change specific sections."
                ),
            }
        self._ensure_dir()
        _atomic_write(path, content)
        return {
            "success": True,
            "name": name,
            "message": f"Skill '{name}' created.",
            "size": len(content),
        }

    def edit(self, name: str, content: str) -> Dict[str, Any]:
        """Replace the entire SKILL.md content."""
        err = _validate_name(name)
        if err:
            return {"success": False, "error": err}
        err = _validate_frontmatter(content)
        if err:
            return {"success": False, "error": err}
        err = _validate_size(content)
        if err:
            return {"success": False, "error": err}
        scan = _scan_memory_content(content)
        if scan:
            return {"success": False, "error": scan}

        path = self._path_for(name)
        if not path.is_file():
            return {"success": False, "error": f"Skill '{name}' not found — use create."}
        _atomic_write(path, content)
        return {
            "success": True,
            "name": name,
            "message": f"Skill '{name}' fully replaced.",
            "size": len(content),
        }

    def patch(
        self,
        name: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Dict[str, Any]:
        """Surgical substring replace inside an existing skill.

        Same shape as our ``file_edit`` tool. The substring must be
        unique unless ``replace_all=True``. This is the preferred way
        for an agent to fix a specific step in a skill without
        rewriting the whole thing.
        """
        err = _validate_name(name)
        if err:
            return {"success": False, "error": err}
        if not old_string:
            return {"success": False, "error": "old_string cannot be empty."}

        path = self._path_for(name)
        if not path.is_file():
            return {"success": False, "error": f"Skill '{name}' not found."}

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"success": False, "error": f"Could not read skill '{name}': {exc}"}

        occurrences = content.count(old_string)
        if occurrences == 0:
            return {
                "success": False,
                "error": f"old_string not found in skill '{name}'.",
            }
        if occurrences > 1 and not replace_all:
            return {
                "success": False,
                "error": (
                    f"old_string matches {occurrences} times in skill '{name}'. "
                    "Include more surrounding context, or pass replace_all=true."
                ),
            }

        new_content = (
            content.replace(old_string, new_string)
            if replace_all
            else content.replace(old_string, new_string, 1)
        )

        err = _validate_frontmatter(new_content)
        if err:
            return {"success": False, "error": f"Patched content invalid: {err}"}
        err = _validate_size(new_content)
        if err:
            return {"success": False, "error": err}
        scan = _scan_memory_content(new_content)
        if scan:
            return {"success": False, "error": scan}

        _atomic_write(path, new_content)
        return {
            "success": True,
            "name": name,
            "message": (
                f"Patched skill '{name}' — "
                f"replaced {occurrences if replace_all else 1} occurrence"
                f"{'s' if (replace_all and occurrences > 1) else ''}."
            ),
            "size": len(new_content),
        }

    def delete(self, name: str) -> Dict[str, Any]:
        err = _validate_name(name)
        if err:
            return {"success": False, "error": err}
        path = self._path_for(name)
        if not path.is_file():
            return {"success": False, "error": f"Skill '{name}' not found."}
        try:
            path.unlink()
        except OSError as exc:
            return {"success": False, "error": f"Could not delete skill '{name}': {exc}"}
        return {"success": True, "name": name, "message": f"Skill '{name}' deleted."}

"""LangChain tool wrappers for the SkillStore.

Three tools following Hermes's progressive-disclosure pattern:

- ``skills_list``: lightweight catalog of available skills (name,
  description, category). The LLM calls this to find out what's on
  the shelf without pulling any full content.
- ``skill_view``: load the full SKILL.md content for a named skill.
  Called when the LLM decides a listed skill is actually relevant to
  the current task.
- ``skill_manage``: CRUD for agent-authored skills with actions
  create / edit / patch / delete. This is the self-improvement hook
  — after a hard task is solved through trial and error, the agent
  captures the recipe here so the next session starts with the
  lesson learned.

Each tool is built per-agent via ``create_*_tool(store)`` so the
closure binds to the agent's own SkillStore instance. If ``store``
is ``None`` the tools still exist but return a disabled error,
keeping the tool schema stable across agents.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from langchain_core.tools import StructuredTool

from agent.skills_store import MAX_SKILL_CHARS, SkillStore

logger = logging.getLogger(__name__)


# ── Descriptions (what the LLM sees) ─────────────────────────


_SKILLS_LIST_DESCRIPTION = (
    "List every skill available to this agent as a minimal catalog: "
    "name, one-line description, and category. Full skill content is NOT "
    "returned — call skill_view(name) to load the body of a specific "
    "skill you want to read.\n\n"
    "Skills are your PROCEDURAL memory: reusable step-by-step recipes "
    "for recurring task types you've figured out before. Start most "
    "non-trivial tasks by calling skills_list to see if a relevant "
    "recipe already exists before deriving one from scratch."
)


_SKILL_VIEW_DESCRIPTION = (
    "Return the full contents of a named skill (YAML frontmatter + "
    "markdown body). Use after skills_list when you've identified a "
    "skill that looks relevant to the current task. Inputs: name "
    "(string, the skill's slug from skills_list)."
)


_SKILL_MANAGE_DESCRIPTION = (
    "Create, update, or delete skills. Skills are your procedural "
    "memory — reusable recipes for recurring task types. Saving a "
    "good skill is one of the highest-leverage things you can do; "
    "it turns a one-time trial-and-error session into knowledge the "
    "next session can reuse directly.\n\n"
    "WHEN TO CREATE:\n"
    "- A difficult task succeeded after 5+ tool calls / several "
    "errors overcome / user-corrected approach that finally worked\n"
    "- You discovered a non-trivial workflow (multi-step procedure, "
    "API quirks you worked around, a verification routine that "
    "catches a class of mistakes)\n"
    "- The user asks you to 'remember how to do this'\n"
    "- You noticed you're about to redo something you already did "
    "in a previous session\n\n"
    "WHEN TO PATCH:\n"
    "- You used a skill and hit a case it didn't cover — fix it "
    "immediately before moving on\n"
    "- A step's exact command changed / paths moved / a new pitfall "
    "was discovered\n"
    "- A skill says 'macOS only' but you just got it working on "
    "Linux too — widen the applicability\n\n"
    "WHEN TO EDIT (full rewrite) instead of patch:\n"
    "- The skill's overall approach changed, not just individual steps\n"
    "- Major reorganization of sections is needed\n\n"
    "DON'T CREATE FOR:\n"
    "- Simple one-off tasks (shell_exec is fine)\n"
    "- Facts (use the memory tool instead — memory is for WHAT, "
    "skills are for HOW)\n"
    "- Trading advice or market commentary (those go in notes, not "
    "reusable playbooks)\n\n"
    "SKILL FORMAT — every skill begins with a YAML frontmatter block:\n"
    "    ---\n"
    "    name: my-skill\n"
    "    description: One-line summary of what this skill does\n"
    "    category: trading | analysis | devops | ...\n"
    "    tags: [list, of, optional, tags]\n"
    "    ---\n"
    "    # Skill Title\n"
    "    \n"
    "    ## When to use\n"
    "    Trigger conditions.\n"
    "    \n"
    "    ## Steps\n"
    "    Numbered steps with exact commands / expected outputs.\n"
    "    \n"
    "    ## Pitfalls\n"
    "    Things that trip you up.\n"
    "    \n"
    "    ## Verification\n"
    "    How to confirm it worked.\n\n"
    "ACTIONS:\n"
    "- create: add a brand-new skill (fails if name already exists)\n"
    "- patch: surgical old_string → new_string replacement inside an "
    "existing skill. Preferred for bug fixes, updates, adding pitfalls.\n"
    "- edit: replace the entire SKILL.md body (for major rewrites only)\n"
    "- delete: remove a skill that's obsolete or wrong beyond repair\n\n"
    f"Per-skill content cap is {MAX_SKILL_CHARS:,} chars — anything "
    "larger belongs in a workspace file, not a skill."
)


# ── Tool factories ───────────────────────────────────────────


def _disabled_response() -> str:
    return json.dumps(
        {
            "success": False,
            "error": "Skills are not available for this agent (no skills store configured).",
        }
    )


def create_skills_list_tool(store: Optional[SkillStore]) -> StructuredTool:
    """Build the ``skills_list`` tool bound to an agent's SkillStore.

    The tool takes no arguments — ``list_skills`` is always a full
    dump of what's in the agent's skills dir. If we later want
    per-category or tag filtering we can add optional params.
    """

    def _list() -> str:
        if store is None:
            return _disabled_response()
        try:
            items = store.list_skills()
        except Exception as exc:  # noqa: BLE001
            logger.warning("skills_list failed: %s", exc)
            return json.dumps({"success": False, "error": f"list failed: {exc}"})
        return json.dumps(
            {
                "success": True,
                "count": len(items),
                "skills": items,
            }
        )

    return StructuredTool.from_function(
        func=_list,
        name="skills_list",
        description=_SKILLS_LIST_DESCRIPTION,
    )


def create_skill_view_tool(store: Optional[SkillStore]) -> StructuredTool:
    """Build the ``skill_view`` tool."""

    def _view(name: str) -> str:
        if store is None:
            return _disabled_response()
        if not name:
            return json.dumps({"success": False, "error": "name is required."})
        try:
            result = store.view(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill_view failed: %s", exc)
            return json.dumps({"success": False, "error": f"view failed: {exc}"})
        return json.dumps(result, ensure_ascii=False)

    return StructuredTool.from_function(
        func=_view,
        name="skill_view",
        description=_SKILL_VIEW_DESCRIPTION,
    )


def create_skill_manage_tool(store: Optional[SkillStore]) -> StructuredTool:
    """Build the ``skill_manage`` tool with create/patch/edit/delete actions."""

    def _manage(
        action: str,
        name: str,
        content: str = "",
        old_string: str = "",
        new_string: str = "",
        replace_all: bool = False,
    ) -> str:
        if store is None:
            return _disabled_response()

        if action == "create":
            if not content:
                return json.dumps(
                    {"success": False, "error": "content is required for 'create'."}
                )
            result = store.create(name, content)
        elif action == "edit":
            if not content:
                return json.dumps(
                    {"success": False, "error": "content is required for 'edit'."}
                )
            result = store.edit(name, content)
        elif action == "patch":
            if not old_string:
                return json.dumps(
                    {"success": False, "error": "old_string is required for 'patch'."}
                )
            result = store.patch(name, old_string, new_string, replace_all=bool(replace_all))
        elif action == "delete":
            result = store.delete(name)
        else:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Unknown action '{action}'. Use: create, edit, patch, delete."
                    ),
                }
            )

        return json.dumps(result, ensure_ascii=False)

    return StructuredTool.from_function(
        func=_manage,
        name="skill_manage",
        description=_SKILL_MANAGE_DESCRIPTION,
    )


def create_skills_tools(store: Optional[SkillStore]) -> list[StructuredTool]:
    """Convenience bundle: all three skills tools for a given store."""
    return [
        create_skills_list_tool(store),
        create_skill_view_tool(store),
        create_skill_manage_tool(store),
    ]

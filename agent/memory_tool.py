"""LangChain tool wrapper around MemoryStore.

Exposes one ``memory`` tool to the LLM with three actions (``add``,
``replace``, ``remove``). There's deliberately no ``read`` action —
memory content is injected into the system prompt at session start
instead, which means the LLM never has to ask "what's in memory?"
(it already sees it) and we save a round-trip.

The tool is constructed per-agent via ``create_memory_tool(store)`` so
each sub-agent gets its own store, and the main ``nano`` agent gets
one bound to the shared user profile + its own MEMORY.md.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from langchain_core.tools import StructuredTool

from agent.memory_store import MemoryStore

logger = logging.getLogger(__name__)


_MEMORY_TOOL_DESCRIPTION = (
    "Save durable information to persistent memory that survives across "
    "sessions. Memory is injected into future turns, so keep entries "
    "compact and focused on facts that will still matter later.\n\n"
    "WHEN TO SAVE (proactively, without being asked):\n"
    "- User corrects you or says 'remember this' / 'don't do that again'\n"
    "- User shares a preference, habit, or detail (name, role, timezone, "
    "trading style, risk tolerance)\n"
    "- You discover something about the environment (available markets, "
    "API quirks, paper portfolio setup)\n"
    "- You learn a convention or workflow specific to this setup\n"
    "- You identify a stable fact that will be useful again next session\n\n"
    "PRIORITY: User preferences + corrections > environment facts > "
    "procedural knowledge. The most valuable memory prevents the user "
    "from having to repeat themselves.\n\n"
    "Do NOT save: session-specific TODO state, individual trade details "
    "(the paper trading engine logs those), raw data dumps, information "
    "already in SOUL.md or agent-config.json, easily re-discovered facts.\n\n"
    "For reusable procedural knowledge (step-by-step recipes for recurring "
    "task types), save a skill with the skill_manage tool instead — memory "
    "is for facts, skills are for how-to.\n\n"
    "TWO TARGETS:\n"
    "- 'user': shared across every agent. Who the user is — name, role, "
    "preferences, communication style, pet peeves, risk tolerance. "
    "Writes here are visible to the trader, analyst, risk-manager, and "
    "every other agent on the next session.\n"
    "- 'memory': THIS agent's personal notes. Each role (trader, analyst, "
    "risk-manager, nano, etc.) has its own MEMORY.md — what you save here "
    "is only visible to future you, not to other agents.\n\n"
    "ACTIONS:\n"
    "- add: append a new entry\n"
    "- replace: update an existing entry. old_text just needs to be a "
    "unique substring of the entry you're updating — you don't have to "
    "echo the full text back.\n"
    "- remove: delete an entry by substring match\n\n"
    "When the memory block in your system prompt shows >80% usage, "
    "consolidate related entries into denser ones before adding new "
    "material."
)


def create_memory_tool(store: Optional[MemoryStore]) -> StructuredTool:
    """Build a ``memory`` tool bound to a specific ``MemoryStore`` instance.

    Each agent constructs its own tool (at agent-init time) so the
    store closure is per-instance. If ``store`` is ``None`` the tool
    still exists but every call returns a "memory disabled" error,
    which keeps the LLM's tool schema stable across agents regardless
    of whether memory is actually enabled for that role.
    """

    def _memory(
        action: str,
        target: str = "memory",
        content: str = "",
        old_text: str = "",
    ) -> str:
        if store is None:
            return json.dumps(
                {
                    "success": False,
                    "error": "Memory is disabled for this agent (see agent-config.json).",
                }
            )

        if target not in ("memory", "user"):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid target '{target}'. Use 'memory' or 'user'.",
                }
            )

        if action == "add":
            if not content:
                return json.dumps(
                    {"success": False, "error": "content is required for 'add'."}
                )
            result = store.add(target, content)
        elif action == "replace":
            if not old_text:
                return json.dumps(
                    {"success": False, "error": "old_text is required for 'replace'."}
                )
            if not content:
                return json.dumps(
                    {"success": False, "error": "content is required for 'replace'."}
                )
            result = store.replace(target, old_text, content)
        elif action == "remove":
            if not old_text:
                return json.dumps(
                    {"success": False, "error": "old_text is required for 'remove'."}
                )
            result = store.remove(target, old_text)
        else:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Unknown action '{action}'. Use: add, replace, remove.",
                }
            )

        return json.dumps(result, ensure_ascii=False)

    return StructuredTool.from_function(
        func=_memory,
        name="memory",
        description=_MEMORY_TOOL_DESCRIPTION,
    )

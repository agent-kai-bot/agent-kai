"""Reflection-based learning — captures per-session tool call history
so the mentor sub-agent can turn hard-won sessions into reusable skills.

The flow is:

1. Every ``SubAgent`` invocation runs with a ``ToolCallRecorder``
   callback attached. The recorder keeps the tool name, the JSON-ish
   input, the output preview, and whether the call errored out.
2. After ``executor.ainvoke`` returns, the SubAgent captures the final
   session state (task, response, tool calls, timestamp) in
   ``self.last_session``.
3. The TUI ``/learn`` slash command asks a specific sub-agent for its
   ``last_session``, bundles it with the agent's chat history + its
   ``existing_skills`` catalog, and sends the bundle to the mentor
   agent as a task.
4. The mentor returns a structured reply (``DECISION: create|patch|no_skill``,
   plus the skill draft if applicable). The TUI's ``/learn`` handler
   parses that reply and performs the ``SkillStore`` write directly
   on the TARGET agent's store (not the mentor's) so skills end up in
   the right library.
5. After any sub-agent task ends, the TUI checks the tool-call count —
   if ≥ ``NUDGE_THRESHOLD`` and no ``skill_manage(create)`` was
   performed during the session, it emits a one-line hint reminding
   the user that they can run ``/learn`` to capture the lesson.

This module intentionally does NOT depend on the TUI — it only owns
the callback + the bundle data structures. The TUI wires the bundle
into a mentor request and persists the result.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler

# ── Tuning knobs ────────────────────────────────────────────
#
# NUDGE_THRESHOLD is the tool-call count above which the TUI nudges
# the user to run ``/learn``. Set by the user at 3 (see design doc):
# "let's add nudge now >=3". Three is the minimum where learning is
# POSSIBLE (three distinct tool operations ≈ a non-trivial workflow)
# — it's not a guarantee that every session above it produced a new
# skill, which is fine, the mentor can honestly return no_skill.

NUDGE_THRESHOLD = 3

# Caps on bundle size — a reflection bundle injected into the mentor's
# input shouldn't blow out its context window. Keep the recent
# turns / calls and truncate large tool outputs.

MAX_CHAT_TURNS = 20
MAX_TOOL_CALLS = 30
MAX_OUTPUT_PREVIEW = 500


@dataclass
class ToolCall:
    """One invocation of a tool during a single agent run."""

    tool: str
    input: Any
    output: str = ""
    error: bool = False
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "input": self.input,
            "output": self.output,
            "error": self.error,
        }


@dataclass
class SessionRecord:
    """Everything the mentor needs to know about a finished session."""

    agent: str
    task: str
    response: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def tool_count(self) -> int:
        return len(self.tool_calls)

    def skill_was_created(self) -> bool:
        """True if this session contained a successful skill_manage(create)."""
        for call in self.tool_calls:
            if call.tool != "skill_manage":
                continue
            if call.error:
                continue
            # The input to skill_manage is a dict-like with an 'action' key.
            # LangChain may hand us a dict, a JSON string, or a repr — be
            # lenient.
            action = _extract_action(call.input)
            if action == "create":
                return True
        return False

    def to_bundle(self, chat_turns: List[str], existing_skills: List[Dict[str, str]]) -> Dict[str, Any]:
        """Render this record into a reflection bundle for the mentor."""
        trimmed_calls = [tc.to_dict() for tc in self.tool_calls[-MAX_TOOL_CALLS:]]
        trimmed_turns = chat_turns[-MAX_CHAT_TURNS:]
        return {
            "target_agent": self.agent,
            "original_task": self.task,
            "target_summary": self.response,
            "tool_calls": trimmed_calls,
            "tool_count": self.tool_count,
            "chat_turns": trimmed_turns,
            "existing_skills": existing_skills,
        }


def _extract_action(raw: Any) -> Optional[str]:
    """Best-effort pull of the ``action`` field from a tool input."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw.get("action")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed.get("action")
        except json.JSONDecodeError:
            pass
        m = re.search(r"action['\"]?\s*[:=]\s*['\"]?(\w+)", raw)
        if m:
            return m.group(1)
    return None


class ToolCallRecorder(BaseCallbackHandler):
    """LangChain callback that records every tool call for one agent run.

    One instance per ``_invoke`` call — the caller creates it, passes
    it as ``config={"callbacks": [recorder]}``, and then reads
    ``recorder.calls`` after the executor finishes.

    Tool starts and ends are correlated via LangChain's ``run_id``
    kwarg, NOT by a pop-from-stack — LangChain fires tools in parallel
    (via ``parallel_tool_calls=True`` which is on by default for
    OpenAI-compatible chat completions), and with a LIFO stack the
    output from tool B would end up glued onto tool A's record.

    Calls are appended to ``self.calls`` in start order, so downstream
    code sees the same order the LLM issued them. The output / error
    is filled in later when the matching ``run_id`` ends.
    """

    def __init__(self) -> None:
        self.calls: List[ToolCall] = []
        # run_id (str) → index into self.calls. We store the index
        # rather than the ToolCall object so start-order is preserved
        # even when multiple tools finish out of order.
        self._by_run_id: Dict[str, int] = {}
        # Fallback FIFO queue for the rare case where run_id is
        # missing (e.g. unit tests that don't pass one).
        self._fallback_idx: List[int] = []

    # ── Tool lifecycle ──────────────────────────────────────

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        name = (serialized or {}).get("name", "?")
        call = ToolCall(tool=name, input=_safe_parse_input(input_str))
        idx = len(self.calls)
        self.calls.append(call)
        if run_id is not None:
            self._by_run_id[str(run_id)] = idx
        else:
            self._fallback_idx.append(idx)

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        idx = self._resolve_index(run_id)
        if idx is None:
            return
        self.calls[idx].output = _truncate(str(output), MAX_OUTPUT_PREVIEW)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        idx = self._resolve_index(run_id)
        if idx is None:
            # Some errors fire without a prior on_tool_start — fabricate
            # a minimal entry so we don't drop the signal entirely.
            self.calls.append(
                ToolCall(tool="?", input=None, output=str(error), error=True)
            )
            return
        self.calls[idx].output = _truncate(str(error), MAX_OUTPUT_PREVIEW)
        self.calls[idx].error = True

    def _resolve_index(self, run_id: Any) -> Optional[int]:
        """Find the index of the pending call that matches this end event."""
        if run_id is not None:
            return self._by_run_id.pop(str(run_id), None)
        if self._fallback_idx:
            return self._fallback_idx.pop(0)
        return None


def _safe_parse_input(raw: str) -> Any:
    """Parse a tool input string as JSON when possible; fall back to raw."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return _truncate(raw, MAX_OUTPUT_PREVIEW)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ── Reflection persistence ──────────────────────────────────
#
# Every reflection run saves a JSON record to a timestamped file under
# ``eval_results/`` so both the user and Claude can review them later.
# The path is configurable so tests can point somewhere ephemeral.


DEFAULT_REFLECTION_DIR = Path("eval_results")


def save_reflection_record(
    bundle: Dict[str, Any],
    mentor_reply: str,
    outcome: Dict[str, Any],
    directory: Optional[Path] = None,
) -> Path:
    """Persist a reflection bundle + mentor reply + outcome to disk.

    Returns the path of the written file. Safe to call repeatedly —
    each call writes a new timestamped file.

    Directory defaults to ``eval_results/`` under the project root.
    """
    directory = directory or DEFAULT_REFLECTION_DIR
    directory.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    agent = bundle.get("target_agent", "unknown")
    path = directory / f"reflection-{ts}-{agent}.json"
    payload = {
        "timestamp": ts,
        "bundle": bundle,
        "mentor_reply": mentor_reply,
        "outcome": outcome,
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


# ── Mentor reply parsing ────────────────────────────────────
#
# The mentor agent returns a semi-structured text reply. We parse it
# out into a decision + skill draft using simple line-based markers
# (rather than forcing JSON, which LLMs mangle). The markers follow
# the format documented in ``workspaces/mentor/skills/how-to-reflect-on-a-session.md``.


# Markers are looked up case-insensitively (the mentor may write
# "decision:" or "DECISION:"), but the BODY grabbed from SKILL_CONTENT
# must be captured case-sensitively — otherwise the frontmatter keys
# (``name:``, ``description:``) look like section markers and the
# grab terminates immediately.
#
# Solution: the marker regexes are IGNORECASE; the content regex uses
# an explicit set of known terminator markers in ALL CAPS, no
# IGNORECASE flag. Body lines can be mixed-case without colliding.

DECISION_RE = re.compile(r"^\s*DECISION\s*:\s*(\w+)\s*$", re.MULTILINE | re.IGNORECASE)
TARGET_RE = re.compile(r"^\s*TARGET_AGENT\s*:\s*([\w\-]+)\s*$", re.MULTILINE | re.IGNORECASE)
NAME_RE = re.compile(r"^\s*SKILL_NAME\s*:\s*([\w\-]+)\s*$", re.MULTILINE | re.IGNORECASE)
OP_RE = re.compile(r"^\s*OP\s*:\s*(\w+)\s*$", re.MULTILINE | re.IGNORECASE)

# Terminator set for the content-grabbing regexes. These are the only
# section markers we recognize as "end of body" — other ALL_CAPS text
# in the body is safe because it needs to be followed by a colon.
_CONTENT_TERMINATORS = r"(?:DECISION|TARGET_AGENT|SKILL_NAME|OP|SKILL_CONTENT|OLD_STRING|NEW_STRING)"

CONTENT_RE = re.compile(
    rf"SKILL_CONTENT\s*:\s*\n(.+?)(?=\n{_CONTENT_TERMINATORS}\s*:|\Z)",
    re.DOTALL,
)
OLD_RE = re.compile(
    rf"OLD_STRING\s*:\s*\n?(.+?)(?=\n{_CONTENT_TERMINATORS}\s*:|\Z)",
    re.DOTALL,
)
NEW_RE = re.compile(
    rf"NEW_STRING\s*:\s*\n?(.+?)(?=\n{_CONTENT_TERMINATORS}\s*:|\Z)",
    re.DOTALL,
)

# Strip the outermost ```…``` wrapper a model often adds around its
# entire reply (and also any trailing standalone ``` on SKILL_CONTENT
# blocks). This is applied before the marker regexes run so fences
# never leak into captured content.
_FENCE_OPEN_RE = re.compile(r"^\s*```[\w]*\s*\n", re.MULTILINE)
_FENCE_CLOSE_RE = re.compile(r"\n```\s*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Remove a single outer ```…``` wrapper and dangling trailing fences."""
    stripped = text.strip()
    # Fast-path: whole reply wrapped in one fence.
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.split("\n")
        if len(lines) >= 2:
            # Drop the first fence (may have a language tag like ```md)
            lines = lines[1:]
            # Drop the last fence line
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines)
    # Also drop any trailing ``` that sits at the end of SKILL_CONTENT
    # when the model closed the inner fence but not the outer one.
    stripped = _FENCE_CLOSE_RE.sub("", stripped)
    return stripped


def parse_mentor_reply(reply: str) -> Dict[str, Any]:
    """Pull the decision + skill draft out of a mentor text reply.

    Returns a dict with ``decision`` set to ``create`` / ``patch`` /
    ``no_skill`` / ``unknown``. For ``create`` the dict has
    ``skill_name`` and ``content``. For ``patch`` the dict has
    ``skill_name``, ``old_string``, ``new_string``. For ``no_skill``
    only the decision is populated. ``target_agent`` is always
    populated if the mentor named one.
    """
    # Models often wrap their reply in one big code fence. Strip the
    # opening/closing fences (and the optional language tag) so the
    # marker regexes see the raw content.
    reply = _strip_code_fences(reply)
    result: Dict[str, Any] = {"decision": "unknown"}

    m_decision = DECISION_RE.search(reply)
    if m_decision:
        result["decision"] = m_decision.group(1).lower()

    m_target = TARGET_RE.search(reply)
    if m_target:
        result["target_agent"] = m_target.group(1)

    m_name = NAME_RE.search(reply)
    if m_name:
        result["skill_name"] = m_name.group(1)

    if result["decision"] == "create":
        m_content = CONTENT_RE.search(reply)
        if m_content:
            result["content"] = m_content.group(1).strip()

    if result["decision"] == "patch":
        m_old = OLD_RE.search(reply)
        m_new = NEW_RE.search(reply)
        if m_old:
            result["old_string"] = m_old.group(1).strip()
        if m_new:
            result["new_string"] = m_new.group(1).strip()

    return result

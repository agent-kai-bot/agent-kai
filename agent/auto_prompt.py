"""Prompt helpers for autonomous-mode execution."""

from __future__ import annotations

import re


_AUTO_STATE_RE = re.compile(
    r"^\[AUTO_STATE:\s*(done|continue|pause)(?:\s*\|\s*reason:\s*(.+?))?\]\s*$",
    re.IGNORECASE,
)


def build_auto_suffix(remaining_iterations: int) -> str:
    """Return the system-prompt suffix used while auto mode is active."""

    return (
        "## AUTONOMOUS MODE ACTIVE\n\n"
        "You are in autonomous mode. Execute tasks immediately:\n\n"
        "1. DO NOT ask for permission. Act, don't ask.\n"
        "2. After completing one step, proceed to the next.\n"
        "3. Think step-by-step but ACT without pausing.\n"
        "4. STOP ONLY when:\n"
        "   a. Task is genuinely complete\n"
        "   b. Error you cannot resolve\n"
        "   c. Need info not available via tools\n"
        "   d. A tool requires human approval (you'll be told)\n\n"
        "5. End every response with exactly one of:\n"
        "   [AUTO_STATE: done]\n"
        "   [AUTO_STATE: continue]\n"
        "   [AUTO_STATE: pause | reason: <why>]\n\n"
        f"6. Budget: {remaining_iterations} iterations remaining."
    )


def parse_auto_state(text: str) -> tuple[str, str | None]:
    """Parse the final AUTO_STATE footer from an agent response."""

    if not isinstance(text, str) or not text.strip():
        return ("unknown", None)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ("unknown", None)

    match = _AUTO_STATE_RE.match(lines[-1])
    if not match:
        return ("unknown", None)

    state = match.group(1).lower()
    reason = match.group(2).strip() if match.group(2) else None
    return (state, reason)

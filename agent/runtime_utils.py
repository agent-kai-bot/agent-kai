"""Runtime helpers for agent execution paths."""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar


EMPTY_RESPONSE_ERROR = "Error: agent returned an empty response."

_SESSION_WORKTREE: ContextVar[str | None] = ContextVar(
    "kai_session_worktree",
    default=None,
)
_SESSION_ENV_OVERLAY: ContextVar[dict[str, str] | None] = ContextVar(
    "kai_session_env_overlay",
    default=None,
)


def ensure_non_empty_response(response: str) -> str:
    """Normalize final agent output.

    Args:
        response: Candidate final response text from an agent run.

    Returns:
        The original response when non-empty, otherwise a stable error string.
    """
    if response and response.strip():
        return response
    return EMPTY_RESPONSE_ERROR


@contextmanager
def session_worktree_context(path: str | None):
    """Temporarily bind a session worktree for tool execution."""

    token = _SESSION_WORKTREE.set(path if path else None)
    try:
        yield
    finally:
        _SESSION_WORKTREE.reset(token)


def current_session_worktree() -> str | None:
    """Return the worktree path bound to the current session context."""

    return _SESSION_WORKTREE.get()


@contextmanager
def session_env_context(env_overlay: Mapping[str, str] | None):
    """Temporarily bind a session-specific subprocess environment overlay."""

    normalized = {
        str(key): str(value)
        for key, value in dict(env_overlay or {}).items()
        if key and value is not None
    }
    token = _SESSION_ENV_OVERLAY.set(normalized)
    try:
        yield
    finally:
        _SESSION_ENV_OVERLAY.reset(token)


def current_session_env_overlay() -> dict[str, str]:
    """Return a copy of the environment overlay bound to this execution context."""

    return dict(_SESSION_ENV_OVERLAY.get() or {})


def session_subprocess_env(
    *,
    extra_env: Mapping[str, str] | None = None,
    worktree: str | None = None,
) -> dict[str, str]:
    """Build a subprocess environment with session and per-call overlays."""

    env = os.environ.copy()
    overlay = current_session_env_overlay()
    if extra_env:
        overlay.update(
            {
                str(key): str(value)
                for key, value in dict(extra_env).items()
                if key and value is not None
            }
        )
    env.update(overlay)
    session_worktree = worktree if worktree is not None else current_session_worktree()
    if session_worktree:
        env["KAI_SESSION_WORKTREE"] = session_worktree
    return env

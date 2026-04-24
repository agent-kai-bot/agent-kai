"""Configuration helpers for the taskboard compatibility gateway."""

from __future__ import annotations

import os
from pathlib import Path

from config import WORKSPACES_DIR


def gateway_token() -> str:
    """Return the bearer token accepted by the compatibility gateway.

    Returns:
        Configured gateway bearer token, or an empty string when auth is off.
    """

    return (
        os.getenv("AGENT_GATEWAY_TOKEN", "").strip()
        or os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
        or os.getenv("OPENCLAW_TOKEN", "").strip()
    )


def allow_unauthenticated_local() -> bool:
    """Return whether localhost may call the gateway without a token.

    Returns:
        True when local clients may bypass bearer-token authentication.
    """

    return os.getenv("TASKBOARD_GATEWAY_ALLOW_LOCAL", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def runs_dir() -> Path:
    """Return the directory used for durable taskboard run records.

    Returns:
        Filesystem path for JSON run records.
    """

    raw = os.getenv("TASKBOARD_RUNS_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path(WORKSPACES_DIR) / "taskboard-runs"

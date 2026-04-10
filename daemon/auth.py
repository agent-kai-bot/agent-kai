"""Helpers for daemon bearer-token authentication."""

from __future__ import annotations

import secrets
from contextlib import suppress
from pathlib import Path

from config import WORKSPACES_DIR

DAEMON_TOKEN_PATH = Path(WORKSPACES_DIR) / "daemon-token.txt"
_LOCAL_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def read_daemon_token(token_path: Path = DAEMON_TOKEN_PATH) -> str | None:
    """Return the persisted daemon token when it exists."""
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def ensure_daemon_token(token_path: Path = DAEMON_TOKEN_PATH) -> str:
    """Return the daemon token, creating it on first use."""
    existing = read_daemon_token(token_path)
    if existing:
        return existing

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    token_path.write_text(f"{token}\n", encoding="utf-8")
    with suppress(OSError):
        token_path.chmod(0o600)
    return token


def parse_bearer_token(value: str | None) -> str | None:
    """Extract the raw token from an Authorization header."""
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    normalized = token.strip()
    return normalized or None


def is_local_client_host(host: str | None) -> bool:
    """Return True when the request originated from the local machine."""
    if not host:
        return False
    normalized = host.strip().lower()
    return normalized in _LOCAL_CLIENT_HOSTS

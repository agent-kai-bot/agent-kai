"""Claude Code subscription OAuth authentication.

Lets the agent talk to ``https://api.anthropic.com/v1/messages`` using the
same Claude.ai subscription OAuth credentials Claude Code stores at
``~/.claude/.credentials.json``. Users on Claude Pro / Max / Team /
Enterprise can run agents against their subscription quota with no API key.

The common path is reusing an existing Claude Code login. If the operator
already ran ``claude auth login --claudeai`` or logged in through Claude Code,
``~/.claude/.credentials.json`` exists and we load it. Tokens are refreshed
automatically when within ``REFRESH_GRACE_SECONDS`` of expiry.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

CLIENT_ID = os.getenv(
    "CLAUDE_CODE_OAUTH_CLIENT_ID",
    "https://claude.ai/oauth/claude-code-client-metadata",
)
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_BETA = "oauth-2025-04-20"
REFRESH_GRACE_SECONDS = 5 * 60
DEFAULT_AUTH_PATH = Path.home() / ".claude" / ".credentials.json"


def redact_token(token: str | None) -> str:
    """Return a log-safe token preview."""
    if not token:
        return ""
    return f"{token[:8]}..."


def _parse_expires_at(value: Any) -> int:
    """Parse Claude Code ``expiresAt`` into Unix epoch seconds."""
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, (int, float)):
        return 0
    # Claude Code stores milliseconds since epoch. Accept seconds too so tests
    # and future formats do not get forced into the expired path.
    if value > 10_000_000_000:
        return int(value / 1000)
    return int(value)


def _expires_at_to_millis(expires_at: int) -> int:
    return int(expires_at * 1000) if expires_at else 0


def _normalize_scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in value.split() if part]
    if isinstance(value, list):
        return [str(part) for part in value if isinstance(part, str) and part]
    return []


@dataclasses.dataclass
class ClaudeCredentials:
    """A loaded set of Claude Code OAuth credentials."""

    access_token: str
    refresh_token: str
    expires_at: int
    subscription_type: str = ""
    rate_limit_tier: str = ""
    scopes: list[str] = dataclasses.field(default_factory=list)

    def is_expired(self, grace_seconds: int = REFRESH_GRACE_SECONDS) -> bool:
        if self.expires_at <= 0:
            return True
        return time.time() + grace_seconds >= self.expires_at

    def to_auth_json(self) -> dict:
        """Render credentials in Claude Code's on-disk format."""
        return {
            "claudeAiOauth": {
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "expiresAt": _expires_at_to_millis(self.expires_at),
                "scopes": list(self.scopes),
                "subscriptionType": self.subscription_type,
                "rateLimitTier": self.rate_limit_tier,
            }
        }


def load_credentials(path: Path = DEFAULT_AUTH_PATH) -> Optional[ClaudeCredentials]:
    """Read ``~/.claude/.credentials.json`` and return parsed credentials.

    Returns None if the file does not exist or is malformed.
    """
    if not path.is_file():
        logger.debug("Claude auth file not found at %s", path)
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read Claude credentials file: %s", exc)
        return None

    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
        return None

    return ClaudeCredentials(
        access_token=access,
        refresh_token=refresh,
        expires_at=_parse_expires_at(oauth.get("expiresAt")),
        subscription_type=str(oauth.get("subscriptionType") or ""),
        rate_limit_tier=str(oauth.get("rateLimitTier") or ""),
        scopes=_normalize_scopes(oauth.get("scopes")),
    )


def save_credentials(creds: ClaudeCredentials, path: Path = DEFAULT_AUTH_PATH) -> None:
    """Atomically write credentials to ``~/.claude/.credentials.json``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(creds.to_auth_json(), f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def refresh_access_token(
    refresh_token: str,
    *,
    http_client: httpx.Client | None = None,
) -> dict:
    """Exchange a refresh token for a fresh Claude access token payload."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": "kai-agent (linux)",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }

    close_client = False
    client = http_client
    if client is None:
        client = httpx.Client(timeout=15)
        close_client = True
    try:
        response = client.post(TOKEN_URL, data=data, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Claude token refresh failed: {exc}") from exc
    finally:
        if close_client:
            client.close()
    if not isinstance(payload, dict):
        raise RuntimeError("Claude token refresh response was not a JSON object")
    return payload


def refresh_credentials(creds: ClaudeCredentials) -> ClaudeCredentials:
    """Return a refreshed credential set, preserving stable metadata."""
    payload = refresh_access_token(creds.refresh_token)
    new_access = payload.get("access_token") or payload.get("accessToken")
    new_refresh = (
        payload.get("refresh_token")
        or payload.get("refreshToken")
        or creds.refresh_token
    )
    if not isinstance(new_access, str) or not new_access:
        raise RuntimeError("Claude refresh response missing access_token")
    if not isinstance(new_refresh, str) or not new_refresh:
        raise RuntimeError("Claude refresh response missing refresh_token")

    expires_at = _parse_expires_at(payload.get("expiresAt") or payload.get("expires_at"))
    expires_in = payload.get("expires_in") or payload.get("expiresIn")
    if expires_at <= 0 and isinstance(expires_in, (int, float)):
        expires_at = int(time.time() + expires_in)

    return ClaudeCredentials(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_at=expires_at,
        subscription_type=str(
            payload.get("subscriptionType")
            or payload.get("subscription_type")
            or creds.subscription_type
        ),
        rate_limit_tier=str(
            payload.get("rateLimitTier")
            or payload.get("rate_limit_tier")
            or creds.rate_limit_tier
        ),
        scopes=_normalize_scopes(payload.get("scopes") or payload.get("scope"))
        or list(creds.scopes),
    )


def get_valid_credentials(
    path: Path = DEFAULT_AUTH_PATH,
    *,
    force_refresh: bool = False,
) -> Optional[ClaudeCredentials]:
    """Load credentials and refresh them if expired. Persists the refresh."""
    creds = load_credentials(path)
    if creds is None:
        return None
    if not force_refresh and not creds.is_expired():
        return creds

    if force_refresh:
        logger.info("Claude access token rejected - refreshing")
    else:
        logger.info("Claude access token expired or near expiry - refreshing")
    refreshed = refresh_credentials(creds)
    try:
        save_credentials(refreshed, path)
    except OSError as exc:
        logger.warning("Failed to persist refreshed Claude credentials: %s", exc)
    return refreshed


def login() -> None:
    """Tell the operator how to create Claude Code OAuth credentials."""
    raise RuntimeError(
        "Claude OAuth login is handled by Claude Code. Run "
        "`claude auth login --claudeai`, then ensure "
        f"{DEFAULT_AUTH_PATH} exists."
    )

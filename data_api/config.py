"""Data API configuration."""

from __future__ import annotations

import os


def _env_list(name: str, default: list[str]) -> list[str]:
    """Read a comma-separated list from the environment.

    Args:
        name: Environment variable name.
        default: Default list when the variable is unset or empty.

    Returns:
        A normalized list of non-empty uppercase tokens.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    values = [item.strip().upper() for item in raw.split(",")]
    return [item for item in values if item]


API_HOST = os.getenv("KAI_API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("KAI_API_PORT", "8877"))
NATS_URL = os.getenv("KAI_NATS_URL", "nats://localhost:4222")

AGENT_KAI_BASE_URL = os.getenv("AGENT_KAI_BASE_URL", "https://agent-k.ai").rstrip("/")
AGENT_KAI_WS_URL = os.getenv("AGENT_KAI_WS_URL", f"{AGENT_KAI_BASE_URL}/v1/ws")
AGENT_KAI_API_KEY_ENV = os.getenv("AGENT_KAI_API_KEY_ENV", "AGENT_KAI_API_KEY")
AGENT_KAI_API_KEY = os.getenv(AGENT_KAI_API_KEY_ENV, "").strip()
AGENT_KAI_HTTP_TIMEOUT_SECONDS = float(os.getenv("AGENT_KAI_HTTP_TIMEOUT_SECONDS", "20"))
AGENT_KAI_WS_BACKOFF_SECONDS = float(os.getenv("AGENT_KAI_WS_BACKOFF_SECONDS", "1"))
AGENT_KAI_MAX_BACKOFF_SECONDS = float(os.getenv("AGENT_KAI_MAX_BACKOFF_SECONDS", "30"))

TRACKED_SYMBOLS = _env_list("KAI_TRACKED_SYMBOLS", ["BTC", "ETH", "SOL"])
BRIDGE_INTERVALS = _env_list("KAI_BRIDGE_INTERVALS", ["1M"])
BRIDGE_INTERVALS = [interval.lower() for interval in BRIDGE_INTERVALS]

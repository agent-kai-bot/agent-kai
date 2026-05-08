"""Feature flag helpers for the daemon signal router."""

from __future__ import annotations

import os
from enum import Enum
from typing import Any


class SignalRouterMode(str, Enum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    NEW = "new"


def kill_switch_active() -> bool:
    """Return whether the environment kill switch forces legacy mode."""

    return os.getenv("KAI_SIGNAL_ROUTER_KILL_SWITCH", "").strip() == "1"


def _signal_router_block(config_block: dict[str, Any]) -> dict[str, Any]:
    daemon_block = config_block.get("daemon")
    if isinstance(daemon_block, dict):
        signal_router_block = daemon_block.get("signal_router")
        if isinstance(signal_router_block, dict):
            return signal_router_block
    return config_block


def resolve_mode(config_block: dict[str, Any]) -> SignalRouterMode:
    """Resolve daemon.signal_router.mode, defaulting to legacy."""

    if kill_switch_active():
        return SignalRouterMode.LEGACY
    router_block = _signal_router_block(config_block or {})
    raw_mode = str(router_block.get("mode", SignalRouterMode.LEGACY.value)).strip()
    try:
        return SignalRouterMode(raw_mode)
    except ValueError:
        return SignalRouterMode.LEGACY

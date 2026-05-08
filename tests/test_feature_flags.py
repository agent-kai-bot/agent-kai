from __future__ import annotations

from daemon.signal_router import SignalRouterMode
from daemon.signal_router.feature_flags import kill_switch_active, resolve_mode


def test_mode_resolution_from_router_block() -> None:
    assert resolve_mode({}) == SignalRouterMode.LEGACY
    assert resolve_mode({"mode": "shadow"}) == SignalRouterMode.SHADOW
    assert resolve_mode({"mode": "new"}) == SignalRouterMode.NEW


def test_mode_resolution_from_full_config() -> None:
    config = {"daemon": {"signal_router": {"mode": "new"}}}

    assert resolve_mode(config) == SignalRouterMode.NEW


def test_kill_switch_overrides_new_to_legacy(monkeypatch) -> None:
    monkeypatch.setenv("KAI_SIGNAL_ROUTER_KILL_SWITCH", "1")

    assert kill_switch_active() is True
    assert resolve_mode({"mode": "new"}) == SignalRouterMode.LEGACY

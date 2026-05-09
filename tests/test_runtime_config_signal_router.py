from __future__ import annotations

import json
from pathlib import Path

from daemon.runtime_config_store import RuntimeConfigStore


def _write_base_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "daemon": {
                    "signal_router": {
                        "mode": "shadow",
                        "channels": {},
                        "routes": [
                            {
                                "name": "route-a",
                                "enabled": True,
                                "channel": "alerts",
                                "match": {},
                                "actions": [],
                            }
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_live_trades_enabled_round_trip(tmp_path: Path) -> None:
    base_path = tmp_path / "agent-config.json"
    override_path = tmp_path / "runtime_config.json"
    _write_base_config(base_path)
    store = RuntimeConfigStore(base_config_path=base_path, overrides_path=override_path)

    assert store.get_signal_router_live_trades_enabled() is False

    store.update_signal_router_live_trades_enabled(True)

    assert store.get_signal_router_live_trades_enabled() is True


def test_per_route_enabled_round_trip(tmp_path: Path) -> None:
    base_path = tmp_path / "agent-config.json"
    override_path = tmp_path / "runtime_config.json"
    _write_base_config(base_path)
    store = RuntimeConfigStore(base_config_path=base_path, overrides_path=override_path)

    assert store.get_signal_router_route_enabled("route-a", default=True) is True

    store.update_signal_router_route_enabled("route-a", False)

    assert store.get_signal_router_route_enabled("route-a", default=True) is False
    effective_routes = store.effective_config()["daemon"]["signal_router"]["routes"]
    assert effective_routes[0]["name"] == "route-a"
    assert effective_routes[0]["enabled"] is False


def test_signal_router_overrides_persist_after_reload(tmp_path: Path) -> None:
    base_path = tmp_path / "agent-config.json"
    override_path = tmp_path / "runtime_config.json"
    _write_base_config(base_path)
    store = RuntimeConfigStore(base_config_path=base_path, overrides_path=override_path)

    store.update_signal_router_live_trades_enabled(True)
    store.update_signal_router_route_enabled("route-a", False)
    reloaded = RuntimeConfigStore(base_config_path=base_path, overrides_path=override_path)

    assert reloaded.get_signal_router_live_trades_enabled() is True
    assert reloaded.get_signal_router_route_enabled("route-a", default=True) is False

from __future__ import annotations

from unittest import mock

from daemon.server import DaemonServer
from daemon.signal_router import RouterDedupTable, SignalRouter


def test_construction_with_empty_config(tmp_path) -> None:
    router = SignalRouter(
        {},
        dedup_table=RouterDedupTable(tmp_path / "dedup.sqlite3"),
    )

    assert router.mode.value == "legacy"
    assert router.routes == {}
    assert router.channels == {}


def test_route_returns_none_for_phase_1_stub(tmp_path) -> None:
    router = SignalRouter(
        {},
        dedup_table=RouterDedupTable(tmp_path / "dedup.sqlite3"),
    )

    assert router.route({"subject": "signals.BTC", "payload": {"symbol": "BTC"}}) is None


def test_channel_lookup_by_subject_pattern(tmp_path) -> None:
    router = SignalRouter(
        {
            "channels": {
                "trade_signals": {
                    "subjects": ["signals.>"],
                    "schema": "trade_signal",
                },
                "ai_analyses": {
                    "subjects": ["ai.analysis.completed"],
                    "schema": "ai_analysis",
                },
            },
            "routes": [],
        },
        dedup_table=RouterDedupTable(tmp_path / "dedup.sqlite3"),
    )

    assert router.find_channel_for_subject("signals.BTC").name == "trade_signals"
    assert router.find_channel_for_subject("signals.crypto.BTC").name == "trade_signals"
    assert router.find_channel_for_subject("ai.analysis.completed").name == "ai_analyses"
    assert router.find_channel_for_subject("orders.BTC") is None


def test_overlay_can_enable_a_config_disabled_route(tmp_path) -> None:
    """Operator UI must be able to flip a route ON whose config default is False."""

    class _FakeStore:
        def __init__(self) -> None:
            self._values: dict[str, bool] = {}

        def get_signal_router_route_enabled(self, name: str, *, default: bool) -> bool:
            return self._values.get(name, default)

        def set(self, name: str, value: bool) -> None:
            self._values[name] = value

    store = _FakeStore()
    router = SignalRouter(
        {
            "routes": [
                {"name": "off-by-default", "enabled": False, "channel": "x", "actions": []},
                {"name": "on-by-default", "enabled": True, "channel": "x", "actions": []},
            ],
        },
        dedup_table=RouterDedupTable(tmp_path / "dedup.sqlite3"),
        runtime_config_store=store,
    )

    off_route = router.routes["off-by-default"]
    on_route = router.routes["on-by-default"]

    assert router.is_route_enabled(off_route) is False
    assert router.is_route_enabled(on_route) is True

    store.set("off-by-default", True)
    store.set("on-by-default", False)

    assert router.is_route_enabled(off_route) is True
    assert router.is_route_enabled(on_route) is False


def test_daemon_signal_router_health_shape(tmp_path) -> None:
    config = {
        "daemon": {
            "signal_router": {
                "mode": "legacy",
                "channels": {},
                "routes": [],
                "dedup_table_path": str(tmp_path / "router_dedup.sqlite3"),
            },
            "heartbeat": {"enabled": False},
        }
    }
    with mock.patch("daemon.server.get_agent_config", return_value=config):
        server = DaemonServer()

    assert server._signal_router_health() == {
        "mode": "legacy",
        "routes_loaded": 0,
        "channels_loaded": 0,
        "dedup_keys_count": 0,
        "kill_switch_active": False,
        "live_trades_enabled": False,
        "routes_enabled_count": 0,
        "routes_disabled_count": 0,
        "shadow_running": False,
        "diff_metrics": {},
    }

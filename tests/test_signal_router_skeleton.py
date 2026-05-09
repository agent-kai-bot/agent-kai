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

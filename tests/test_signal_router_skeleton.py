from __future__ import annotations

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

from __future__ import annotations

import json
from pathlib import Path

from agent.signal_handlers import CooldownTracker, SignalHandler, matches as legacy_matches
from daemon.signal_router import route_matches
from daemon.signal_router.shim_signal_handlers import (
    generate_parity_fixtures,
    route_cooldown_key,
    translate_signal_handlers_config,
)


ROOT = Path(__file__).resolve().parents[1]


def test_actual_agent_config_signal_handlers_have_zero_match_divergence() -> None:
    config = json.loads((ROOT / "agent-config.json").read_text(encoding="utf-8"))
    routes, errors = translate_signal_handlers_config(config, mode="shadow")
    route_by_name = {route.name.removeprefix("legacy:"): route for route in routes}

    assert errors == []

    fixture_count = 0
    divergences = []
    for raw in config["signal_handlers"]:
        handler = SignalHandler.from_dict(raw)
        route = route_by_name[handler.name]
        for label, event in generate_parity_fixtures(raw):
            fixture_count += 1
            legacy_result = legacy_matches(handler, event)
            router_result = route_matches(route, event)
            if legacy_result != router_result:
                divergences.append((handler.name, label, legacy_result, router_result))

    assert fixture_count == 10
    assert divergences == []


def test_cooldown_key_and_ttl_boundary_match_legacy(monkeypatch) -> None:
    config = {
        "signal_handlers": [
            {
                "name": "ttl",
                "match": {"symbol": "BTC"},
                "action": "chat_message",
                "template": "{symbol}",
                "cooldown_seconds": 10,
            }
        ]
    }
    routes, errors = translate_signal_handlers_config(config, mode="shadow")
    assert errors == []
    route = routes[0]
    handler = SignalHandler.from_dict(config["signal_handlers"][0])
    tracker = CooldownTracker()
    now = {"value": 1000.0}

    monkeypatch.setattr("agent.signal_handlers.time.time", lambda: now["value"])

    key = route_cooldown_key(route, "btc")
    assert key == (route.name, "BTC")
    assert tracker.can_fire(handler.name, "btc", handler.cooldown_seconds) is True
    tracker.mark_fired(handler.name, "btc")
    router_last_fired = {key: now["value"]}

    assert tracker.can_fire(handler.name, "btc", handler.cooldown_seconds) is False
    assert ((now["value"] - router_last_fired[key]) >= route.cooldown_seconds) is False

    now["value"] = 1010.0
    assert tracker.can_fire(handler.name, "btc", handler.cooldown_seconds) is True
    assert ((now["value"] - router_last_fired[key]) >= route.cooldown_seconds) is True

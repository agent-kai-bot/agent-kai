from __future__ import annotations

import json
from pathlib import Path

from config import CONFIG_PATH
from daemon.signal_router import SignalRouter


def test_polymarket_route_loads_from_agent_config() -> None:
    config = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    router_config = config["daemon"]["signal_router"]

    router = SignalRouter(router_config)

    route = router.routes["polymarket-alarm-response"]
    assert route.name == "polymarket-alarm-response"
    assert route.enabled is True
    assert route.channel == "polymarket_alarms"
    assert router.channels["polymarket_alarms"].subjects == [
        "polymarket.alpha.alarm.>"
    ]


def test_polymarket_inject_session_action_wiring_is_canonical() -> None:
    config = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    router = SignalRouter(config["daemon"]["signal_router"])
    action = router.routes["polymarket-alarm-response"].actions[0]

    assert action.kind == "inject_session"
    assert action.params["require_auto_mode"] is False
    assert action.params["single_auto_iteration"] is True
    assert action.params["max_per_hour"] == 60
    assert action.params["prefetch_polymarket_bbo"] is True
    assert action.params["prefetch_polymarket_token_info"] is True
    assert action.params["cooldown_key_template"] == "{rule_id}:{token_id}"
    assert action.params["cooldown_seconds"] == 600
    assert action.params["daily_cap"] == 50
    assert action.params["hourly_cap"] == 10
    assert "POLYMARKET ALARM" in action.params["template_inline"]

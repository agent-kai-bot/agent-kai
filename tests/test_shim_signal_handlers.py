from __future__ import annotations

from daemon.signal_router.shim_signal_handlers import (
    route_cooldown_key,
    translate_signal_handlers_config,
)


def _translate(handler: dict):
    routes, errors = translate_signal_handlers_config(
        {"agents": {"analyst": {}, "trader": {}}, "signal_handlers": [handler]},
        mode="shadow",
    )
    assert errors == []
    assert len(routes) == 1
    return routes[0]


def test_dispatch_agent_translates_to_inject_session() -> None:
    route = _translate(
        {
            "name": "to-analyst",
            "match": {"signal_type": "BUY"},
            "action": "dispatch_agent",
            "agent": "analyst",
            "task_template": "review {symbol}",
            "cooldown_seconds": 60,
        }
    )

    assert route.name == "legacy:to-analyst"
    assert route.actions[0].kind == "inject_session"
    assert route.actions[0].target == "analyst"
    assert route.actions[0].params["template_inline"] == "review {symbol}"
    assert route.cooldown_seconds == 60


def test_dispatch_kai_translates_task_template_or_template() -> None:
    route = _translate(
        {
            "name": "to-kai",
            "match": {"signal_type": "SELL"},
            "action": "dispatch_kai",
            "template": "chat {symbol}",
        }
    )

    assert route.actions[0].kind == "inject_session"
    assert route.actions[0].target == "kai"
    assert route.actions[0].params["template_inline"] == "chat {symbol}"


def test_chat_message_translates_to_chat_notify() -> None:
    route = _translate(
        {
            "name": "chat",
            "match": {"source": "ai-token-analyzer"},
            "action": "chat_message",
            "template": "analysis {symbol}",
        }
    )

    assert route.channel == "ai_analyses"
    assert route.actions[0].kind == "notify"
    assert route.actions[0].target == "chat"
    assert route.actions[0].params["template_inline"] == "analysis {symbol}"


def test_publish_translates_to_nats_notify() -> None:
    route = _translate(
        {
            "name": "publish",
            "match": {"signal_type": "BUY"},
            "action": "publish",
            "subject": "signals.filtered",
            "template": "{symbol}",
        }
    )

    assert route.actions[0].kind == "notify"
    assert route.actions[0].target == "nats"
    assert route.actions[0].params["subject"] == "signals.filtered"
    assert route.actions[0].params["template_inline"] == "{symbol}"


def test_publish_without_template_marks_raw_event() -> None:
    route = _translate(
        {
            "name": "publish-raw",
            "match": {"signal_type": "BUY"},
            "action": "publish",
            "subject": "signals.filtered",
        }
    )

    assert route.actions[0].params["raw_event"] is True


def test_webhook_translates_to_webhook_notify() -> None:
    route = _translate(
        {
            "name": "webhook",
            "match": {"signal_type": "BUY"},
            "action": "webhook",
            "url": "https://example.invalid/hook",
            "template": "{symbol}",
        }
    )

    assert route.actions[0].kind == "notify"
    assert route.actions[0].target == "webhook"
    assert route.actions[0].params["url"] == "https://example.invalid/hook"
    assert route.actions[0].params["template_inline"] == "{symbol}"


def test_implicit_trader_gate_forces_requires_autotrade() -> None:
    route = _translate(
        {
            "name": "trade",
            "match": {"signal_type": "BUY"},
            "action": "dispatch_agent",
            "agent": "Trader",
            "task_template": "trade {symbol}",
            "requires_autotrade": False,
        }
    )

    assert route.requires_autotrade is True
    assert route.actions[0].target == "Trader"


def test_cooldown_key_format_preserved() -> None:
    route = _translate(
        {
            "name": "cooldown",
            "match": {"symbol": "btc"},
            "action": "chat_message",
            "template": "{symbol}",
        }
    )

    assert route_cooldown_key(route, "btc") == ("legacy:cooldown", "BTC")

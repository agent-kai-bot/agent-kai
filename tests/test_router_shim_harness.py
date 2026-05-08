"""Reusable Phase 1 signal-router shim harness helpers."""

from __future__ import annotations

from typing import Any

import pytest

from daemon.signal_router import ActionDescriptor, Channel, Route, RouterDedupTable


def make_test_envelope(
    rule_id: str = "rule-1",
    token_id: str = "token-1",
    **extra: Any,
) -> dict[str, Any]:
    """Build a NATS-message-like envelope for router tests."""

    payload = {
        "rule_id": rule_id,
        "token_id": token_id,
        "symbol": extra.pop("symbol", "BTC"),
        "signal_type": extra.pop("signal_type", "BUY"),
    }
    payload.update(extra.pop("payload", {}))
    return {
        "subject": extra.pop("subject", "signals.BTC"),
        "headers": extra.pop("headers", {}),
        "payload": payload,
        **extra,
    }


def make_test_channel(
    name: str = "trade_signals",
    subjects: list[str] | None = None,
    schema: str | None = "trade_signal",
) -> Channel:
    return Channel(
        name=name,
        subjects=subjects or ["signals.>"],
        schema=schema,
    )


def make_test_route(
    name: str = "test-route",
    channel: str = "trade_signals",
    match: dict[str, Any] | None = None,
    actions: list[ActionDescriptor] | None = None,
    pre_action: dict[str, Any] | None = None,
    enabled: bool = True,
) -> Route:
    return Route(
        name=name,
        channel=channel,
        match=match or {},
        actions=actions
        or [ActionDescriptor(kind="ui_panel", target="signals", params={})],
        pre_action=pre_action,
        enabled=enabled,
    )


@pytest.fixture
def signal_router_test_db(tmp_path):
    table = RouterDedupTable(tmp_path / "router_dedup.sqlite3")
    try:
        yield table
    finally:
        table.close()

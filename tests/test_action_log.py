from __future__ import annotations

import json

from daemon.signal_router import ActionDescriptor, ExecutionContext
from daemon.signal_router.actions.log import LogExecutor


def test_log_writes_structured_log_line_format() -> None:
    audit = []

    result = LogExecutor().execute(
        ActionDescriptor(kind="log", target="audit", params={"template_inline": "{symbol} logged"}),
        {"subject": "signals.BTC", "payload": {"symbol": "BTC"}},
        ExecutionContext(channel="trade_signals", route_name="r1", audit_writer=audit.append),
    )

    assert result.status == "fired"
    record = json.loads(result.detail)
    assert record["kind"] == "signal_router.action"
    assert record["action_kind"] == "log"
    assert record["message"] == "BTC logged"
    assert audit[0]["route_name"] == "r1"

from __future__ import annotations

from daemon.signal_router import ActionDescriptor, ExecutionContext
from daemon.signal_router.actions.alert import AlertExecutor

from tests.test_action_helpers import FakeSession


def test_alert_emits_high_salience_telemetry_and_ui_event() -> None:
    telemetry = []
    session = FakeSession()

    result = AlertExecutor().execute(
        ActionDescriptor(kind="alert", target="operator", params={"severity": "critical", "template_inline": "{symbol}"}),
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(
            channel="polymarket_alarms",
            sessions={"kai": session},
            telemetry_emitter=lambda topic, payload: telemetry.append((topic, payload)),
        ),
    )

    assert result.status == "fired"
    assert telemetry[0][0] == "auto.signal_router.alert"
    assert telemetry[0][1]["severity"] == "critical"
    assert session.events[0][0] == "signal.received"
    assert session.events[0][1]["signal"]["category"] == "alerts"

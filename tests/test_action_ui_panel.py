from __future__ import annotations

from daemon.signal_router import ActionDescriptor, ExecutionContext
from daemon.signal_router.actions.ui_panel import UIPanelExecutor

from tests.test_action_helpers import FakeSession


def test_ui_panel_preserves_signal_envelope_and_adds_category_channel() -> None:
    session = FakeSession()
    bus_events = []
    action = ActionDescriptor(kind="ui_panel", target="signals", params={})
    envelope = {
        "subject": "signals.clucmay02.BTC",
        "payload": {"symbol": "BTC", "signal_type": "BUY"},
    }

    result = UIPanelExecutor().execute(
        action,
        envelope,
        ExecutionContext(
            channel="trade_signals",
            sessions={"kai": session},
            daemon_event_publisher=lambda channel, payload: bus_events.append((channel, payload)),
        ),
    )

    assert result.status == "fired"
    assert session.events == [
        (
            "signal.received",
            {
                "signal": {
                    "symbol": "BTC",
                    "signal_type": "BUY",
                    "subject": "signals.clucmay02.BTC",
                    "category": "signals",
                    "channel": "trade_signals",
                }
            },
        )
    ]
    assert bus_events == [
        (
            "signals",
            {
                "symbol": "BTC",
                "signal_type": "BUY",
                "subject": "signals.clucmay02.BTC",
                "category": "signals",
                "channel": "trade_signals",
            },
        )
    ]

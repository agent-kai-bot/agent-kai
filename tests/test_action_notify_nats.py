from __future__ import annotations

from daemon.signal_router import ActionDescriptor, ExecutionContext
from daemon.signal_router.actions.notify import NotifyExecutor


def test_notify_nats_publishes_rendered_template() -> None:
    published = []
    action = ActionDescriptor(
        kind="notify",
        target="nats",
        params={"subject": "signals.filtered", "template_inline": "{symbol} ok"},
    )

    result = NotifyExecutor().execute(
        action,
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(nats_publisher=lambda subject, payload: published.append((subject, payload))),
    )

    assert result.status == "fired"
    assert published == [("signals.filtered", {"message": "BTC ok"})]


def test_notify_nats_raw_event() -> None:
    published = []
    envelope = {"subject": "signals.BTC", "payload": {"symbol": "BTC"}}

    NotifyExecutor().execute(
        ActionDescriptor(kind="notify", target="nats", params={"subject": "raw.signals", "raw_event": True}),
        envelope,
        ExecutionContext(nats_publisher=lambda subject, payload: published.append((subject, payload))),
    )

    assert published == [("raw.signals", envelope)]

from __future__ import annotations

from daemon.signal_router import ActionDescriptor, ExecutionContext
from daemon.signal_router.actions.notify import NotifyExecutor


def test_notify_chat_renders_template_to_chat_logger() -> None:
    messages = []

    result = NotifyExecutor().execute(
        ActionDescriptor(kind="notify", target="chat", params={"template_inline": "{symbol} fired"}),
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(chat_logger=messages.append),
    )

    assert result.status == "fired"
    assert messages == ["BTC fired"]

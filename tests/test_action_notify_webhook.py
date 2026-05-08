from __future__ import annotations

from daemon.signal_router import ActionDescriptor, ExecutionContext
from daemon.signal_router.actions.notify import NotifyExecutor


def test_notify_webhook_posts_url_and_template() -> None:
    posts = []

    result = NotifyExecutor().execute(
        ActionDescriptor(
            kind="notify",
            target="webhook",
            params={"url": "https://example.invalid/hook", "template_inline": "{symbol} fired"},
        ),
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(webhook_poster=lambda url, payload: posts.append((url, payload))),
    )

    assert result.status == "fired"
    assert posts[0][0] == "https://example.invalid/hook"
    assert posts[0][1]["message"] == "BTC fired"


def test_notify_webhook_http_error_returns_failed_without_raising() -> None:
    telemetry = []

    def boom(_url, _payload):
        raise RuntimeError("bad gateway")

    result = NotifyExecutor().execute(
        ActionDescriptor(
            kind="notify",
            target="webhook",
            params={"url": "https://example.invalid/hook", "template_inline": "{symbol}"},
        ),
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(
            webhook_poster=boom,
            telemetry_emitter=lambda topic, payload: telemetry.append((topic, payload)),
        ),
    )

    assert result.status == "failed"
    assert telemetry[0][0] == "auto.notify.webhook.failed"

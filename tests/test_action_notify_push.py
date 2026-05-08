from __future__ import annotations

from daemon.signal_router import ActionDescriptor, ExecutionContext
from daemon.signal_router.actions.notify import NotifyExecutor


class Response:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


def _push_action(**params):
    merged = {
        "template_inline": "{symbol} fired",
        "backends": ["pushover", "ntfy", "log_only"],
        "pushover": {"priority": 1},
        "ntfy": {"priority": 3},
    }
    merged.update(params)
    return ActionDescriptor(kind="notify", target="push", params=merged)


def test_push_pushover_success_stops_fallback(monkeypatch) -> None:
    monkeypatch.setenv("KAI_PUSHOVER_USER", "u")
    monkeypatch.setenv("KAI_PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("KAI_NTFY_TOPIC", "topic")
    posts = []

    result = NotifyExecutor().execute(
        _push_action(),
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(http_poster=lambda url, **kwargs: posts.append((url, kwargs)) or Response()),
    )

    assert result.status == "fired"
    assert result.detail == "pushover"
    assert len(posts) == 1
    assert "pushover" in posts[0][0]


def test_push_pushover_5xx_falls_back_to_ntfy(monkeypatch) -> None:
    monkeypatch.setenv("KAI_PUSHOVER_USER", "u")
    monkeypatch.setenv("KAI_PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("KAI_NTFY_TOPIC", "topic")
    posts = []

    def post(url, **kwargs):
        posts.append((url, kwargs))
        return Response(500 if "pushover" in url else 200)

    result = NotifyExecutor().execute(
        _push_action(),
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(http_poster=post),
    )

    assert result.status == "fired"
    assert result.detail == "ntfy"
    assert len(posts) == 2
    assert posts[1][0].endswith("/topic")


def test_push_both_network_backends_fail_log_only_audits_and_emits_failed(monkeypatch) -> None:
    monkeypatch.setenv("KAI_PUSHOVER_USER", "u")
    monkeypatch.setenv("KAI_PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("KAI_NTFY_TOPIC", "topic")
    telemetry = []
    audit = []

    result = NotifyExecutor().execute(
        _push_action(backends=["pushover", "ntfy", "log_only"]),
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(
            http_poster=lambda _url, **_kwargs: Response(500),
            telemetry_emitter=lambda topic, payload: telemetry.append((topic, payload)),
            audit_writer=audit.append,
        ),
    )

    assert result.status == "failed"
    assert telemetry[0][0] == "auto.notify.push.failed"
    assert audit[0]["backend"] == "log_only"
    assert audit[1]["status"] == "failed"


def test_push_log_only_backend_writes_audit_and_reports_failure(monkeypatch) -> None:
    monkeypatch.delenv("KAI_PUSHOVER_USER", raising=False)
    audit = []

    result = NotifyExecutor().execute(
        _push_action(backends=["log_only"]),
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(audit_writer=audit.append),
    )

    assert result.status == "failed"
    assert result.detail == "all_push_backends_failed"
    assert audit[0]["backend"] == "log_only"


def test_push_env_only_credentials_rejected_at_validate_time() -> None:
    errors = NotifyExecutor().validate(_push_action(pushover={"user": "inline", "priority": 1}))

    assert any(error.field == "pushover.user" for error in errors)


def test_push_pushover_priority_range(monkeypatch) -> None:
    monkeypatch.setenv("KAI_PUSHOVER_USER", "u")
    monkeypatch.setenv("KAI_PUSHOVER_TOKEN", "t")
    monkeypatch.setenv("KAI_NTFY_TOPIC", "topic")

    assert not [
        error
        for error in NotifyExecutor().validate(_push_action(pushover={"priority": 2}))
        if error.field == "pushover.priority"
    ]
    errors = NotifyExecutor().validate(_push_action(pushover={"priority": 3}))

    assert any(error.field == "pushover.priority" for error in errors)

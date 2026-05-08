from __future__ import annotations

from daemon.event_injector import EventInjectionDecision
from daemon.signal_router import ActionDescriptor, ExecutionContext, RouterDedupTable
from daemon.signal_router.actions.inject_session import InjectSessionExecutor

from tests.test_action_helpers import FakeManaged, FakeSession


class FakeInjector:
    def __init__(self) -> None:
        self.requests = []

    def handle(self, managed, request):
        self.requests.append((managed, request))
        return EventInjectionDecision(True, "ok")


def _action(**params):
    merged = {"template_inline": "review {symbol}"}
    merged.update(params)
    return ActionDescriptor(kind="inject_session", target="analyst", params=merged)


def test_inject_session_builds_event_injection_request_with_router_source() -> None:
    injector = FakeInjector()
    managed = FakeManaged(FakeSession("analyst"))

    result = InjectSessionExecutor().execute(
        _action(rate_limit={"max_per_hour": 7}, require_auto_mode=False),
        {"subject": "signals.BTC", "payload": {"symbol": "BTC"}},
        ExecutionContext(
            channel="trade_signals",
            sessions={"analyst": managed},
            event_injector=injector,
            monotonic_seconds=lambda: 12.0,
        ),
    )

    assert result.status == "fired"
    request = injector.requests[0][1]
    assert request.policy.source == "signal_router:trade_signals"
    assert request.policy.max_injected_turns_per_hour == 7
    assert request.policy.require_auto_mode is False
    assert request.policy.single_auto_iteration is True
    assert request.render_values["symbol"] == "BTC"


def test_inject_session_dedup_suppresses_duplicate(tmp_path) -> None:
    injector = FakeInjector()
    managed = FakeManaged(FakeSession("analyst"))
    table = RouterDedupTable(tmp_path / "dedup.sqlite3")
    action = _action(dedup={"ttl_seconds": 60, "key_fields": ["channel", "symbol"]})
    context = ExecutionContext(
        channel="trade_signals",
        route_name="r1",
        sessions={"analyst": managed},
        event_injector=injector,
        dedup_table=table,
    )
    envelope = {"payload": {"symbol": "BTC"}}

    first = InjectSessionExecutor().execute(action, envelope, context)
    second = InjectSessionExecutor().execute(action, envelope, context)

    assert first.status == "fired"
    assert second.status == "skipped"
    assert second.detail == "dedup_suppressed"
    assert len(injector.requests) == 1
    table.close()


def test_inject_session_trader_requires_autotrade() -> None:
    action = ActionDescriptor(kind="inject_session", target="trader", params={"template_inline": "{symbol}"})

    result = InjectSessionExecutor().execute(
        action,
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(autotrade_enabled=lambda: False),
    )

    assert result.status == "skipped"
    assert result.detail == "autotrade_disabled"

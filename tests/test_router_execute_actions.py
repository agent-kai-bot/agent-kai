from __future__ import annotations

from datetime import datetime, timezone

from daemon.signal_router import (
    ActionDescriptor,
    ExecutionContext,
    MatchResult,
    RouteDecision,
    SignalRouter,
)
from daemon.signal_router.actions.base import ActionResult


class FakeExecutor:
    kind = "fake"

    def __init__(self) -> None:
        self.calls = []

    def validate(self, action):
        return []

    def execute(self, action, envelope, context):
        self.calls.append((action, envelope, context))
        return ActionResult(action.kind, action.target, "fired", None, {"dry_run": context.dry_run})


def _decision() -> RouteDecision:
    return RouteDecision(
        route_name="r1",
        channel="trade_signals",
        match_result=MatchResult(True, "matched"),
        actions=[ActionDescriptor(kind="fake", target="x", params={})],
        decided_at=datetime.now(timezone.utc),
        dedup_key=None,
        dedup_status=None,
    )


def test_execute_actions_legacy_invokes_zero_executors(tmp_path) -> None:
    router = SignalRouter(
        {"mode": "legacy", "dedup_table_path": str(tmp_path / "d.sqlite3")},
    )
    executor = FakeExecutor()
    router.action_executors = {"fake": executor}

    assert router.execute_actions(_decision(), {"payload": {}}) == []
    assert executor.calls == []


def test_execute_actions_shadow_forces_dry_run(tmp_path) -> None:
    router = SignalRouter(
        {"mode": "shadow", "dedup_table_path": str(tmp_path / "d.sqlite3")},
    )
    executor = FakeExecutor()
    router.action_executors = {"fake": executor}

    result = router.execute_actions(_decision(), {"payload": {}}, ExecutionContext(dry_run=False))

    assert result[0].metrics["dry_run"] is True
    assert executor.calls[0][2].dry_run is True


def test_execute_actions_new_invokes_real_executor(tmp_path) -> None:
    router = SignalRouter(
        {"mode": "new", "dedup_table_path": str(tmp_path / "d.sqlite3")},
    )
    executor = FakeExecutor()
    router.action_executors = {"fake": executor}

    result = router.execute_actions(_decision(), {"payload": {}}, ExecutionContext(dry_run=True))

    assert result[0].metrics["dry_run"] is False
    assert executor.calls[0][2].dry_run is False

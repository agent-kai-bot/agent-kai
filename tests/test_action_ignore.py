from __future__ import annotations

from daemon.signal_router import ActionDescriptor, ExecutionContext
from daemon.signal_router.actions.ignore import IgnoreExecutor


def test_ignore_records_reason_and_has_no_side_effect() -> None:
    audit = []

    result = IgnoreExecutor().execute(
        ActionDescriptor(kind="ignore", target=None, params={"reason": "noise"}),
        {"payload": {"symbol": "BTC"}},
        ExecutionContext(audit_writer=audit.append),
    )

    assert result.status == "skipped"
    assert result.detail == "noise"
    assert audit[0]["reason"] == "noise"

from __future__ import annotations

from datetime import datetime, timezone

from daemon.signal_router import ActionDescriptor, MatchResult, RouteDecision
from daemon.signal_router.route_decision import DEDUP_STATUS_VALUES


def test_route_decision_audit_payload_shape() -> None:
    decided_at = datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc)
    decision = RouteDecision(
        route_name="route-a",
        channel="trade_signals",
        match_result=MatchResult(
            matched=True,
            reason="matched configured fields",
            details={"symbol": "BTC"},
        ),
        actions=[
            ActionDescriptor(
                kind="inject_session",
                target="analyst",
                params={"template_inline": "Analyze {symbol}"},
            )
        ],
        decided_at=decided_at,
        dedup_key="route-a:BTC",
        dedup_status="fired",
    )

    assert decision.audit_payload() == {
        "route_name": "route-a",
        "channel": "trade_signals",
        "match_result": {
            "matched": True,
            "reason": "matched configured fields",
            "details": {"symbol": "BTC"},
        },
        "actions": [
            {
                "kind": "inject_session",
                "target": "analyst",
                "params": {"template_inline": "Analyze {symbol}"},
            }
        ],
        "decided_at": decided_at.isoformat(),
        "dedup_key": "route-a:BTC",
        "dedup_status": "fired",
    }


def test_dedup_status_values_round_trip_in_audit_payload() -> None:
    for status in DEDUP_STATUS_VALUES:
        decision = RouteDecision(
            route_name="route-a",
            channel="trade_signals",
            match_result=MatchResult(matched=True, reason="ok"),
            actions=[],
            decided_at=datetime(2026, 5, 8, tzinfo=timezone.utc),
            dedup_key=None,
            dedup_status=status,
        )

        assert decision.audit_payload()["dedup_status"] == status

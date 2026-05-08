from __future__ import annotations

from datetime import datetime, timedelta, timezone

from daemon.signal_router.diff_metrics import (
    DIVERGENCE_KINDS,
    DiffMetric,
    DiffMetricStore,
    DivergenceKind,
)


def test_all_divergence_kinds_round_trip() -> None:
    for kind in DIVERGENCE_KINDS:
        metric = DiffMetric(
            route_name="r1",
            legacy_decision="fired",
            router_decision="would_fire",
            divergence_kind=kind,
            details={"kind": kind.value},
        )

        restored = DiffMetric.from_dict(metric.to_dict())

        assert restored.divergence_kind == kind
        assert restored.details == {"kind": kind.value}


def test_rolling_window_aggregation_correct() -> None:
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    store = DiffMetricStore()
    store.record(
        DiffMetric(
            "r1",
            "fired",
            "would_fire",
            DivergenceKind.AGREED_FIRE,
            ts=now - timedelta(minutes=30),
        )
    )
    store.record(
        DiffMetric(
            "r1",
            "fired",
            "would_suppress",
            DivergenceKind.LEGACY_FIRED_ROUTER_SUPPRESSED,
            ts=now - timedelta(hours=2),
        )
    )
    store.record(
        DiffMetric(
            "r1",
            "fired",
            "would_suppress",
            DivergenceKind.CAP_SKEW,
            ts=now - timedelta(hours=25),
        )
    )

    snapshot = store.snapshot(now=now)

    assert snapshot["rolling_1h"]["agreed_fire"] == 1
    assert snapshot["rolling_1h"]["legacy_fired_router_suppressed"] == 0
    assert snapshot["rolling_24h"]["agreed_fire"] == 1
    assert snapshot["rolling_24h"]["legacy_fired_router_suppressed"] == 1
    assert snapshot["rolling_24h"]["cap_skew"] == 0


def test_by_route_aggregation_correct() -> None:
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    store = DiffMetricStore()
    store.record(DiffMetric("r1", "fired", "would_fire", DivergenceKind.AGREED_FIRE, ts=now))
    store.record(DiffMetric("r2", "fired", "would_fire", DivergenceKind.AGREED_FIRE, ts=now))
    store.record(DiffMetric("r2", "suppressed", "would_fire", DivergenceKind.LEGACY_SUPPRESSED_ROUTER_FIRED, ts=now))

    snapshot = store.snapshot(now=now)

    assert snapshot["by_route"]["r1"]["agreed_fire"] == 1
    assert snapshot["by_route"]["r1"]["legacy_suppressed_router_fired"] == 0
    assert snapshot["by_route"]["r2"]["agreed_fire"] == 1
    assert snapshot["by_route"]["r2"]["legacy_suppressed_router_fired"] == 1


def test_last_diff_sample_has_top_5_most_recent_divergent() -> None:
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    store = DiffMetricStore()
    store.record(DiffMetric("agree", "fired", "would_fire", DivergenceKind.AGREED_FIRE, ts=now))
    for index in range(7):
        store.record(
            DiffMetric(
                route_name=f"r{index}",
                legacy_decision="fired",
                router_decision="would_suppress",
                divergence_kind=DivergenceKind.MATCH_DIVERGED,
                ts=now + timedelta(seconds=index),
            )
        )

    sample = store.snapshot(now=now + timedelta(seconds=10))["last_diff_sample"]

    assert [item["route_name"] for item in sample] == ["r6", "r5", "r4", "r3", "r2"]

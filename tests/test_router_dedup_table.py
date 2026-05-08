from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from daemon.signal_router.dedup_table import RouterDedupTable


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def test_cooldown_ttl_fires_suppresses_then_fires_after_expiry(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc))
    table = RouterDedupTable(tmp_path / "dedup.sqlite3", now_fn=clock)

    assert table.check_and_record_cooldown("route:BTC", 60) is True
    assert table.check_and_record_cooldown("route:BTC", 60) is False

    clock.advance(timedelta(seconds=61))

    assert table.check_and_record_cooldown("route:BTC", 60) is True


def test_daily_cap_rolls_over_at_utc_midnight(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 5, 8, 23, 59, tzinfo=timezone.utc))
    table = RouterDedupTable(tmp_path / "dedup.sqlite3", now_fn=clock)

    assert table.check_daily_cap("route-a", 1) is True
    table.record_fire("route-a", None)
    assert table.check_daily_cap("route-a", 1) is False

    clock.advance(timedelta(minutes=2))

    assert table.check_daily_cap("route-a", 1) is True


def test_hourly_cap_rolls_over_at_top_of_hour(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 5, 8, 12, 59, tzinfo=timezone.utc))
    table = RouterDedupTable(tmp_path / "dedup.sqlite3", now_fn=clock)

    assert table.check_hourly_cap("route-a", 1) is True
    table.record_fire("route-a", None)
    assert table.check_hourly_cap("route-a", 1) is False

    clock.advance(timedelta(minutes=2))

    assert table.check_hourly_cap("route-a", 1) is True


def test_concurrent_check_and_record_allows_one_winner(tmp_path) -> None:
    db_path = tmp_path / "dedup.sqlite3"
    barrier = threading.Barrier(8)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker() -> None:
        table = RouterDedupTable(db_path)
        barrier.wait()
        result = table.check_and_record_cooldown("route:BTC", 3600)
        with results_lock:
            results.append(result)
        table.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 7


def test_restart_durability_preserves_rows(tmp_path) -> None:
    db_path = tmp_path / "dedup.sqlite3"
    table = RouterDedupTable(db_path)
    assert table.check_and_record_cooldown("route:BTC", 3600) is True
    table.record_fire("route-a", None)
    table.close()

    restarted = RouterDedupTable(db_path)
    try:
        assert restarted.check_and_record_cooldown("route:BTC", 3600) is False
        assert restarted.check_daily_cap("route-a", 2) is True
        assert restarted.check_daily_cap("route-a", 1) is False
    finally:
        restarted.close()


def test_purge_expired_removes_only_expired_rows(tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc))
    table = RouterDedupTable(tmp_path / "dedup.sqlite3", now_fn=clock)

    assert table.check_and_record_cooldown("expired-soon", 10) is True
    assert table.check_and_record_cooldown("still-live", 120) is True
    clock.advance(timedelta(seconds=11))

    assert table.purge_expired() == 1
    assert table.check_and_record_cooldown("still-live", 120) is False
    assert table.check_and_record_cooldown("expired-soon", 120) is True

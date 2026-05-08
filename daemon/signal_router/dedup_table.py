"""SQLite-backed cooldown and cap state for the daemon signal router."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

NAMESPACE_COOLDOWN = "cooldown"
NAMESPACE_DAILY_CAP = "daily_cap"
NAMESPACE_HOURLY_CAP = "hourly_cap"


def _default_agentkai_home() -> Path:
    return Path(os.getenv("AGENTKAI_HOME", Path.home() / ".agentkai")).expanduser()


def default_dedup_table_path() -> Path:
    return _default_agentkai_home() / "router_dedup.sqlite3"


def _resolve_db_path(db_path: str | Path | None) -> Path:
    if db_path is None:
        return default_dedup_table_path()
    raw_path = str(db_path)
    if "${AGENTKAI_HOME}" in raw_path and "AGENTKAI_HOME" not in os.environ:
        raw_path = raw_path.replace("${AGENTKAI_HOME}", str(_default_agentkai_home()))
    return Path(os.path.expandvars(raw_path)).expanduser()


class RouterDedupTable:
    """Durable router cooldown and route-cap table."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = _resolve_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dedup_keys (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    expires_at_unix INTEGER NOT NULL,
                    counter INTEGER DEFAULT 1,
                    last_fired_at_unix INTEGER NOT NULL,
                    PRIMARY KEY (namespace, key)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dedup_expires
                ON dedup_keys(expires_at_unix)
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _now(self) -> datetime:
        now = self._now_fn()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _now_unix(self) -> int:
        return int(self._now().timestamp())

    def check_and_record_cooldown(self, key: str, ttl_seconds: int) -> bool:
        """Return True and reserve the key unless an unexpired cooldown exists."""

        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        now_unix = self._now_unix()
        expires_at_unix = int((self._now() + timedelta(seconds=ttl_seconds)).timestamp())
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT expires_at_unix
                FROM dedup_keys
                WHERE namespace = ? AND key = ?
                """,
                (NAMESPACE_COOLDOWN, key),
            ).fetchone()
            if row is not None and int(row["expires_at_unix"]) > now_unix:
                return False
            conn.execute(
                """
                INSERT INTO dedup_keys (
                    namespace, key, expires_at_unix, counter, last_fired_at_unix
                )
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    expires_at_unix = excluded.expires_at_unix,
                    counter = 1,
                    last_fired_at_unix = excluded.last_fired_at_unix
                """,
                (NAMESPACE_COOLDOWN, key, expires_at_unix, now_unix),
            )
            return True

    def check_daily_cap(self, route_name: str, max_per_day: int) -> bool:
        """Return True when the route has fired fewer than max_per_day times today."""

        if max_per_day <= 0:
            return False
        return self._cap_count(NAMESPACE_DAILY_CAP, self._daily_key(route_name)) < max_per_day

    def check_hourly_cap(self, route_name: str, max_per_hour: int) -> bool:
        """Return True when the route has fired fewer than max_per_hour times this hour."""

        if max_per_hour <= 0:
            return False
        return self._cap_count(
            NAMESPACE_HOURLY_CAP,
            self._hourly_key(route_name),
        ) < max_per_hour

    def record_fire(self, route_name: str, dedup_key: str | None) -> None:
        """Increment hourly/daily counters and mark an optional cooldown key fired."""

        now = self._now()
        now_unix = int(now.timestamp())
        day_expires = int(
            (
                datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
                + timedelta(days=1)
            ).timestamp()
        )
        hour_expires = int(
            (
                datetime(now.year, now.month, now.day, now.hour, tzinfo=timezone.utc)
                + timedelta(hours=1)
            ).timestamp()
        )
        with self._transaction() as conn:
            self._increment_cap_locked(
                conn,
                namespace=NAMESPACE_DAILY_CAP,
                key=self._daily_key(route_name),
                expires_at_unix=day_expires,
                now_unix=now_unix,
            )
            self._increment_cap_locked(
                conn,
                namespace=NAMESPACE_HOURLY_CAP,
                key=self._hourly_key(route_name),
                expires_at_unix=hour_expires,
                now_unix=now_unix,
            )
            if dedup_key is not None:
                conn.execute(
                    """
                    INSERT INTO dedup_keys (
                        namespace, key, expires_at_unix, counter, last_fired_at_unix
                    )
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(namespace, key) DO UPDATE SET
                        counter = dedup_keys.counter + 1,
                        last_fired_at_unix = excluded.last_fired_at_unix
                    """,
                    (NAMESPACE_COOLDOWN, dedup_key, now_unix, now_unix),
                )

    def purge_expired(self) -> int:
        """Delete expired rows and return the number removed."""

        now_unix = self._now_unix()
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM dedup_keys WHERE expires_at_unix <= ?",
                (now_unix,),
            )
            return int(cursor.rowcount or 0)

    def count_keys(self) -> int:
        """Return the number of rows currently tracked."""

        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS count FROM dedup_keys").fetchone()
        return int(row["count"])

    def _cap_count(self, namespace: str, key: str) -> int:
        now_unix = self._now_unix()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT counter
                FROM dedup_keys
                WHERE namespace = ? AND key = ? AND expires_at_unix > ?
                """,
                (namespace, key, now_unix),
            ).fetchone()
        if row is None:
            return 0
        return int(row["counter"])

    def _increment_cap_locked(
        self,
        conn: sqlite3.Connection,
        *,
        namespace: str,
        key: str,
        expires_at_unix: int,
        now_unix: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO dedup_keys (
                namespace, key, expires_at_unix, counter, last_fired_at_unix
            )
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                expires_at_unix = excluded.expires_at_unix,
                counter = dedup_keys.counter + 1,
                last_fired_at_unix = excluded.last_fired_at_unix
            """,
            (namespace, key, expires_at_unix, now_unix),
        )

    def _daily_key(self, route_name: str) -> str:
        day = self._now().strftime("%Y-%m-%d")
        return f"daily:{route_name}:{day}"

    def _hourly_key(self, route_name: str) -> str:
        hour = self._now().strftime("%Y-%m-%dT%H")
        return f"hourly:{route_name}:{hour}"

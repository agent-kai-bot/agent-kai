"""Phase 0 follow-up (#10247): orphan-session sweep false-positives stop.

The remote agent_runs ledger gets PATCHed terminal by Phase 0 fix #3
(_finalize_dispatcher_inprocess_run). The local `sessions.status` used
to stay at 'running' forever, so the sweeper marked otherwise-finished
sessions stuck_aborted ~60min later and posted misleading [System]
audit comments. The fix closes the lifecycle on the local table too.

Tests
- mark_session_terminal updates the local row to a terminal status
  matching the supplied RunOutcome.status.
- Status mapping: covers succeeded, failed, timeout, cancelled, etc.
- mark_session_terminal does not touch rows that aren't 'running'
  (idempotent on retry).
- Sweeper's pending-stuck filter no longer matches finished rows.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.taskboard_dispatcher import _TaskboardQueueStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_session(db: Path, session_id: str, status: str = "running") -> None:
    """Insert a sessions row directly so the test doesn't need the spawn flow."""
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE,
            taskboard_task_id INTEGER,
            fire_generation INTEGER,
            agent_id TEXT,
            source TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            webhook_pending_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_progress_at TEXT,
            aborted_at TEXT
        )
        """
    )
    now = _utc_now().isoformat()
    conn.execute(
        "INSERT INTO sessions (session_id, taskboard_task_id, fire_generation, agent_id, source, status, created_at, updated_at, last_progress_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, 9999, 1, "developer", "taskboard_dispatcher", status, now, now, now),
    )
    conn.commit()
    conn.close()


def _read_status(db: Path, session_id: str) -> str | None:
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


class MarkSessionTerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "daemon-state.sqlite3"
        self.store = _TaskboardQueueStore(self.db, clock=_utc_now)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_succeeded_outcome_marks_local_completed(self) -> None:
        _seed_session(self.db, "sess-ok")
        self.store.mark_session_terminal(
            session_id="sess-ok", outcome_status="succeeded"
        )
        self.assertEqual(_read_status(self.db, "sess-ok"), "completed")

    def test_failed_outcome_marks_local_failed(self) -> None:
        _seed_session(self.db, "sess-bad")
        self.store.mark_session_terminal(
            session_id="sess-bad", outcome_status="failed"
        )
        self.assertEqual(_read_status(self.db, "sess-bad"), "failed")

    def test_endpoint_failed_maps_to_failed(self) -> None:
        _seed_session(self.db, "sess-ep")
        self.store.mark_session_terminal(
            session_id="sess-ep", outcome_status="endpoint_failed"
        )
        self.assertEqual(_read_status(self.db, "sess-ep"), "failed")

    def test_timeout_maps_to_failed(self) -> None:
        _seed_session(self.db, "sess-to")
        self.store.mark_session_terminal(
            session_id="sess-to", outcome_status="timeout"
        )
        self.assertEqual(_read_status(self.db, "sess-to"), "failed")

    def test_cancelled_maps_to_cancelled(self) -> None:
        _seed_session(self.db, "sess-cn")
        self.store.mark_session_terminal(
            session_id="sess-cn", outcome_status="cancelled"
        )
        self.assertEqual(_read_status(self.db, "sess-cn"), "cancelled")

    def test_requires_approval_maps_to_completed(self) -> None:
        # Approval-blocked is a terminal state where the agent did its
        # framing work and stopped at a tool boundary — local-side this
        # is "done with what it could do" → completed.
        _seed_session(self.db, "sess-ap")
        self.store.mark_session_terminal(
            session_id="sess-ap", outcome_status="requires_approval_blocked"
        )
        self.assertEqual(_read_status(self.db, "sess-ap"), "completed")

    def test_unknown_outcome_falls_back_to_completed(self) -> None:
        _seed_session(self.db, "sess-unknown")
        self.store.mark_session_terminal(
            session_id="sess-unknown", outcome_status="something_new"
        )
        # We default to 'completed' so the sweeper at least stops re-aborting.
        self.assertEqual(_read_status(self.db, "sess-unknown"), "completed")

    def test_idempotent_no_op_on_already_terminal_row(self) -> None:
        # Seed as 'completed' then call again with 'failed' — the WHERE
        # clause filters status='running' so a finalized row is not
        # touched (preserves the original terminal state).
        _seed_session(self.db, "sess-already-done", status="completed")
        self.store.mark_session_terminal(
            session_id="sess-already-done", outcome_status="failed"
        )
        self.assertEqual(_read_status(self.db, "sess-already-done"), "completed")

    def test_no_op_when_session_does_not_exist(self) -> None:
        # Nonexistent session_id: should not raise.
        self.store.mark_session_terminal(
            session_id="never-existed", outcome_status="succeeded"
        )

    def test_sweeper_no_longer_matches_after_finalize(self) -> None:
        # Seed a "stuck" session, finalize it, then ask the sweeper
        # for stuck rows older than 60s. Should be empty.
        _seed_session(self.db, "sess-not-stuck")
        # Backdate progress so the row is "no-progress old enough" for sweep.
        conn = sqlite3.connect(self.db)
        old = (_utc_now() - timedelta(hours=2)).isoformat()
        conn.execute(
            """
            UPDATE sessions
            SET created_at = ?, updated_at = ?, last_progress_at = ?
            WHERE session_id = ?
            """,
            (old, old, old, "sess-not-stuck"),
        )
        conn.commit()
        conn.close()

        before = self.store.stuck_sessions(older_than_seconds=60)
        self.assertEqual(len(before), 1, "row should be visible to sweeper before finalize")

        self.store.mark_session_terminal(
            session_id="sess-not-stuck", outcome_status="succeeded"
        )

        after = self.store.stuck_sessions(older_than_seconds=60)
        self.assertEqual(
            len(after),
            0,
            "finalized row must not match the stuck-sessions sweep filter",
        )


if __name__ == "__main__":
    unittest.main()

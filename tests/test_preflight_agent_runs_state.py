from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "preflight-agent-runs-state.sh"


class PreflightAgentRunsStateScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "agent-runs.sqlite3"
        self._init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                """
                CREATE TABLE agent_runs (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER,
                    role TEXT,
                    status TEXT NOT NULL,
                    session_id TEXT,
                    created_at TEXT,
                    started_at TEXT,
                    failure_class TEXT,
                    failure_detail TEXT,
                    finished_at TEXT
                )
                """
            )
            zombie_statuses = ["queued", "dispatching", "spawning", "running", "queued"]
            for idx, status in enumerate(zombie_statuses, start=1):
                conn.execute(
                    """
                    INSERT INTO agent_runs (
                        id, task_id, role, status, session_id, created_at, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idx,
                        9000 + idx,
                        "developer",
                        status,
                        f"agent:developer:task-{9000+idx}:run_{idx}",
                        "2026-01-01T00:00:00Z",
                        "2026-01-01T00:00:00Z",
                    ),
                )
            # Fresh row should remain active after cleanup.
            conn.execute(
                """
                INSERT INTO agent_runs (
                    id, task_id, role, status, session_id, created_at, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    99,
                    9999,
                    "developer",
                    "running",
                    "agent:developer:task-9999:run_99",
                    "2099-01-01T00:00:00Z",
                    "2099-01-01T00:00:00Z",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), "--sqlite-db", str(self.db), "--stuck-after-seconds", "60", *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_lists_zombies_and_plan(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 3)
        self.assertIn("stale_rows_total=5", result.stdout)
        self.assertIn("unknown_age_rows_total=0", result.stdout)
        self.assertIn("SQL UPDATE agent_runs", result.stdout)
        self.assertIn("gate=blocked", result.stdout)

    def test_apply_marks_zombies_terminal_and_capacity_drops_to_one_live_row(self) -> None:
        result = self._run("--apply")
        self.assertEqual(result.returncode, 4)
        self.assertIn("patched_rows=5", result.stdout)
        self.assertIn("capacity_after=1", result.stdout)
        self.assertIn("gate=blocked", result.stdout)

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, status, failure_class, failure_detail, finished_at FROM agent_runs ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

        terminal_ids = {1, 2, 3, 4, 5}
        for row in rows:
            if row["id"] in terminal_ids:
                self.assertEqual(row["status"], "stuck_aborted")
                self.assertEqual(row["failure_class"], "session_stuck_no_progress")
                self.assertIn("preflight cleanup", row["failure_detail"])
                self.assertTrue(row["finished_at"])
            elif row["id"] == 99:
                self.assertEqual(row["status"], "running")

    def test_apply_clears_gate_and_returns_zero_capacity_when_all_active_rows_are_zombies(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM agent_runs WHERE id = 99")
            conn.commit()
        finally:
            conn.close()

        result = self._run("--apply")
        self.assertEqual(result.returncode, 0)
        self.assertIn("patched_rows=5", result.stdout)
        self.assertIn("capacity_after=0", result.stdout)
        self.assertIn("gate=clear", result.stdout)


if __name__ == "__main__":
    unittest.main()

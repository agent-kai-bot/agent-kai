"""Phase 0 follow-up (#10271): session-token mint must use proper-case role.

Regression test for the reviewer-409 bug. Before the fix, the dispatcher
minted session tokens with `agent='code-reviewer'` (kebab-case agent_id).
The taskboard's `REVIEWER_AGENT_TO_TYPE` keys are proper-case
('Code Reviewer' / 'Security Auditor' / 'QA Agent'), so
`validate_task_status` rejected reviewer writebacks with
`409 Task not in active status`. Fix: pass `route.role` instead of
`route.agent_id` to the mint endpoint.

These tests freeze the proper-case contract so a future refactor can't
silently regress it.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.taskboard_dispatcher import TaskboardDispatcher, resolve_taskboard_role


class _DisabledAgentRunsClient:
    enabled = False

    def list_by_status(self, status: str, limit: int = 200):
        raise AssertionError(f"should not query agent_runs ledger for {status}")


class _CaptureSpawner:
    def __init__(self) -> None:
        self.spawn_calls: list[dict] = []

    async def spawn(self, **kwargs):
        self.spawn_calls.append(kwargs)
        return kwargs["session_id"]

    async def abort(self, session_id: str) -> None:
        raise AssertionError(f"unexpected abort: {session_id}")


class _FakeTaskClient:
    def __init__(self, tasks: dict[int, dict]) -> None:
        self.tasks = tasks
        self.comments: list[tuple[int, str]] = []

    async def fetch_task(self, task_id: int) -> dict:
        return dict(self.tasks[task_id])

    async def post_audit_comment(self, task_id: int, content: str) -> None:
        self.comments.append((task_id, content))


def _create_pending_table(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            dispatch_status TEXT,
            session_id TEXT,
            processed_at TEXT,
            audit_posted_at TIMESTAMPTZ,
            last_error TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _seed_review_row(db: Path, task_id: int, fire_generation: int) -> int:
    conn = sqlite3.connect(db)
    payload = (
        f'{{"event_id": "ev-{task_id}-{fire_generation}", "task_id": {task_id}, '
        f'"fire_generation": {fire_generation}, '
        f'"from_status": "In Progress", "to_status": "Review"}}'
    )
    cur = conn.execute(
        "INSERT INTO webhook_pending(received_at, payload, dispatch_status) VALUES (?, ?, ?)",
        ("2026-05-02T13:00:00Z", payload, "accepted"),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


class SessionTokenRoleCasingTests(unittest.IsolatedAsyncioTestCase):
    """Confirms the dispatcher passes proper-case role to the mint endpoint."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "daemon-state.sqlite3"
        _create_pending_table(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_review_fanout_mints_proper_case_for_each_reviewer(self) -> None:
        _seed_review_row(self.db, task_id=901, fire_generation=2)
        task = {"id": 901, "agent": "Developer", "fire_generation": 2}
        spawner = _CaptureSpawner()
        dispatcher = TaskboardDispatcher(
            db_path=self.db,
            task_client=_FakeTaskClient({901: task}),
            session_manager=spawner,
            nats_bus=None,
            agent_runs_client=_DisabledAgentRunsClient(),
        )

        mint_calls: list[tuple[int, str]] = []

        def _capture_mint(self_, *, task_id: int, role: str) -> tuple[str, int]:
            mint_calls.append((task_id, role))
            return f"tok-{role.replace(' ', '-').lower()}", 1

        with mock.patch.object(
            TaskboardDispatcher,
            "_mint_taskboard_session_token",
            new=_capture_mint,
        ), mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered prompt",
        ):
            await dispatcher.run_once()

        # Three reviewer mints, one per role, all PROPER-CASE not kebab.
        self.assertEqual(len(mint_calls), 3)
        roles_passed = {role for _, role in mint_calls}
        self.assertEqual(
            roles_passed,
            {"Code Reviewer", "Security Auditor", "QA Agent"},
            "Mint must receive proper-case role names that match "
            "taskboard's REVIEWER_AGENT_TO_TYPE keys, NOT kebab-case",
        )
        # Sanity: never see kebab-case in the mint call list.
        for _, role in mint_calls:
            self.assertNotIn("-", role, f"kebab-case leaked into mint: {role}")

    async def test_developer_spawn_mints_proper_case_developer(self) -> None:
        # Insert a Backlog→In Progress webhook row for a Developer task.
        conn = sqlite3.connect(self.db)
        payload = (
            '{"event_id": "ev-902-1", "task_id": 902, "fire_generation": 1, '
            '"from_status": "Backlog", "to_status": "In Progress"}'
        )
        conn.execute(
            "INSERT INTO webhook_pending(received_at, payload, dispatch_status) VALUES (?, ?, ?)",
            ("2026-05-02T13:00:00Z", payload, "accepted"),
        )
        conn.commit()
        conn.close()

        task = {"id": 902, "agent": "Developer", "fire_generation": 1}
        spawner = _CaptureSpawner()
        dispatcher = TaskboardDispatcher(
            db_path=self.db,
            task_client=_FakeTaskClient({902: task}),
            session_manager=spawner,
            nats_bus=None,
            agent_runs_client=_DisabledAgentRunsClient(),
        )

        mint_calls: list[tuple[int, str]] = []

        def _capture_mint(self_, *, task_id: int, role: str) -> tuple[str, int]:
            mint_calls.append((task_id, role))
            return f"tok-{role}", 1

        with mock.patch.object(
            TaskboardDispatcher,
            "_mint_taskboard_session_token",
            new=_capture_mint,
        ), mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered prompt",
        ):
            await dispatcher.run_once()

        self.assertEqual(len(mint_calls), 1)
        self.assertEqual(
            mint_calls[0][1],
            "Developer",
            "Developer mint must use proper-case 'Developer'",
        )

    def test_resolve_taskboard_role_returns_proper_case_role(self) -> None:
        # Sanity: route.role is proper-case for every reviewer + dev.
        for input_label in ["code reviewer", "Code Reviewer", "code-reviewer"]:
            self.assertEqual(resolve_taskboard_role(input_label).role, "Code Reviewer")
        for input_label in ["security auditor", "Security Auditor", "security-auditor"]:
            self.assertEqual(resolve_taskboard_role(input_label).role, "Security Auditor")
        for input_label in ["qa agent", "QA Agent", "qa-agent"]:
            self.assertEqual(resolve_taskboard_role(input_label).role, "QA Agent")
        for input_label in ["developer", "Developer"]:
            self.assertEqual(resolve_taskboard_role(input_label).role, "Developer")


if __name__ == "__main__":
    unittest.main()

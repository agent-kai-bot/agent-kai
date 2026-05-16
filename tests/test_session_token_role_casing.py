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

from agent.runtime_config_resolver import RoleRuntimeConfig
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
        self.moves: list[tuple[int, str, str, str]] = []

    async def fetch_task(self, task_id: int) -> dict:
        return dict(self.tasks[task_id])

    async def post_audit_comment(self, task_id: int, content: str) -> None:
        self.comments.append((task_id, content))

    async def move_task_status(
        self,
        task_id: int,
        status: str,
        *,
        reason: str = "",
        agent: str = "Orchestrator",
    ) -> None:
        self.moves.append((task_id, status, reason, agent))
        self.tasks[task_id] = dict(self.tasks[task_id])
        self.tasks[task_id]["status"] = status


class _GateRuntimeConfigResolver:
    def resolve_for_role(self, role: str, **_kwargs) -> RoleRuntimeConfig:
        return RoleRuntimeConfig(
            role=role,
            forgejo_pat=f"forgejo-pat-for-{role}",
            taskboard_base_url="http://taskboard.local",
            taskboard_bearer_token=f"gate-taskboard-token-for-{role}",
            taskboard_mint_bearer_token="admin-taskboard-token",
            source="test",
        )


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

    async def test_legacy_review_status_mints_proper_case_for_code_reviewer(self) -> None:
        """SPEC v23: legacy ``Review`` fires Code Reviewer alone (single mint).

        Pre-#10261 this fired CR + SA + QA in parallel and raced the per-task
        session_token generation (Router v2 #10276). With single-role fanout
        only one mint happens per status transition, so the race is impossible.
        The proper-case assertion still applies: mint must receive
        ``"Code Reviewer"`` (not ``"code-reviewer"`` kebab-case).
        """

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

        self.assertEqual(len(mint_calls), 1)
        self.assertEqual(
            mint_calls[0],
            (901, "Code Reviewer"),
            "Mint must receive proper-case ``Code Reviewer`` matching the "
            "taskboard's REVIEWER_AGENT_TO_TYPE key, NOT kebab-case",
        )
        for _, role in mint_calls:
            self.assertNotIn("-", role, f"kebab-case leaked into mint: {role}")

    async def test_gate_reviewer_runtime_uses_gate_bearer_but_mint_uses_admin_bearer(self) -> None:
        _seed_review_row(self.db, task_id=903, fire_generation=4)
        task = {"id": 903, "agent": "Developer", "fire_generation": 4}
        spawner = _CaptureSpawner()
        dispatcher = TaskboardDispatcher(
            db_path=self.db,
            task_client=_FakeTaskClient({903: task}),
            session_manager=spawner,
            nats_bus=None,
            agent_runs_client=_DisabledAgentRunsClient(),
            runtime_config_resolver=_GateRuntimeConfigResolver(),
        )

        mint_calls: list[dict] = []

        def _capture_mint(
            self_,
            *,
            task_id: int,
            role: str,
            base_url: str | None = None,
            bearer_token: str | None = None,
        ) -> tuple[str, int]:
            mint_calls.append(
                {
                    "task_id": task_id,
                    "role": role,
                    "base_url": base_url,
                    "bearer_token": bearer_token,
                }
            )
            return "session-token", 4

        with mock.patch.object(
            TaskboardDispatcher,
            "_mint_taskboard_session_token",
            new=_capture_mint,
        ), mock.patch(
            "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
            return_value="rendered prompt",
        ):
            await dispatcher.run_once()

        self.assertEqual(
            mint_calls,
            [
                {
                    "task_id": 903,
                    "role": "Code Reviewer",
                    "base_url": "http://taskboard.local",
                    "bearer_token": "admin-taskboard-token",
                }
            ],
        )
        self.assertEqual(len(spawner.spawn_calls), 1)
        spawn = spawner.spawn_calls[0]
        self.assertEqual(
            spawn["taskboard_bearer_token"],
            "gate-taskboard-token-for-code-reviewer",
        )
        self.assertEqual(
            spawn["runtime_env"]["TASKBOARD_BEARER_TOKEN"],
            "gate-taskboard-token-for-code-reviewer",
        )
        self.assertEqual(
            spawn["runtime_config"].taskboard_mint_bearer_token,
            "admin-taskboard-token",
        )

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

        task = {
            "id": 902,
            "agent": "Developer",
            "fire_generation": 1,
            "project": {"repoUrl": "https://forgejo.example/alpha-tech-org/example.git"},
        }
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

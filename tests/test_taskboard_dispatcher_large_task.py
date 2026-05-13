"""Large task regression tests for the taskboard dispatcher boundary."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agent.taskboard_dispatcher import TaskboardDispatcher


NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeSessionManager:
    def __init__(self) -> None:
        self.spawn_calls: list[dict] = []

    async def spawn(self, **kwargs):
        self.spawn_calls.append(kwargs)
        return kwargs["session_id"]

    async def abort(self, session_id: str) -> None:
        raise AssertionError(f"unexpected abort: {session_id}")


class _DisabledAgentRunsClient:
    enabled = False
    base_url = ""


async def test_default_client_fetches_full_large_task_before_spawn(tmp_path: Path) -> None:
    """Dispatcher default client fetches >20K task JSON without truncation."""

    db_path = tmp_path / "daemon.sqlite3"
    _create_pending_table(db_path)
    large_comment = "dispatcher-large-comment-" + ("x" * 45_000)
    task_id = 10413
    fire_generation = 12
    _insert_pending(db_path, task_id, fire_generation)
    latest_task = {
        "id": task_id,
        "title": "Large dispatcher task",
        "description": "Needs full comment context.",
        "status": "In Progress",
        "agent": "Developer",
        "agent_id": "developer",
        "fire_generation": fire_generation,
        "comments": [{"id": 1, "content": large_comment}],
    }
    session_manager = _FakeSessionManager()

    def request_side_effect(**kwargs):
        method = kwargs["method"]
        url = kwargs["url"]
        if method == "GET" and url == "http://taskboard.local/api/tasks/10413":
            return _FakeResponse(payload=latest_task)
        if method == "POST" and url == "http://taskboard.local/api/tasks/10413/comments":
            return _FakeResponse(status_code=201, payload={"id": 99})
        raise AssertionError(f"unexpected request: {method} {url}")

    def render_side_effect(role, task, **_):
        return f"prompt for {role}: {task['comments'][0]['content']}"

    env = {
        "TASKBOARD_URL": "http://taskboard.local",
        "TASKBOARD_BEARER_TOKEN": "",
        "OPENCLAW_GATEWAY_TOKEN": "",
        "OPENCLAW_TOKEN": "",
    }
    with mock.patch.dict(os.environ, env), mock.patch(
        "agent.taskboard_service_client.requests.request",
        side_effect=request_side_effect,
    ) as request_mock, mock.patch(
        "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
        side_effect=render_side_effect,
    ):
        dispatcher = TaskboardDispatcher(
            db_path=db_path,
            session_manager=session_manager,
            max_concurrent_spawns=6,
            clock=lambda: NOW,
            agent_runs_client=_DisabledAgentRunsClient(),
        )
        counts = await dispatcher.run_once()

    assert counts == {"spawned": 1}
    assert _pending_row(db_path, 1)["dispatch_status"] == "spawned"
    assert len(session_manager.spawn_calls) == 1
    spawn = session_manager.spawn_calls[0]
    assert spawn["task"]["comments"][0]["content"] == large_comment
    assert large_comment in spawn["prompt"]
    assert request_mock.call_count == 2


def _create_pending_table(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE webhook_pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                processed_at TEXT,
                dispatch_status TEXT,
                session_id TEXT,
                last_error TEXT,
                audit_posted_at TEXT
            )
            """
        )


def _insert_pending(db_path: Path, task_id: int, fire_generation: int) -> int:
    payload = {
        "event_id": f"event-{task_id}-{fire_generation}",
        "event_type": "task.status_changed",
        "occurred_at": _iso(NOW),
        "task_id": task_id,
        "fire_generation": fire_generation,
        "from_status": "Backlog",
        "to_status": "In Progress",
        "task": {
            "id": task_id,
            "agent": "Developer",
            "fire_generation": fire_generation,
            "status": "In Progress",
        },
    }
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO webhook_pending(received_at, payload) VALUES (?, ?)",
            (_iso(NOW), json.dumps(payload)),
        )
        return int(cursor.lastrowid)


def _pending_row(db_path: Path, row_id: int) -> sqlite3.Row:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM webhook_pending WHERE id = ?",
            (row_id,),
        ).fetchone()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )

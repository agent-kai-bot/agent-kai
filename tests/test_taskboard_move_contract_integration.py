"""Integration tests against the live taskboard FastAPI move contract."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest import mock
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agent.runtime_config_resolver import RoleRuntimeConfig
from agent.taskboard_dispatcher import SELF_MOVE_REASON
from agent.taskboard_dispatcher import TaskboardDispatcher
from agent.taskboard_service_client import TaskboardServiceClient


TASKBOARD_ROOT = Path("/home/atc/git/OPS/openclawdev-taskboard")
TASKBOARD_APP = TASKBOARD_ROOT / "app.py"
BOOTSTRAP_TOKEN = "bootstrap-v2-token-123456"
NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_REPO_URL = "https://forgejo.example/alpha-tech-org/example.git"


def test_move_task_status_user_actor_succeeds_against_taskboard_contract(
    tmp_path,
    monkeypatch,
) -> None:
    """KAI's pre-spawn self-move uses the live /move contract, not a fake."""

    taskboard_app = _load_taskboard_app(monkeypatch)
    with _isolated_taskboard_db(taskboard_app, tmp_path / "taskboard.db"):
        with patch.object(taskboard_app.manager, "broadcast", new=AsyncMock()):
            with TestClient(
                taskboard_app.app,
                base_url="http://taskboard.test",
            ) as client:
                task_id = _seed_review_task(taskboard_app, client)
                service = TaskboardServiceClient(
                    "http://taskboard.test",
                    bearer_token=BOOTSTRAP_TOKEN,
                    request_func=_testclient_request(client),
                )

                moved = service.move_task_status(
                    task_id,
                    "Fixing",
                    reason=SELF_MOVE_REASON,
                    agent="User",
                )

    assert moved["status"] == "moved"
    assert moved["new_status"] == "Fixing"
    assert moved["requested_status"] == "Fixing"


def test_request_changes_move_only_spawns_from_real_taskboard_status_webhook(
    tmp_path,
    monkeypatch,
) -> None:
    """Exercise KAI move-only dispatch through taskboard's real outbox payload."""

    taskboard_app = _load_taskboard_app(monkeypatch)
    monkeypatch.setenv(
        taskboard_app.webhook_outbox.ENV_TASKBOARD_WEBHOOK_SECRET,
        "test-secret-value",
    )
    with _isolated_taskboard_db(taskboard_app, tmp_path / "taskboard.db"):
        with patch.object(taskboard_app.manager, "broadcast", new=AsyncMock()):
            with TestClient(
                taskboard_app.app,
                base_url="http://taskboard.test",
            ) as client:
                _seed_status_subscription(taskboard_app)
                task_id = _seed_review_task(
                    taskboard_app,
                    client,
                    agent="Developer",
                    task_type="Feature",
                    implementation_agent="Developer",
                )
                service = TaskboardServiceClient(
                    "http://taskboard.test",
                    bearer_token=BOOTSTRAP_TOKEN,
                    request_func=_testclient_request(client),
                )
                repo_service = _RepoAwareTaskboardServiceClient(service)
                kai_db = tmp_path / "kai-daemon-state.sqlite3"
                _create_kai_pending_table(kai_db)
                _insert_kai_pending(
                    kai_db,
                    {
                        "event_id": "verdict-request-changes-real-taskboard",
                        "event_type": "review.verdict_submitted",
                        "task_id": task_id,
                        "fire_generation": 1,
                        "gate_type": "code",
                        "verdict": "REQUEST_CHANGES",
                        "task": {
                            "id": task_id,
                            "agent": "Code Reviewer",
                            "implementation_agent": "Developer",
                            "status": "Review",
                            "fire_generation": 1,
                        },
                    },
                )
                first_spawner = _FakeSessionManager()
                first_dispatcher = _dispatcher(
                    kai_db,
                    repo_service,
                    first_spawner,
                )

                first_counts = asyncio.run(first_dispatcher.run_once())

                assert first_counts == {"move_only": 1}
                assert first_spawner.spawn_calls == []

                payload = _single_status_outbox_payload(taskboard_app, task_id)
                assert payload["event_type"] == "task.status_changed"
                assert payload["from_status"] == "Review"
                assert payload["to_status"] == "Fixing"
                _insert_kai_pending(kai_db, payload)

                second_spawner = _FakeSessionManager()
                second_dispatcher = _dispatcher(
                    kai_db,
                    repo_service,
                    second_spawner,
                )
                with mock.patch(
                    "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
                    side_effect=lambda role, task, **_: f"prompt for {role} #{task['id']}",
                ):
                    second_counts = asyncio.run(second_dispatcher.run_once())

                assert second_counts == {"spawned": 1}
                assert len(second_spawner.spawn_calls) == 1
                assert second_spawner.spawn_calls[0]["role"] == "Developer"
                assert second_spawner.spawn_calls[0]["task"]["status"] == "Fixing"
                assert _kai_pending_statuses(kai_db) == ["move_only", "spawned"]


def test_project_id_only_status_webhook_fetches_real_taskboard_project_before_spawn(
    tmp_path,
    monkeypatch,
) -> None:
    """Real taskboard payloads with project=None resolve repo routing via GET project."""

    taskboard_app = _load_taskboard_app(monkeypatch)
    monkeypatch.setenv(
        taskboard_app.webhook_outbox.ENV_TASKBOARD_WEBHOOK_SECRET,
        "test-secret-value",
    )
    with _isolated_taskboard_db(taskboard_app, tmp_path / "taskboard.db"):
        with patch.object(taskboard_app.manager, "broadcast", new=AsyncMock()):
            with TestClient(
                taskboard_app.app,
                base_url="http://taskboard.test",
            ) as client:
                _seed_status_subscription(taskboard_app)
                task_id = _seed_review_task(
                    taskboard_app,
                    client,
                    agent="Developer",
                    task_type="Feature",
                    implementation_agent="Developer",
                )
                project_id = _set_project_git_url_for_task(
                    taskboard_app,
                    task_id,
                    DEFAULT_REPO_URL,
                )
                service = TaskboardServiceClient(
                    "http://taskboard.test",
                    bearer_token=BOOTSTRAP_TOKEN,
                    request_func=_testclient_request(client),
                )

                moved = service.move_task_status(
                    task_id,
                    "Fixing",
                    reason=SELF_MOVE_REASON,
                    agent="User",
                )
                assert moved["status"] == "moved"

                payload = _single_status_outbox_payload(taskboard_app, task_id)
                assert payload["event_type"] == "task.status_changed"
                assert payload["task"]["project_id"] == project_id
                assert "repo_url" not in payload["task"]
                assert "git_url" not in payload["task"]
                payload["task"]["project"] = None

                kai_db = tmp_path / "kai-daemon-state.sqlite3"
                _create_kai_pending_table(kai_db)
                _insert_kai_pending(kai_db, payload)
                spawner = _FakeSessionManager()
                dispatcher = _dispatcher(kai_db, service, spawner)
                with mock.patch(
                    "agent.taskboard_dispatcher.render_taskboard_fire_prompt",
                    side_effect=lambda role, task, **_: f"prompt for {role} #{task['id']}",
                ):
                    counts = asyncio.run(dispatcher.run_once())

                assert counts == {"spawned": 1}
                assert len(spawner.spawn_calls) == 1
                spawn_task = spawner.spawn_calls[0]["task"]
                assert spawn_task["project_id"] == project_id
                assert spawn_task["project"]["git_url"] == DEFAULT_REPO_URL
                assert spawner.spawn_calls[0]["role"] == "Developer"


def _load_taskboard_app(monkeypatch):
    monkeypatch.setenv("TASKBOARD_WEBHOOK_WORKER_DISABLED", "1")
    existing = sys.modules.get("app")
    if existing is not None:
        existing_path = Path(getattr(existing, "__file__", "") or "")
        if existing_path and existing_path.resolve() != TASKBOARD_APP:
            del sys.modules["app"]
    if str(TASKBOARD_ROOT) not in sys.path:
        monkeypatch.syspath_prepend(str(TASKBOARD_ROOT))
    return importlib.import_module("app")


@contextmanager
def _isolated_taskboard_db(taskboard_app: Any, db_path: Path) -> Iterator[None]:
    original = (
        taskboard_app.DB_PATH,
        taskboard_app._DB_POOL,
        taskboard_app._DB_POOL_PATH,
        taskboard_app.TASKBOARD_BEARER_TOKEN,
        taskboard_app.TASKBOARD_BROWSER_TOKEN,
        taskboard_app.TASKBOARD_AUTH_GRACE_MODE,
        taskboard_app.TASKBOARD_LOOPBACK_AUTH_BYPASS,
        taskboard_app.TASKBOARD_API_KEY,
    )
    taskboard_app.DB_PATH = db_path
    taskboard_app._DB_POOL = None
    taskboard_app._DB_POOL_PATH = None
    taskboard_app.TASKBOARD_BEARER_TOKEN = BOOTSTRAP_TOKEN
    taskboard_app.TASKBOARD_BROWSER_TOKEN = ""
    taskboard_app.TASKBOARD_AUTH_GRACE_MODE = False
    taskboard_app.TASKBOARD_LOOPBACK_AUTH_BYPASS = False
    taskboard_app.TASKBOARD_API_KEY = ""
    taskboard_app.init_db()
    try:
        yield
    finally:
        (
            taskboard_app.DB_PATH,
            taskboard_app._DB_POOL,
            taskboard_app._DB_POOL_PATH,
            taskboard_app.TASKBOARD_BEARER_TOKEN,
            taskboard_app.TASKBOARD_BROWSER_TOKEN,
            taskboard_app.TASKBOARD_AUTH_GRACE_MODE,
            taskboard_app.TASKBOARD_LOOPBACK_AUTH_BYPASS,
            taskboard_app.TASKBOARD_API_KEY,
        ) = original
        taskboard_app._DB_POOL = None
        taskboard_app._DB_POOL_PATH = None


def _seed_review_task(
    taskboard_app: Any,
    client: TestClient,
    *,
    agent: str = "Developer",
    task_type: str = "Feature",
    implementation_agent: str = "Developer",
) -> int:
    response = client.post(
        "/api/tasks",
        headers=_auth(),
        json={
            "title": "KAI move contract regression",
            "description": "Review to Fixing self-move contract",
            "priority": "High",
            "agent": agent,
            "task_type": task_type,
        },
    )
    assert response.status_code == 200, response.text
    task_id = int(response.json()["id"])
    with taskboard_app.get_db() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'Review',
                agent = ?,
                implementation_agent = ?,
                fire_generation = 0
            WHERE id = ?
            """,
            (agent, implementation_agent, task_id),
        )
        conn.execute("DELETE FROM webhook_outbox")
        conn.commit()
    return task_id


def _seed_status_subscription(taskboard_app: Any) -> None:
    with taskboard_app.get_db() as conn:
        taskboard_app.webhook_outbox.insert_subscription(
            conn,
            name="kai",
            target_url="http://kai.test/api/webhooks/taskboard",
            event_types=["task.status_changed"],
            secret_vault_path="kai/taskboard-webhook-secret",
            active=True,
        )
        conn.commit()


def _set_project_git_url_for_task(
    taskboard_app: Any,
    task_id: int,
    git_url: str,
) -> int:
    with taskboard_app.get_db() as conn:
        task_row = conn.execute(
            "SELECT project_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        assert task_row is not None
        project_id = int(task_row["project_id"])
        project_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        updates = ["git_url = ?", "updated_at = ?"]
        params: list[Any] = [git_url, _iso(NOW)]
        if "git_branch" in project_columns:
            updates.append("git_branch = ?")
            params.append("main")
        if "default_branch" in project_columns:
            updates.append("default_branch = ?")
            params.append("main")
        params.append(project_id)
        conn.execute(
            f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
    return project_id


def _single_status_outbox_payload(taskboard_app: Any, task_id: int) -> dict[str, Any]:
    with taskboard_app.get_db() as conn:
        rows = conn.execute(
            """
            SELECT payload_json
            FROM webhook_outbox
            WHERE event_type = 'task.status_changed'
            ORDER BY id
            """,
        ).fetchall()
    payloads = [json.loads(row["payload_json"]) for row in rows]
    matching = [
        payload
        for payload in payloads
        if isinstance(payload, dict) and int(payload.get("task_id") or 0) == task_id
    ]
    assert len(matching) == 1
    return matching[0]


def _create_kai_pending_table(db_path: Path) -> None:
    with _connect_kai(db_path) as conn:
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


def _insert_kai_pending(db_path: Path, payload: dict[str, Any]) -> None:
    with _connect_kai(db_path) as conn:
        conn.execute(
            "INSERT INTO webhook_pending(received_at, payload) VALUES (?, ?)",
            (_iso(NOW), json.dumps(payload)),
        )


def _kai_pending_statuses(db_path: Path) -> list[str]:
    with _connect_kai(db_path) as conn:
        rows = conn.execute(
            "SELECT dispatch_status FROM webhook_pending ORDER BY id"
        ).fetchall()
    return [str(row["dispatch_status"]) for row in rows]


def _connect_kai(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _dispatcher(
    db_path: Path,
    task_client: TaskboardServiceClient,
    session_manager: "_FakeSessionManager",
) -> TaskboardDispatcher:
    return TaskboardDispatcher(
        db_path=db_path,
        task_client=task_client,
        session_manager=session_manager,
        clock=lambda: NOW,
        agent_runs_client=mock.Mock(enabled=False),
        runtime_config_resolver=_FakeRuntimeConfigResolver(),
    )


class _FakeSessionManager:
    def __init__(self) -> None:
        self.spawn_calls: list[dict[str, Any]] = []

    async def spawn(self, **kwargs):
        self.spawn_calls.append(kwargs)
        return kwargs["session_id"]

    async def abort(self, session_id: str) -> None:
        raise AssertionError(f"unexpected abort: {session_id}")


class _RepoAwareTaskboardServiceClient:
    def __init__(self, inner: TaskboardServiceClient) -> None:
        self.inner = inner

    def fetch_task(self, task_id: int) -> dict[str, Any]:
        task = self.inner.fetch_task(task_id)
        task["repo_url"] = DEFAULT_REPO_URL
        task["default_branch"] = "main"
        return task

    def post_audit_comment(self, task_id: int, content: str) -> dict[str, Any]:
        return self.inner.post_audit_comment(task_id, content)

    def move_task_status(
        self,
        task_id: int,
        status: str,
        *,
        reason: str = "",
        agent: str = "Orchestrator",
    ) -> dict[str, Any]:
        return self.inner.move_task_status(
            task_id,
            status,
            reason=reason,
            agent=agent,
        )


class _FakeRuntimeConfigResolver:
    def resolve_for_role(self, role: str, **_kwargs) -> RoleRuntimeConfig:
        return RoleRuntimeConfig(
            role=role,
            forgejo_pat=f"pat-for-{role}",
            forgejo_user=f"user-for-{role}",
            forgejo_base_url="http://forgejo.local",
            taskboard_base_url="",
            taskboard_bearer_token="",
            source="test",
        )


def _testclient_request(client: TestClient):
    def _request(**kwargs):
        kwargs.pop("timeout", None)
        return client.request(**kwargs)

    return _request


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )

"""Integration tests against the live taskboard FastAPI move contract."""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agent.taskboard_dispatcher import SELF_MOVE_REASON
from agent.taskboard_service_client import TaskboardServiceClient


TASKBOARD_ROOT = Path("/home/atc/git/OPS/openclawdev-taskboard")
TASKBOARD_APP = TASKBOARD_ROOT / "app.py"
BOOTSTRAP_TOKEN = "bootstrap-v2-token-123456"


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


def _seed_review_task(taskboard_app: Any, client: TestClient) -> int:
    response = client.post(
        "/api/tasks",
        headers=_auth(),
        json={
            "title": "KAI move contract regression",
            "description": "Review to Fixing self-move contract",
            "priority": "High",
            "agent": "Developer",
            "task_type": "Feature",
        },
    )
    assert response.status_code == 200, response.text
    task_id = int(response.json()["id"])
    with taskboard_app.get_db() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'Review',
                agent = 'Developer',
                implementation_agent = 'Developer',
                fire_generation = 0
            WHERE id = ?
            """,
            (task_id,),
        )
        conn.execute("DELETE FROM webhook_outbox")
        conn.commit()
    return task_id


def _testclient_request(client: TestClient):
    def _request(**kwargs):
        kwargs.pop("timeout", None)
        return client.request(**kwargs)

    return _request


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}

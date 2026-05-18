"""Tests for the taskboard compatibility gateway."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from taskboard_gateway.app import (
    _resolve_gateway_max_iterations,
    create_gateway_app,
    execute_run_with_local_session,
)
from taskboard_gateway.runs import RunStore, TaskboardRun


class _FakeSession:
    """Minimal session double for local executor tests.

    Args:
        name: Session name supplied by the gateway executor.
    """

    def __init__(self, name: str) -> None:
        type(self).instances.append(self)
        self.name = name
        self.loaded = False
        self.saved = False
        self.agent_name = None
        self.auto_started = False
        self.max_iterations = None
        self.heartbeat_subscribed = None

    instances: list["_FakeSession"] = []

    def load(self) -> None:
        """Record that persisted session state was loaded."""

        self.loaded = True

    def attach_runtime(self, *, agent_name: str) -> object:
        """Record the selected local agent.

        Args:
            agent_name: Local agent name passed by the gateway.

        Returns:
            Opaque fake runner object.
        """

        self.agent_name = agent_name
        return object()

    def start_auto_mode(
        self,
        *,
        max_iterations: int,
        readonly: bool,
        heartbeat_subscribed: bool | None = None,
    ) -> dict:
        """Record auto-mode startup.

        Args:
            max_iterations: Auto-mode iteration budget.
            readonly: Whether auto mode is read-only.
            heartbeat_subscribed: Whether daemon heartbeat injections are enabled.

        Returns:
            Minimal auto-mode payload.
        """

        self.auto_started = True
        self.max_iterations = max_iterations
        self.heartbeat_subscribed = heartbeat_subscribed
        return {
            "iterations_total": max_iterations,
            "readonly": readonly,
            "heartbeat_subscribed": heartbeat_subscribed,
        }

    async def stream_agent_events(self, user_input: str, *, source: str, job_id: str):
        """Yield a deterministic fake agent event stream.

        Args:
            user_input: Prompt sent to the local session.
            source: Source label for the run.
            job_id: Gateway run id.

        Yields:
            Token and final events shaped like the daemon session stream.
        """

        yield {
            "type": "token",
            "data": f"{source}:{job_id}:{user_input[:4]}",
        }
        yield {"type": "final", "data": "fake final"}

    def save(self) -> None:
        """Record session persistence."""

        self.saved = True

    def stop_auto_mode(self, reason: str) -> dict:
        """Return a fake auto-stop payload.

        Args:
            reason: Stop reason.

        Returns:
            Auto-stop payload.
        """

        return {"reason": reason}


class TaskboardGatewayTests(unittest.TestCase):
    """Validate the OpenClaw-compatible surface used by the taskboard."""

    def setUp(self) -> None:
        """Create an isolated run store for each test."""

        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.temp_dir.name))

    def tearDown(self) -> None:
        """Remove temporary run-store files."""

        self.temp_dir.cleanup()

    def _client(self, executor=None, message_executor=None) -> TestClient:
        """Create a test client bound to this test's run store.

        Args:
            executor: Optional fake run executor.
            message_executor: Optional fake synchronous message executor.

        Returns:
            FastAPI test client.
        """

        return TestClient(
            create_gateway_app(
                store=self.store,
                executor=executor,
                message_executor=message_executor,
            )
        )

    def test_status_reports_ok(self) -> None:
        """Gateway status endpoint returns an OK payload."""

        with self._client() as client:
            response = client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["service"], "taskboard-agent-gateway")

    def test_sessions_spawn_returns_accepted_envelope_and_persists_run(self) -> None:
        """``sessions_spawn`` returns accepted details and writes a run."""

        async def fake_executor(run: TaskboardRun, store: RunStore) -> None:
            """Mark a run complete without invoking an LLM."""

            store.update_status(run, "running")
            store.update_status(run, "completed", final_text="done")

        with self._client(executor=fake_executor) as client:
            response = client.post(
                "/tools/invoke",
                json={
                    "tool": "sessions_spawn",
                    "args": {
                        "agentId": "developer",
                        "task": "# Task Assignment\n\nTask #123: Build it",
                        "label": "task-123",
                        "cleanup": "keep",
                    },
                },
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            details = payload["result"]["details"]
            self.assertEqual(details["status"], "accepted")
            self.assertIn("childSessionKey", details)
            self.assertIn("runId", details)

            run = self.store.get(details["runId"])
            self.assertIsNotNone(run)
            assert run is not None
            self.assertEqual(run.requested_agent_id, "developer")
            self.assertEqual(run.local_agent_name, "developer")
            self.assertEqual(run.task_id, 123)
            self.assertEqual(run.session_key, details["childSessionKey"])

    def test_sessions_spawn_accepts_optional_model_field(self) -> None:
        """Manual taskboard sessions may pass a model field."""

        async def fake_executor(run: TaskboardRun, store: RunStore) -> None:
            """Mark a run complete without invoking an LLM."""

            store.update_status(run, "completed", final_text="done")

        with self._client(executor=fake_executor) as client:
            response = client.post(
                "/tools/invoke",
                json={
                    "tool": "sessions_spawn",
                    "args": {
                        "agentId": "main",
                        "task": "manual session",
                        "label": "manual",
                        "cleanup": "keep",
                        "model": "local-default",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_sessions_spawn_unknown_agent_fails_closed(self) -> None:
        """Unknown taskboard agent ids fail without creating run records."""

        with self._client() as client:
            response = client.post(
                "/tools/invoke",
                json={
                    "tool": "sessions_spawn",
                    "args": {
                        "agentId": "missing-role",
                        "task": "Task #123",
                        "label": "task-123",
                        "cleanup": "keep",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("unknown agentId", payload["error"])
        self.assertEqual(self.store.list_runs(limit=None), [])

    def test_taskboard_default_role_ids_are_spawnable(self) -> None:
        """Taskboard default role ids resolve as first-class local agents."""

        role_ids = [
            "developer",
            "code-reviewer",
            "security-auditor",
            "architect",
            "qa-agent",
            "ux-manager",
            "deep-research",
        ]

        with self._client() as client:
            for role_id in role_ids:
                response = client.post(
                    "/tools/invoke",
                    json={
                        "tool": "sessions_spawn",
                        "args": {
                            "agentId": role_id,
                            "task": f"Task #100: role check for {role_id}",
                            "label": f"task-100-{role_id}",
                            "cleanup": "keep",
                        },
                    },
                )

                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertTrue(payload["ok"], payload.get("error"))

    def test_sessions_list_returns_recent_runs(self) -> None:
        """``sessions_list`` returns persisted runs in gateway shape."""

        run = self.store.create_run(
            requested_agent_id="developer",
            local_agent_name="developer",
            prompt="Task #321",
            label="task-321",
            cleanup="keep",
        )

        with self._client() as client:
            response = client.post(
                "/tools/invoke",
                json={
                    "tool": "sessions_list",
                    "args": {"limit": 10, "messageLimit": 0},
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        sessions = payload["result"]["details"]["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["runId"], run.run_id)
        self.assertEqual(sessions[0]["key"], run.session_key)

    def test_sessions_send_appends_followup(self) -> None:
        """``sessions_send`` records a follow-up message on the target run."""

        async def fake_message_executor(**kwargs) -> str:
            """Return a deterministic synchronous reply."""

            return f"reply to {kwargs['message']}"

        run = self.store.create_run(
            requested_agent_id="developer",
            local_agent_name="developer",
            prompt="Task #321",
            label="task-321",
            cleanup="keep",
        )

        with self._client(message_executor=fake_message_executor) as client:
            response = client.post(
                "/tools/invoke",
                json={
                    "tool": "sessions_send",
                    "args": {
                        "sessionKey": run.session_key,
                        "message": "follow-up",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["details"]["reply"], "reply to follow-up")
        refreshed = self.store.get(run.run_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.followups[-1]["message"], "follow-up")

    def test_sessions_send_main_session_returns_synchronous_reply(self) -> None:
        """Command-bar ``sessions_send`` works without a prior spawn record."""

        calls = []

        async def fake_message_executor(**kwargs) -> str:
            """Capture the resolved send arguments and return a reply."""

            calls.append(kwargs)
            return "main reply"

        with self._client(message_executor=fake_message_executor) as client:
            response = client.post(
                "/tools/invoke",
                json={
                    "tool": "sessions_send",
                    "args": {
                        "sessionKey": "main",
                        "message": "hello",
                        "timeoutSeconds": 90,
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["details"]["status"], "completed")
        self.assertEqual(payload["result"]["details"]["reply"], "main reply")
        self.assertEqual(calls[0]["local_agent_name"], "kai")

    def test_sessions_send_queues_when_run_is_active(self) -> None:
        """Follow-ups to currently running task sessions are queued."""

        async def hanging_executor(run: TaskboardRun, store: RunStore) -> None:
            """Keep a run active while the test sends a follow-up."""

            store.update_status(run, "running")
            await asyncio.sleep(60)

        with self._client(executor=hanging_executor) as client:
            spawn = client.post(
                "/tools/invoke",
                json={
                    "tool": "sessions_spawn",
                    "args": {
                        "agentId": "developer",
                        "task": "Task #654",
                        "label": "task-654",
                        "cleanup": "keep",
                    },
                },
            ).json()
            session_key = spawn["result"]["details"]["childSessionKey"]
            run_id = spawn["result"]["details"]["runId"]

            for _ in range(50):
                run = self.store.get(run_id)
                if run and run.status == "running":
                    break
                time.sleep(0.01)

            response = client.post(
                "/tools/invoke",
                json={
                    "tool": "sessions_send",
                    "args": {"sessionKey": session_key, "message": "later"},
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["details"]["status"], "queued")
        run = self.store.get(run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.followups[-1]["message"], "later")

    def test_cron_wake_accepts_background_main_run(self) -> None:
        """Taskboard comment wake notifications schedule a main-agent run."""

        async def fake_executor(run: TaskboardRun, store: RunStore) -> None:
            """Complete a wake run without invoking an LLM."""

            store.update_status(run, "completed", final_text="awake")

        with self._client(executor=fake_executor) as client:
            response = client.post(
                "/api/cron/wake",
                json={"action": "wake", "text": "comment notification"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        run = self.store.get(payload["runId"])
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.requested_agent_id, "main")
        self.assertEqual(run.local_agent_name, "kai")

    def test_abort_marks_run_aborted(self) -> None:
        """Abort endpoint marks a running session as aborted."""

        async def hanging_executor(run: TaskboardRun, store: RunStore) -> None:
            """Keep a run active until the test aborts it."""

            store.update_status(run, "running")
            await asyncio.sleep(60)

        with self._client(executor=hanging_executor) as client:
            spawn = client.post(
                "/tools/invoke",
                json={
                    "tool": "sessions_spawn",
                    "args": {
                        "agentId": "developer",
                        "task": "Task #456",
                        "label": "task-456",
                        "cleanup": "keep",
                    },
                },
            ).json()
            session_key = spawn["result"]["details"]["childSessionKey"]
            run_id = spawn["result"]["details"]["runId"]

            for _ in range(50):
                run = self.store.get(run_id)
                if run and run.status == "running":
                    break
                time.sleep(0.01)

            response = client.post(f"/api/sessions/{session_key}/abort")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        run = self.store.get(run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.status, "aborted")

    def test_default_executor_runs_local_session_to_completion(self) -> None:
        """Default executor drives a local session and persists final output."""

        _FakeSession.instances.clear()
        with mock.patch("taskboard_gateway.app.Session", _FakeSession):
            with self._client() as client:
                spawn = client.post(
                    "/tools/invoke",
                    json={
                        "tool": "sessions_spawn",
                        "args": {
                            "agentId": "developer",
                            "task": (
                                "Task #789: execute\n"
                                "Use start-work 789 \"Developer\" tok-123 3"
                            ),
                            "label": "task-789",
                            "cleanup": "keep",
                        },
                    },
                ).json()

                run_id = spawn["result"]["details"]["runId"]
                for _ in range(100):
                    run = self.store.get(run_id)
                    if run and run.status == "completed":
                        break
                    time.sleep(0.01)

        run = self.store.get(run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.final_text, "fake final")
        self.assertGreaterEqual(len(run.events), 2)
        self.assertTrue(_FakeSession.instances)
        context = _FakeSession.instances[0].taskboard_context
        self.assertEqual(context.session_token, "tok-123")
        self.assertEqual(context.session_generation, 3)
        self.assertFalse(_FakeSession.instances[0].heartbeat_subscribed)

    def test_gateway_max_iterations_uses_agent_config_not_hardcoded_20(self) -> None:
        """Taskboard auto-runs inherit the local agent's configured budget."""

        _FakeSession.instances.clear()
        run = self.store.create_run(
            requested_agent_id="code-reviewer",
            local_agent_name="code-reviewer",
            prompt="Task #195: review it",
            label="task-195",
            cleanup="keep",
        )

        with mock.patch("taskboard_gateway.app.Session", _FakeSession):
            asyncio.run(execute_run_with_local_session(run, self.store))

        self.assertEqual(_FakeSession.instances[-1].max_iterations, 2000)
        self.assertNotEqual(_FakeSession.instances[-1].max_iterations, 20)

    def test_gateway_max_iterations_default_env_and_malformed_fallback(self) -> None:
        """Gateway max-iteration cap is env-tunable and typo-tolerant."""

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertGreaterEqual(
                _resolve_gateway_max_iterations("code-reviewer"),
                200,
            )

        with mock.patch.dict("os.environ", {"KAI_AUTO_ITERATIONS_CAP": "321"}, clear=True):
            self.assertEqual(_resolve_gateway_max_iterations("code-reviewer"), 321)

        with mock.patch.dict("os.environ", {"KAI_AUTO_ITERATIONS_CAP": "bad"}, clear=True):
            self.assertEqual(_resolve_gateway_max_iterations("code-reviewer"), 2000)


if __name__ == "__main__":
    unittest.main()

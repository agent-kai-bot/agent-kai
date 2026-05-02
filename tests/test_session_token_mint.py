"""Phase 0 follow-up (#10247): session-token mint flow.

Verifies that:

1. ``render_taskboard_fire_prompt`` accepts ``session_token`` +
   ``session_generation`` kwargs and substitutes them into templates.
2. ``TaskboardDispatcher._mint_taskboard_session_token`` POSTs to
   ``/api/tasks/{id}/sessions`` and returns ``(token, generation)`` on
   success.
3. The mint helper degrades gracefully (empty token + ``None`` generation)
   on transport errors, non-200 responses, missing config.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.prompt_renderer import render_taskboard_fire_prompt
from agent.taskboard_dispatcher import TaskboardDispatcher


class _StubResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or (str(payload) if payload else "")

    def json(self) -> dict:
        return self._payload


class _StubHttpClient:
    """Minimal httpx.Client stand-in used as a context manager."""

    def __init__(self, response: _StubResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json or {}))
        return self.response


class PromptRendererSessionTokenTests(unittest.TestCase):
    def test_template_receives_session_token_and_generation(self) -> None:
        rendered = render_taskboard_fire_prompt(
            "developer",
            {"id": 999, "title": "smoke", "description": "x"},
            session_token="tok-abc-123",
            session_generation=7,
        )
        self.assertIn("tok-abc-123", rendered)
        self.assertIn("Session generation: 7", rendered)

    def test_missing_session_kwargs_render_empty(self) -> None:
        rendered = render_taskboard_fire_prompt(
            "developer",
            {"id": 999, "title": "smoke", "description": "x"},
        )
        # Template line literally is "- Session generation: {session_generation}"
        # which renders empty via _SafeFormatDict — verify no stray placeholder.
        self.assertNotIn("{session_token}", rendered)
        self.assertNotIn("{session_generation}", rendered)


class MintSessionTokenTests(unittest.TestCase):
    def _dispatcher(self) -> TaskboardDispatcher:
        # The helpers _mint_taskboard_session_token / _taskboard_base_url /
        # _taskboard_bearer_token are independent of the rest of the
        # dispatcher state, so we can construct a minimal instance.
        d = TaskboardDispatcher.__new__(TaskboardDispatcher)
        d._agent_runs_client = MagicMock(base_url="http://taskboard.local:18180")
        return d

    def test_returns_token_and_generation_on_200(self) -> None:
        resp = _StubResponse(
            200,
            payload={
                "task_id": 42,
                "agent": "developer",
                "token": "uuid-token-abc",
                "generation": 3,
            },
        )
        client = _StubHttpClient(resp)
        d = self._dispatcher()
        with patch.dict("os.environ", {"TASKBOARD_BEARER_TOKEN": "secret"}):
            with patch("httpx.Client", return_value=client):
                token, gen = d._mint_taskboard_session_token(task_id=42, role="developer")
        self.assertEqual(token, "uuid-token-abc")
        self.assertEqual(gen, 3)
        self.assertEqual(len(client.calls), 1)
        url, body = client.calls[0]
        self.assertTrue(url.endswith("/api/tasks/42/sessions"))
        self.assertEqual(body["agent"], "developer")
        self.assertTrue(body["allow_parallel_review"])

    def test_returns_empty_on_non_200(self) -> None:
        resp = _StubResponse(500, payload={"detail": "boom"}, text="boom")
        client = _StubHttpClient(resp)
        d = self._dispatcher()
        with patch.dict("os.environ", {"TASKBOARD_BEARER_TOKEN": "secret"}):
            with patch("httpx.Client", return_value=client):
                token, gen = d._mint_taskboard_session_token(task_id=42, role="developer")
        self.assertEqual(token, "")
        self.assertIsNone(gen)

    def test_returns_empty_on_transport_exception(self) -> None:
        d = self._dispatcher()

        class _BoomClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                raise ConnectionError("net down")

        with patch.dict("os.environ", {"TASKBOARD_BEARER_TOKEN": "secret"}):
            with patch("httpx.Client", return_value=_BoomClient()):
                token, gen = d._mint_taskboard_session_token(task_id=42, role="developer")
        self.assertEqual(token, "")
        self.assertIsNone(gen)

    def test_returns_empty_when_bearer_missing(self) -> None:
        d = self._dispatcher()
        with patch.dict("os.environ", {}, clear=True):
            token, gen = d._mint_taskboard_session_token(task_id=42, role="developer")
        self.assertEqual(token, "")
        self.assertIsNone(gen)

    def test_returns_empty_when_base_url_missing(self) -> None:
        d = self._dispatcher()
        d._agent_runs_client = MagicMock(base_url=None)
        with patch.dict("os.environ", {"TASKBOARD_BEARER_TOKEN": "secret"}, clear=True):
            token, gen = d._mint_taskboard_session_token(task_id=42, role="developer")
        self.assertEqual(token, "")
        self.assertIsNone(gen)


if __name__ == "__main__":
    unittest.main()

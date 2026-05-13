"""Tests for agent.run_outcome_reaper — close-the-loop on agent_runs ledger."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agent.agent_runs_client import AgentRunsClient
from agent.run_outcome_reaper import (
    ReaperStateStore,
    iter_run_files,
    parse_run_artifact,
    reap_directory,
    reap_one,
    summarize_results,
)


def _write_run_file(directory: Path, run_id: str, payload: dict) -> Path:
    path = directory / f"{run_id}.json"
    payload.setdefault("run_id", run_id)
    payload.setdefault("status", "completed")
    payload.setdefault("session_key", payload.get("session_key", ""))
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


class _StubClient:
    """Drop-in replacement for AgentRunsClient that records calls."""

    def __init__(
        self,
        *,
        ledger_rows: dict[int, list[dict]] | None = None,
        post_audit_ok: bool = True,
    ) -> None:
        self.enabled = True
        self.ledger_rows = ledger_rows or {}
        self.patches: list[tuple[int, dict]] = []
        self.audit_comments: list[tuple[int, str]] = []
        self.creates: list[dict] = []
        self.post_audit_ok = post_audit_ok

    def list_for_task(self, task_id: int, *, role=None, status=None, limit=200):
        return list(self.ledger_rows.get(task_id, []))

    def patch(self, run_id: int, body: dict):
        self.patches.append((run_id, body))
        return {"id": run_id, **body}

    def post_audit_comment(self, task_id: int, content: str) -> bool:
        self.audit_comments.append((task_id, content))
        return self.post_audit_ok

    def create(self, body: dict) -> int:
        self.creates.append(body)
        return 999


class ParseRunArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parses_minimal_run_file(self) -> None:
        path = _write_run_file(
            self.dir,
            "run_abc123",
            {
                "session_key": "agent:code-reviewer:task-10213-code-review:run_abc123",
                "status": "completed",
                "started_at": "2026-04-30T17:40:33Z",
                "ended_at": "2026-04-30T17:40:35Z",
                "events": [
                    {"type": "error", "data": "Primary endpoint failed: Connection error."},
                    {"type": "final", "data": "Error: agent returned an empty response."},
                ],
                "final_text": "Error: agent returned an empty response.",
            },
        )
        artifact = parse_run_artifact(path)
        assert artifact is not None
        self.assertEqual(artifact.run_id, "run_abc123")
        self.assertEqual(artifact.task_id, 10213)
        self.assertEqual(artifact.role, "code-reviewer")
        self.assertEqual(artifact.status, "completed")
        self.assertTrue(artifact.is_terminal)
        self.assertAlmostEqual(artifact.elapsed_seconds, 2.0, places=1)

    def test_returns_none_on_invalid_json(self) -> None:
        path = self.dir / "bad.json"
        path.write_text("{ this is not json", encoding="utf-8")
        self.assertIsNone(parse_run_artifact(path))

    def test_returns_none_when_missing_required_fields(self) -> None:
        path = self.dir / "empty.json"
        path.write_text("{}", encoding="utf-8")
        self.assertIsNone(parse_run_artifact(path))

    def test_running_artifact_not_terminal(self) -> None:
        path = _write_run_file(
            self.dir,
            "run_xyz",
            {
                "session_key": "agent:developer:task-10213:run_xyz",
                "status": "running",
            },
        )
        artifact = parse_run_artifact(path)
        assert artifact is not None
        self.assertFalse(artifact.is_terminal)


class ReaperStateStoreTests(unittest.TestCase):
    def test_record_and_has_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite"
            store = ReaperStateStore(db)
            self.assertFalse(store.has_seen("run_a"))
            store.record(
                run_id="run_a",
                ended_at="2026-04-30T17:40:35Z",
                ledger_run_id=42,
                terminal_status="endpoint_failed",
                failure_class="endpoint_unreachable",
            )
            self.assertTrue(store.has_seen("run_a"))


class ReapOneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state = ReaperStateStore(self.dir / "state.sqlite")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_endpoint_failed_artifact(self, *, run_id="run_xx", task_id=10213):
        path = _write_run_file(
            self.dir,
            run_id,
            {
                "session_key": f"agent:code-reviewer:task-{task_id}-code-review:{run_id}",
                "status": "completed",
                "started_at": "2026-04-30T17:40:33Z",
                "ended_at": "2026-04-30T17:40:35Z",
                "events": [
                    {"type": "error", "data": "Primary endpoint failed: Connection error."},
                    {"type": "final", "data": "Error: agent returned an empty response."},
                ],
                "final_text": "Error: agent returned an empty response.",
            },
        )
        return parse_run_artifact(path)

    def test_terminal_run_resolves_ledger_row_and_patches(self) -> None:
        artifact = self._make_endpoint_failed_artifact()
        client = _StubClient(
            ledger_rows={
                10213: [
                    {
                        "id": 100,
                        "session_id": artifact.session_key,
                        "task_id": 10213,
                        "role": "code-reviewer",
                        "status": "spawning",
                    }
                ]
            }
        )
        result = reap_one(artifact, client=client, state=self.state)
        self.assertFalse(result.skipped)
        self.assertEqual(result.outcome.status, "endpoint_failed")
        self.assertEqual(result.outcome.failure_class, "endpoint_unreachable")
        self.assertEqual(result.ledger_run_id, 100)
        # PATCH body includes status, failure_class, failure_detail, finished_at.
        self.assertEqual(len(client.patches), 1)
        patch_id, patch_body = client.patches[0]
        self.assertEqual(patch_id, 100)
        self.assertEqual(patch_body["status"], "endpoint_failed")
        self.assertEqual(patch_body["failure_class"], "endpoint_unreachable")
        self.assertIn("finished_at", patch_body)
        # Audit comment posted with [KAI] FAILED format.
        self.assertEqual(len(client.audit_comments), 1)
        task_id, comment = client.audit_comments[0]
        self.assertEqual(task_id, 10213)
        self.assertTrue(comment.startswith("[KAI] FAILED code-reviewer: endpoint_unreachable"))

    def test_skips_runs_already_reaped(self) -> None:
        artifact = self._make_endpoint_failed_artifact(run_id="run_dup")
        client = _StubClient(ledger_rows={10213: []})
        # First run: process normally.
        reap_one(artifact, client=client, state=self.state)
        # Second run: should skip.
        result = reap_one(artifact, client=client, state=self.state)
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "already_reaped")

    def test_skips_non_terminal_runs(self) -> None:
        path = _write_run_file(
            self.dir,
            "run_running",
            {
                "session_key": "agent:developer:task-9999:run_running",
                "status": "running",
            },
        )
        artifact = parse_run_artifact(path)
        client = _StubClient()
        result = reap_one(artifact, client=client, state=self.state)
        self.assertTrue(result.skipped)
        self.assertEqual(result.skip_reason, "run_not_terminal")
        self.assertEqual(client.patches, [])
        self.assertEqual(client.audit_comments, [])

    def test_dry_run_does_not_patch_or_post(self) -> None:
        artifact = self._make_endpoint_failed_artifact(run_id="run_dry")
        client = _StubClient(ledger_rows={10213: []})
        result = reap_one(artifact, client=client, state=self.state, dry_run=True)
        self.assertFalse(result.skipped)
        self.assertIsNotNone(result.outcome)
        self.assertEqual(client.patches, [])
        self.assertEqual(client.audit_comments, [])
        # Dry-run should also NOT mark seen.
        self.assertFalse(self.state.has_seen("run_dry"))

    def test_succeeded_run_posts_completed_comment(self) -> None:
        path = _write_run_file(
            self.dir,
            "run_ok",
            {
                "session_key": "agent:qa-agent:task-10213-qa:run_ok",
                "status": "completed",
                "started_at": "2026-04-30T20:00:00Z",
                "ended_at": "2026-04-30T20:01:12Z",
                "events": [{"type": "final", "data": "Verdict: APPROVED."}],
                "final_text": "Verdict: APPROVED. Tests pass.",
            },
        )
        artifact = parse_run_artifact(path)
        client = _StubClient(
            ledger_rows={
                10213: [
                    {"id": 200, "session_id": artifact.session_key, "task_id": 10213}
                ]
            }
        )
        result = reap_one(artifact, client=client, state=self.state)
        self.assertEqual(result.outcome.status, "succeeded")
        _, comment = client.audit_comments[0]
        self.assertTrue(comment.startswith("[KAI] COMPLETED qa-agent: ok"))

    def test_done_session_with_late_wall_clock_sentinel_marks_succeeded(self) -> None:
        artifact_path = self.dir / "code-reviewer" / "claude" / "artifacts" / "10413-final.txt"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("review complete\n", encoding="utf-8")
        final_text = (
            "Task #10413 is complete.\n"
            f"Wrote final artifact to `{artifact_path}`.\n\n"
            "[AUTO_STATE: done]"
        )
        path = _write_run_file(
            self.dir,
            "run_10413_300",
            {
                "session_key": "agent:code-reviewer:task-10413-code-review:run_10413_300",
                "status": "completed",
                "started_at": "2026-05-13T14:58:35.756Z",
                "ended_at": "2026-05-13T14:58:35.765Z",
                "events": [
                    {"type": "final", "data": final_text},
                    {
                        "type": "auto_stopped",
                        "data": {
                            "reason": "wall-clock budget exceeded",
                            "elapsed_seconds": 241.7,
                        },
                    },
                ],
                "final_text": final_text,
            },
        )
        artifact = parse_run_artifact(path)
        client = _StubClient(
            ledger_rows={
                10413: [
                    {
                        "id": 300,
                        "session_id": artifact.session_key,
                        "task_id": 10413,
                        "role": "code-reviewer",
                        "status": "running",
                    }
                ]
            }
        )

        result = reap_one(artifact, client=client, state=self.state)

        self.assertEqual(result.outcome.status, "succeeded")
        self.assertIsNone(result.outcome.failure_class)
        self.assertEqual(client.patches[0][0], 300)
        self.assertEqual(client.patches[0][1]["status"], "succeeded")
        self.assertNotIn("failure_class", client.patches[0][1])
        self.assertAlmostEqual(artifact.elapsed_seconds, 0.009, places=3)

    def test_real_wall_clock_budget_marks_specific_failure_class(self) -> None:
        path = _write_run_file(
            self.dir,
            "run_wall_clock",
            {
                "session_key": "agent:developer:task-10413:run_wall_clock",
                "status": "completed",
                "started_at": "2026-05-13T14:00:00Z",
                "ended_at": "2026-05-13T14:03:02Z",
                "events": [
                    {
                        "type": "auto_stopped",
                        "data": {
                            "reason": "wall-clock budget exceeded",
                            "elapsed_seconds": 182.0,
                        },
                    }
                ],
            },
        )
        artifact = parse_run_artifact(path)
        client = _StubClient(
            ledger_rows={
                10413: [
                    {"id": 301, "session_id": artifact.session_key, "task_id": 10413}
                ]
            }
        )

        result = reap_one(artifact, client=client, state=self.state)

        self.assertEqual(result.outcome.status, "failed")
        self.assertEqual(result.outcome.failure_class, "wall_clock_budget_exceeded")
        self.assertIn("elapsed=182.0s", result.outcome.failure_detail)
        self.assertEqual(client.patches[0][1]["failure_class"], "wall_clock_budget_exceeded")


class ReapDirectoryTests(unittest.TestCase):
    def test_processes_all_terminal_files_oldest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            paths = []
            for run_id in ("run_a", "run_b", "run_c"):
                path = _write_run_file(
                    tmp_dir,
                    run_id,
                    {
                        "session_key": f"agent:developer:task-10213:{run_id}",
                        "status": "completed",
                        "events": [
                            {"type": "final", "data": "ok"}
                        ],
                        "final_text": "ok",
                    },
                )
                paths.append(path)
            client = _StubClient(ledger_rows={10213: []})
            state = ReaperStateStore(tmp_dir / "state.sqlite")
            results = reap_directory(
                client=client,
                state=state,
                directory=tmp_dir,
            )
            self.assertEqual(len(results), 3)
            # Each run was patched (ledger_run_id may be None since rows are empty,
            # but the audit comment still posts).
            self.assertEqual(len(client.audit_comments), 3)
            histogram = summarize_results(results)
            self.assertEqual(histogram.get("succeeded"), 3)


if __name__ == "__main__":
    unittest.main()

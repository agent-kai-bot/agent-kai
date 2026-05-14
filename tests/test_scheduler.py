"""Focused scheduler override persistence tests for #10427."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from daemon.scheduler import SCHEDULER_V2_ROUTING_KEY, ScheduledJob, Scheduler, _utc_now


class SchedulerOverrideTests(unittest.TestCase):
    def test_scheduled_job_accepts_v2_fields_and_v1_payloads(self):
        created_at = _utc_now().replace(microsecond=0).isoformat()
        v1_job = ScheduledJob.model_validate(
            {
                "id": "job-v1",
                "type": "event",
                "spec": {"channel": "signals", "filter": {"symbol": "BTC"}},
                "prompt": "Check BTC",
                "owner_session": "terminal",
                "created_at": created_at,
                "created_by": "agent",
            }
        )
        self.assertIsNone(v1_job.target_agent_role)
        self.assertEqual(v1_job.routing_overrides(), {})

        v2_job = ScheduledJob.model_validate(
            {
                "id": "job-v2",
                "type": "event",
                "spec": {"channel": "signals", "filter": {"symbol": "BTC"}},
                "prompt": "Check BTC",
                "owner_session": "terminal",
                "created_at": created_at,
                "created_by": "agent",
                "target_agent_role": "analyst",
                "reasoning_effort": "x-high",
                "thinking_level": "xhigh",
                "extra_env": {"KAI_TEST": "1"},
            }
        )
        self.assertEqual(v2_job.target_agent_role, "analyst")
        self.assertEqual(v2_job.reasoning_effort, "xhigh")
        self.assertEqual(v2_job.effective_reasoning_effort, "xhigh")
        self.assertEqual(v2_job.extra_env, {"KAI_TEST": "1"})

    def test_sidecar_round_trip_keeps_v1_jobs_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_path = Path(tmpdir) / "scheduler" / "jobs.json"
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=jobs_path,
            )
            job = scheduler.create_event_job(
                condition={"channel": "signals", "filter": {"symbol": "BTC"}},
                prompt="Check BTC",
                owner_session="terminal",
                created_by="agent",
                target_agent_role="analyst",
                reasoning_effort="xhigh",
                extra_env={"KAI_TEST": "1"},
            )

            persisted = json.loads(jobs_path.read_text(encoding="utf-8"))
            self.assertNotIn("target_agent_role", persisted["jobs"][job.id])
            self.assertNotIn("reasoning_effort", persisted["jobs"][job.id])
            self.assertEqual(
                persisted[SCHEDULER_V2_ROUTING_KEY][job.id],
                {
                    "extra_env": {"KAI_TEST": "1"},
                    "reasoning_effort": "xhigh",
                    "target_agent_role": "analyst",
                },
            )

            reloaded = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=jobs_path,
            )
            jobs = reloaded.load_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].target_agent_role, "analyst")
            self.assertEqual(jobs[0].reasoning_effort, "xhigh")
            self.assertEqual(jobs[0].extra_env, {"KAI_TEST": "1"})

    def test_unknown_and_invalid_sidecar_entries_are_logged_and_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_path = Path(tmpdir) / "scheduler" / "jobs.json"
            jobs_path.parent.mkdir(parents=True, exist_ok=True)
            jobs_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "jobs": {
                            "job-valid": {
                                "id": "job-valid",
                                "type": "event",
                                "spec": {
                                    "channel": "signals",
                                    "filter": {"symbol": "BTC"},
                                },
                                "prompt": "Check BTC",
                                "owner_session": "terminal",
                                "created_at": "2026-04-10T00:00:00+00:00",
                                "created_by": "agent",
                            }
                        },
                        SCHEDULER_V2_ROUTING_KEY: {
                            "job-valid": {"unknown": "field"},
                            "job-missing": {"target_agent_role": "analyst"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=jobs_path,
            )

            with self.assertLogs("daemon.scheduler", level="WARNING") as captured:
                jobs = scheduler.load_jobs()

            self.assertEqual(len(jobs), 1)
            self.assertIsNone(jobs[0].target_agent_role)
            messages = "\n".join(captured.output)
            self.assertIn("dropping invalid scheduler routing sidecar", messages)
            self.assertIn("unknown job job-missing", messages)

    def test_v1_jobs_file_loads_without_modification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_path = Path(tmpdir) / "scheduler" / "jobs.json"
            jobs_path.parent.mkdir(parents=True, exist_ok=True)
            original = json.dumps(
                {
                    "version": 1,
                    "jobs": {
                        "job-v1": {
                            "id": "job-v1",
                            "type": "event",
                            "spec": {"channel": "signals", "filter": {"symbol": "BTC"}},
                            "prompt": "Check BTC",
                            "owner_session": "terminal",
                            "created_at": "2026-04-10T00:00:00+00:00",
                            "created_by": "agent",
                        }
                    },
                },
                indent=2,
            )
            jobs_path.write_text(original, encoding="utf-8")

            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=jobs_path,
            )
            jobs = scheduler.load_jobs()

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()

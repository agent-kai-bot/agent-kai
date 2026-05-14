"""Focused scheduler tool override tests for #10427."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.tools import create_tools
from daemon.scheduler import Scheduler


class SchedulerToolOverrideTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_tools_accept_override_arguments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
            )
            await scheduler.start()
            try:
                session = SimpleNamespace(name="alpha")
                tool_map = {
                    tool.name: tool
                    for tool in create_tools(scheduler=scheduler, session=session)
                }

                result = tool_map["schedule_recurring"].invoke(
                    {
                        "cron": "*/5 * * * *",
                        "prompt": "Check BTC Wyckoff structure",
                        "target_agent_role": "analyst",
                        "reasoning_effort": "x-high",
                        "extra_env": {"KAI_TEST_MODE": "1"},
                    }
                )

                self.assertIn("Scheduled recurring job", result)
                job = scheduler.list_jobs_for_session("alpha")[0]
                self.assertEqual(job.target_agent_role, "analyst")
                self.assertEqual(job.reasoning_effort, "xhigh")
                self.assertEqual(job.extra_env, {"KAI_TEST_MODE": "1"})
            finally:
                await scheduler.shutdown()

    async def test_schedule_tools_reject_invalid_reasoning_effort(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
            )
            await scheduler.start()
            try:
                session = SimpleNamespace(name="alpha")
                tool_map = {
                    tool.name: tool
                    for tool in create_tools(scheduler=scheduler, session=session)
                }

                with self.assertRaisesRegex(ValueError, "invalid reasoning_effort"):
                    tool_map["schedule_at"].invoke(
                        {
                            "when": "2026-06-10T01:00:00+00:00",
                            "prompt": "Check BTC",
                            "reasoning_effort": "ultra",
                        }
                    )
            finally:
                await scheduler.shutdown()


if __name__ == "__main__":
    unittest.main()

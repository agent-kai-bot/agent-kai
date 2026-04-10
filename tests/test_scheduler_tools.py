"""Tests for scheduler management tools exposed to the agent runtime."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.tools import create_tools
from daemon.scheduler import Scheduler


class SchedulerToolTests(unittest.IsolatedAsyncioTestCase):
    """Validate the Phase 5 scheduler tools."""

    async def test_schedule_tools_create_and_list_jobs_for_current_session(self):
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
                    for tool in create_tools(
                        scheduler=scheduler,
                        session=session,
                    )
                }

                result = tool_map["schedule_at"].invoke(
                    {
                        "when": "2026-04-10T01:00:00+00:00",
                        "prompt": "Check BTC",
                    }
                )
                event_result = tool_map["schedule_when"].invoke(
                    {
                        "condition": {
                            "channel": "signals",
                            "filter": {"symbol": "BTC", "score": {"gte": 0.9}},
                        },
                        "prompt": "Summarize the signal",
                    }
                )
                listed = tool_map["list_scheduled_jobs"].invoke({})

                self.assertIn("Scheduled", result)
                self.assertIn("Scheduled event job", event_result)
                self.assertIn("session=alpha", listed)
                self.assertEqual(len(scheduler.list_jobs_for_session("alpha")), 2)
            finally:
                await scheduler.shutdown()

    async def test_pause_resume_and_cancel_tools_update_scheduler_state(self):
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
                    for tool in create_tools(
                        scheduler=scheduler,
                        session=session,
                    )
                }

                tool_map["schedule_recurring"].invoke(
                    {
                        "cron": "*/5 * * * *",
                        "prompt": "Recurring check",
                    }
                )
                job = scheduler.list_jobs_for_session("alpha")[0]

                paused = tool_map["pause_scheduled_job"].invoke({"job_id": job.id})
                resumed = tool_map["resume_scheduled_job"].invoke({"job_id": job.id})
                cancelled = tool_map["cancel_scheduled_job"].invoke({"job_id": job.id})

                self.assertIn("Paused", paused)
                self.assertIn("Resumed", resumed)
                self.assertIn("Cancelled", cancelled)
                self.assertEqual(scheduler.get_job(job.id).status, "cancelled")
            finally:
                await scheduler.shutdown()

    async def test_loop_guard_blocks_likely_self_scheduling(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
            )
            await scheduler.start()
            try:
                current_job = scheduler.create_event_job(
                    condition={
                        "channel": "signals",
                        "filter": {"symbol": "BTC", "score": {"gte": 0.9}},
                    },
                    prompt="Summarize the signal",
                    owner_session="alpha",
                    created_by="user",
                )
                session = SimpleNamespace(
                    name="alpha",
                    current_source="scheduler",
                    current_job_id=current_job.id,
                )
                tool_map = {
                    tool.name: tool
                    for tool in create_tools(
                        scheduler=scheduler,
                        session=session,
                    )
                }

                blocked = tool_map["schedule_when"].invoke(
                    {
                        "condition": {
                            "channel": "signals",
                            "filter": {"symbol": "BTC", "score": {"gte": 0.9}},
                        },
                        "prompt": "Summarize the signal",
                    }
                )

                self.assertIn("Refusing to create a likely self-scheduling loop", blocked)
                self.assertEqual(len(scheduler.list_jobs_for_session("alpha")), 1)
            finally:
                await scheduler.shutdown()


if __name__ == "__main__":
    unittest.main()

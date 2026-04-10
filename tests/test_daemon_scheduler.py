"""Tests for daemon scheduler primitives."""

from __future__ import annotations

import unittest
from datetime import timedelta

from daemon.scheduler import Scheduler, _utc_now


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    """Validate the Phase 5 scheduler wrapper."""

    async def test_absolute_jobs_dispatch_through_callback(self):
        fired: list[tuple[str, str]] = []

        async def dispatch(job, fired_at):
            fired.append((job["id"], fired_at.isoformat()))

        scheduler = Scheduler(dispatch_callback=dispatch)
        await scheduler.start()
        try:
            when = (_utc_now() + timedelta(minutes=1)).replace(microsecond=0)
            job = {
                "id": "job-absolute",
                "type": "absolute",
                "spec": {"at": when.isoformat()},
            }

            next_run = scheduler.schedule_job(job)
            self.assertEqual(next_run, when)

            await scheduler._fire_scheduled_job(job["id"])

            self.assertEqual([item[0] for item in fired], ["job-absolute"])
            self.assertEqual(scheduler.get_job("job-absolute"), job)
        finally:
            await scheduler.shutdown()

    async def test_cron_jobs_validate_and_register_next_run(self):
        scheduler = Scheduler(dispatch_callback=lambda *_args: None)
        await scheduler.start()
        try:
            next_run = scheduler.schedule_job(
                {
                    "id": "job-cron",
                    "type": "cron",
                    "spec": {"cron": "*/5 * * * *", "tz": "UTC"},
                }
            )

            self.assertIsNotNone(next_run)
            self.assertIsNotNone(scheduler.next_run("job-cron"))

            with self.assertRaisesRegex(ValueError, "valid cron expression"):
                scheduler.schedule_job(
                    {
                        "id": "job-bad-cron",
                        "type": "cron",
                        "spec": {"cron": "not a cron"},
                    }
                )
        finally:
            await scheduler.shutdown()

    async def test_pause_resume_and_remove_proxy_to_apscheduler(self):
        scheduler = Scheduler(dispatch_callback=lambda *_args: None)
        await scheduler.start()
        try:
            when = (_utc_now() + timedelta(minutes=1)).replace(microsecond=0)
            scheduler.schedule_job(
                {
                    "id": "job-ops",
                    "type": "absolute",
                    "spec": {"at": when.isoformat()},
                }
            )

            scheduler.pause_job("job-ops")
            self.assertIsNone(scheduler.next_run("job-ops"))

            scheduler.resume_job("job-ops")
            self.assertIsNotNone(scheduler.next_run("job-ops"))

            removed = scheduler.remove_job("job-ops")
            self.assertTrue(removed)
            self.assertIsNone(scheduler.get_job("job-ops"))
        finally:
            await scheduler.shutdown()


if __name__ == "__main__":
    unittest.main()

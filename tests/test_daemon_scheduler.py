"""Tests for daemon scheduler primitives."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timezone, timedelta
from pathlib import Path

from daemon.scheduler import (
    DaemonEventBus,
    ScheduledJob,
    Scheduler,
    _utc_now,
    matches_structured_filter,
)


class _FakeAsyncIOScheduler:
    def __init__(self, *_, **__):
        self.jobs = {}
        self.listeners = []
        self.started = False

    def add_listener(self, callback, mask):
        self.listeners.append((callback, mask))

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.started = False

    def add_job(self, func, *, trigger, id, replace_existing=False, args=None, **kwargs):
        self.jobs[id] = {
            "func": func,
            "trigger": trigger,
            "args": args or [],
            "kwargs": kwargs,
            "next_run_time": trigger.get_next_fire_time(None, _utc_now()),
        }

    def get_job(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            return None
        return type("FakeJob", (), {"next_run_time": job["next_run_time"]})()

    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)

    def pause_job(self, job_id):
        if job_id in self.jobs:
            self.jobs[job_id]["next_run_time"] = None

    def resume_job(self, job_id):
        if job_id in self.jobs:
            trigger = self.jobs[job_id]["trigger"]
            self.jobs[job_id]["next_run_time"] = trigger.get_next_fire_time(None, _utc_now())


class _FakeMissedEvent:
    def __init__(self, job_id: str, scheduled_run_time=None):
        from apscheduler.events import EVENT_JOB_MISSED

        self.code = EVENT_JOB_MISSED
        self.job_id = job_id
        self.scheduled_run_time = scheduled_run_time


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    """Validate the Phase 5 scheduler wrapper."""

    async def test_absolute_jobs_dispatch_through_callback(self):
        fired: list[tuple[str, str]] = []
        created_at = _utc_now().replace(microsecond=0).isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            async def dispatch(job, fired_at):
                fired.append((job.id, fired_at.isoformat()))

            scheduler = Scheduler(
                dispatch_callback=dispatch,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
            )
            await scheduler.start()
            try:
                when = (_utc_now() + timedelta(minutes=1)).replace(microsecond=0)
                job = {
                    "id": "job-absolute",
                    "type": "absolute",
                    "spec": {"at": when.isoformat()},
                    "prompt": "Check BTC",
                    "owner_session": "terminal",
                    "created_at": created_at,
                    "created_by": "user",
                }

                next_run = scheduler.schedule_job(job, persist=False)
                self.assertEqual(next_run, when)

                await scheduler._fire_scheduled_job(job["id"])

                self.assertEqual([item[0] for item in fired], ["job-absolute"])
                scheduled = scheduler.get_job("job-absolute")
                self.assertIsNotNone(scheduled)
                self.assertEqual(scheduled.id, "job-absolute")
                self.assertEqual(scheduled.prompt, "Check BTC")
            finally:
                await scheduler.shutdown()

    async def test_cron_jobs_validate_and_register_next_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
            )
            await scheduler.start()
            try:
                created_at = _utc_now().replace(microsecond=0).isoformat()
                next_run = scheduler.schedule_job(
                    {
                        "id": "job-cron",
                        "type": "cron",
                        "spec": {"cron": "*/5 * * * *", "tz": "UTC"},
                        "prompt": "Recurring check",
                        "owner_session": "terminal",
                        "created_at": created_at,
                        "created_by": "agent",
                    },
                    persist=False,
                )

                self.assertIsNotNone(next_run)
                self.assertIsNotNone(scheduler.next_run("job-cron"))

                with self.assertRaisesRegex(ValueError, "valid cron expression"):
                    scheduler.schedule_job(
                        {
                            "id": "job-bad-cron",
                            "type": "cron",
                            "spec": {"cron": "not a cron"},
                            "prompt": "Bad recurring check",
                            "owner_session": "terminal",
                            "created_at": created_at,
                            "created_by": "agent",
                        },
                        persist=False,
                    )
            finally:
                await scheduler.shutdown()

    async def test_pause_resume_and_remove_proxy_to_apscheduler(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
            )
            await scheduler.start()
            try:
                when = (_utc_now() + timedelta(minutes=1)).replace(microsecond=0)
                created_at = _utc_now().replace(microsecond=0).isoformat()
                scheduler.schedule_job(
                    {
                        "id": "job-ops",
                        "type": "absolute",
                        "spec": {"at": when.isoformat()},
                        "prompt": "Ops check",
                        "owner_session": "terminal",
                        "created_at": created_at,
                        "created_by": "user",
                    },
                    persist=False,
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

    async def test_event_jobs_fire_when_daemon_event_bus_matches(self):
        fired: list[str] = []
        created_at = _utc_now().replace(microsecond=0).isoformat()
        event_bus = DaemonEventBus()
        with tempfile.TemporaryDirectory() as tmpdir:
            async def dispatch(job, _fired_at):
                fired.append(job.id)

            scheduler = Scheduler(
                dispatch_callback=dispatch,
                event_bus=event_bus,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
            )
            await scheduler.start()
            try:
                scheduler.schedule_job(
                    {
                        "id": "job-event-match",
                        "type": "event",
                        "spec": {
                            "channel": "signals",
                            "filter": {
                                "symbol": "BTC",
                                "score": {"gte": 0.9},
                            },
                        },
                        "prompt": "Summarize the signal",
                        "owner_session": "terminal",
                        "created_at": created_at,
                        "created_by": "agent",
                    },
                    persist=False,
                )

                await event_bus.publish(
                    "signals",
                    {"symbol": "ETH", "score": 0.95},
                )
                await event_bus.publish(
                    "signals",
                    {"symbol": "BTC", "score": 0.95},
                )

                self.assertEqual(fired, ["job-event-match"])
            finally:
                await scheduler.shutdown()

    def test_event_jobs_validate_structured_filters(self):
        created_at = _utc_now().replace(microsecond=0).isoformat()
        job = ScheduledJob.model_validate(
            {
                "id": "job-event",
                "type": "event",
                "spec": {
                    "channel": "signals",
                    "filter": {
                        "symbol": "BTC",
                        "score": {"gte": 0.8},
                        "side": ["long", "strong-long"],
                    },
                },
                "prompt": "Summarize the signal",
                "owner_session": "terminal",
                "created_at": created_at,
                "created_by": "agent",
            }
        )

        self.assertEqual(job.type, "event")

        with self.assertRaisesRegex(ValueError, "invalid structured filter"):
            ScheduledJob.model_validate(
                {
                    "id": "job-bad-event",
                    "type": "event",
                    "spec": {
                        "channel": "signals",
                        "filter": {"score": {"between": [0.8, 1.0]}},
                    },
                    "prompt": "Broken filter",
                    "owner_session": "terminal",
                    "created_at": created_at,
                    "created_by": "agent",
                }
            )

    def test_structured_filter_matching_supports_core_operators(self):
        payload = {
            "symbol": "BTC",
            "side": "long",
            "score": 0.91,
            "strategy": "breakout_alpha",
        }

        self.assertTrue(
            matches_structured_filter(
                payload,
                {
                    "symbol": "BTC",
                    "score": {"gt": 0.9},
                    "strategy": {"regex": "breakout"},
                },
            )
        )
        self.assertFalse(
            matches_structured_filter(
                payload,
                {
                    "side": {"ne": "long"},
                },
            )
        )

    async def test_jobs_persist_and_reload_from_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_path = Path(tmpdir) / "scheduler" / "jobs.json"
            created_at = _utc_now().replace(microsecond=0).isoformat()
            when = (_utc_now() + timedelta(minutes=2)).replace(microsecond=0)

            first = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=jobs_path,
            )
            await first.start()
            try:
                first.schedule_job(
                    {
                        "id": "job-persisted",
                        "type": "absolute",
                        "spec": {"at": when.isoformat()},
                        "prompt": "Reload me",
                        "owner_session": "terminal",
                        "created_at": created_at,
                        "created_by": "user",
                    }
                )
            finally:
                await first.shutdown()

            self.assertTrue(jobs_path.exists())

            second = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=jobs_path,
            )
            loaded = second.load_jobs()

            self.assertEqual([job.id for job in loaded], ["job-persisted"])
            self.assertEqual(second.get_job("job-persisted").prompt, "Reload me")

    async def test_session_job_limit_rejects_extra_active_jobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
                session_job_limit=1,
            )
            await scheduler.start()
            try:
                scheduler.create_absolute_job(
                    when="2026-04-10T01:00:00+00:00",
                    prompt="First job",
                    owner_session="alpha",
                    created_by="user",
                )

                with self.assertRaisesRegex(ValueError, "already has 1 active"):
                    scheduler.create_event_job(
                        condition={
                            "channel": "signals",
                            "filter": {"symbol": "BTC"},
                        },
                        prompt="Second job",
                        owner_session="alpha",
                        created_by="user",
                    )
            finally:
                await scheduler.shutdown()

    async def test_cron_misfire_event_triggers_catch_up_dispatch(self):
        fired: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            async def dispatch(job, _fired_at):
                fired.append(job.id)

            scheduler = Scheduler(
                dispatch_callback=dispatch,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
                apscheduler_factory=_FakeAsyncIOScheduler,
            )
            await scheduler.start()
            try:
                job = scheduler.create_recurring_job(
                    cron="55 12 * * *",
                    prompt="Lineup watch",
                    owner_session="alpha",
                    created_by="agent",
                )

                scheduler._handle_apscheduler_event(_FakeMissedEvent(job.id))
                await asyncio.sleep(0)

                self.assertEqual(fired, [job.id])
            finally:
                await scheduler.shutdown()

    async def test_old_cron_misfire_event_is_not_caught_up(self):
        fired: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            async def dispatch(job, _fired_at):
                fired.append(job.id)

            scheduler = Scheduler(
                dispatch_callback=dispatch,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
                apscheduler_factory=_FakeAsyncIOScheduler,
                catch_up_window_seconds=300,
            )
            await scheduler.start()
            try:
                job = scheduler.create_recurring_job(
                    cron="55 12 * * *",
                    prompt="Lineup watch",
                    owner_session="alpha",
                    created_by="agent",
                )

                old_run = _utc_now() - timedelta(seconds=1000)
                scheduler._handle_apscheduler_event(_FakeMissedEvent(job.id, old_run))
                await asyncio.sleep(0)

                self.assertEqual(fired, [])
            finally:
                await scheduler.shutdown()

    async def test_scheduled_jobs_use_catch_up_misfire_grace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
                apscheduler_factory=_FakeAsyncIOScheduler,
                catch_up_window_seconds=123,
            )
            await scheduler.start()
            try:
                recurring = scheduler.create_recurring_job(
                    cron="*/5 * * * *",
                    prompt="Recurring check",
                    owner_session="alpha",
                    created_by="agent",
                )
                absolute = scheduler.create_absolute_job(
                    when=(_utc_now() + timedelta(minutes=5)).astimezone(timezone.utc).isoformat(),
                    prompt="One shot",
                    owner_session="alpha",
                    created_by="agent",
                )

                self.assertEqual(
                    scheduler._scheduler.jobs[recurring.id]["kwargs"]["misfire_grace_time"],
                    123,
                )
                self.assertTrue(scheduler._scheduler.jobs[recurring.id]["kwargs"]["coalesce"])
                self.assertEqual(
                    scheduler._scheduler.jobs[absolute.id]["kwargs"]["misfire_grace_time"],
                    123,
                )
            finally:
                await scheduler.shutdown()

    async def test_failed_absolute_job_counts_as_run_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = Scheduler(
                dispatch_callback=lambda *_args: None,
                jobs_path=Path(tmpdir) / "scheduler" / "jobs.json",
            )
            await scheduler.start()
            try:
                created_at = _utc_now().replace(microsecond=0).isoformat()
                when = (_utc_now() + timedelta(minutes=1)).replace(microsecond=0)
                scheduler.schedule_job(
                    {
                        "id": "job-failed-attempt",
                        "type": "absolute",
                        "spec": {"at": when.isoformat()},
                        "prompt": "Check BTC",
                        "owner_session": "terminal",
                        "created_at": created_at,
                        "created_by": "user",
                    },
                    persist=False,
                )

                updated = scheduler.record_failure(
                    "job-failed-attempt",
                    fired_at=_utc_now(),
                    error="session missing",
                )

                self.assertEqual(updated.status, "failed")
                self.assertEqual(updated.run_count, 1)
                self.assertIsNotNone(updated.last_run)
                self.assertEqual(updated.last_result_preview, "session missing")
            finally:
                await scheduler.shutdown()


if __name__ == "__main__":
    unittest.main()

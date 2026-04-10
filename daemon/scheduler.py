"""Daemon-resident job scheduler primitives."""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from croniter import croniter

DispatchCallback = Callable[[dict[str, Any], datetime], Awaitable[None] | None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("absolute schedules require an ISO timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    when = datetime.fromisoformat(normalized)
    if when.tzinfo is None:
        raise ValueError("absolute schedules require a timezone-aware timestamp")
    return when


def _coerce_timezone(value: str | None) -> ZoneInfo:
    return ZoneInfo(value or "UTC")


class Scheduler:
    """Thin wrapper around APScheduler for daemon-managed jobs."""

    def __init__(
        self,
        *,
        dispatch_callback: DispatchCallback,
        timezone_name: str = "UTC",
        apscheduler_factory: Callable[..., AsyncIOScheduler] | None = None,
    ) -> None:
        self.dispatch_callback = dispatch_callback
        self.timezone_name = timezone_name
        self._apscheduler_factory = apscheduler_factory or AsyncIOScheduler
        self._scheduler = self._apscheduler_factory(timezone=_coerce_timezone(timezone_name))
        self._jobs: dict[str, dict[str, Any]] = {}
        self._started = False
        self.log = logging.getLogger(__name__)

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        self._scheduler.start()
        self._started = True

    async def shutdown(self) -> None:
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self._jobs[key] for key in sorted(self._jobs)]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)

    def schedule_job(self, job: dict[str, Any]) -> datetime | None:
        job_type = job.get("type")
        if job_type == "absolute":
            return self._schedule_absolute(job)
        if job_type == "cron":
            return self._schedule_cron(job)
        self._jobs[job["id"]] = dict(job)
        return None

    def remove_job(self, job_id: str) -> bool:
        existed = self._jobs.pop(job_id, None) is not None
        try:
            self._scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001
            return existed
        return True

    def pause_job(self, job_id: str) -> None:
        self._scheduler.pause_job(job_id)

    def resume_job(self, job_id: str) -> None:
        self._scheduler.resume_job(job_id)

    def next_run(self, job_id: str) -> datetime | None:
        scheduled = self._scheduler.get_job(job_id)
        if scheduled is None:
            return None
        return getattr(scheduled, "next_run_time", None)

    async def fire_event_job(self, job_id: str, payload: dict[str, Any]) -> None:
        job = self._jobs[job_id]
        await self._dispatch(job, fired_at=_utc_now(), payload=payload)

    async def _fire_scheduled_job(self, job_id: str) -> None:
        job = self._jobs[job_id]
        await self._dispatch(job, fired_at=_utc_now())

    async def _dispatch(
        self,
        job: dict[str, Any],
        *,
        fired_at: datetime,
        payload: dict[str, Any] | None = None,
    ) -> None:
        maybe_awaitable = self.dispatch_callback(job, fired_at)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable
        if payload is not None:
            self.log.debug("event job fired job_id=%s payload=%s", job.get("id"), payload)

    def _schedule_absolute(self, job: dict[str, Any]) -> datetime:
        when = _parse_datetime(job.get("spec", {}).get("at", ""))
        self._jobs[job["id"]] = dict(job)
        self._scheduler.add_job(
            self._fire_scheduled_job,
            trigger=DateTrigger(run_date=when),
            id=job["id"],
            replace_existing=True,
            args=[job["id"]],
        )
        return when

    def _schedule_cron(self, job: dict[str, Any]) -> datetime:
        spec = job.get("spec", {})
        cron = spec.get("cron")
        if not isinstance(cron, str) or not croniter.is_valid(cron):
            raise ValueError("recurring schedules require a valid cron expression")
        tz_name = spec.get("tz")
        timezone_info = _coerce_timezone(tz_name if isinstance(tz_name, str) else None)
        trigger = CronTrigger.from_crontab(cron, timezone=timezone_info)
        self._jobs[job["id"]] = dict(job)
        self._scheduler.add_job(
            self._fire_scheduled_job,
            trigger=trigger,
            id=job["id"],
            replace_existing=True,
            args=[job["id"]],
        )
        next_run = self.next_run(job["id"])
        if next_run is None:
            raise RuntimeError(f"cron job '{job['id']}' did not produce a next run")
        return next_run

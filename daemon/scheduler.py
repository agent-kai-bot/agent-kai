"""Daemon-resident job scheduler primitives."""

from __future__ import annotations

import inspect
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from croniter import croniter
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
JobType = Literal["absolute", "cron", "event"]
JobStatus = Literal["active", "paused", "completed", "failed", "cancelled"]
JobConcurrency = Literal["skip", "queue"]
DispatchCallback = Callable[["ScheduledJob", datetime], Awaitable[None] | None]

STRUCTURED_FILTER_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {
        "scalar": {
            "type": ["string", "number", "boolean"],
        },
        "scalar_array": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/scalar"},
        },
        "comparison": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 1,
            "additionalProperties": False,
            "properties": {
                "eq": {"$ref": "#/$defs/scalar"},
                "ne": {"$ref": "#/$defs/scalar"},
                "gt": {"type": "number"},
                "gte": {"type": "number"},
                "lt": {"type": "number"},
                "lte": {"type": "number"},
                "in": {"$ref": "#/$defs/scalar_array"},
                "contains": {"type": "string", "minLength": 1},
                "regex": {"type": "string", "minLength": 1},
            },
        },
        "filter_value": {
            "oneOf": [
                {"$ref": "#/$defs/scalar"},
                {"$ref": "#/$defs/scalar_array"},
                {"$ref": "#/$defs/comparison"},
            ]
        },
    },
    "type": "object",
    "minProperties": 1,
    "propertyNames": {"type": "string", "minLength": 1},
    "additionalProperties": {"$ref": "#/$defs/filter_value"},
}
STRUCTURED_FILTER_VALIDATOR = Draft202012Validator(STRUCTURED_FILTER_SCHEMA)


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


def validate_structured_filter(filter_spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(filter_spec, dict):
        raise ValueError("event schedules require a filter object")
    try:
        STRUCTURED_FILTER_VALIDATOR.validate(filter_spec)
    except JsonSchemaValidationError as exc:
        raise ValueError(f"invalid structured filter: {exc.message}") from exc
    return filter_spec


def matches_structured_filter(payload: dict[str, Any], filter_spec: dict[str, Any]) -> bool:
    for field, expected in filter_spec.items():
        actual = payload.get(field)
        if isinstance(expected, dict):
            operator, operand = next(iter(expected.items()))
            if operator == "eq" and actual != operand:
                return False
            if operator == "ne" and actual == operand:
                return False
            if operator == "gt" and not (isinstance(actual, (int, float)) and actual > operand):
                return False
            if operator == "gte" and not (isinstance(actual, (int, float)) and actual >= operand):
                return False
            if operator == "lt" and not (isinstance(actual, (int, float)) and actual < operand):
                return False
            if operator == "lte" and not (isinstance(actual, (int, float)) and actual <= operand):
                return False
            if operator == "in" and actual not in operand:
                return False
            if operator == "contains":
                haystack = actual if isinstance(actual, list) else str(actual or "")
                if operand not in haystack:
                    return False
            if operator == "regex":
                if not re.search(str(operand), str(actual or "")):
                    return False
            continue
        if isinstance(expected, list):
            if actual not in expected:
                return False
            continue
        if actual != expected:
            return False
    return True


class ScheduledJob(BaseModel):
    """Persisted scheduler job record."""

    model_config = ConfigDict(extra="forbid")

    id: NonEmptyString
    type: JobType
    spec: dict[str, Any]
    prompt: NonEmptyString
    owner_session: NonEmptyString
    created_at: NonEmptyString
    created_by: Literal["user", "agent"]
    last_run: str | None = None
    next_run: str | None = None
    run_count: int = 0
    max_runs: int | None = None
    status: JobStatus = "active"
    last_result_preview: str | None = None
    concurrency: JobConcurrency = "queue"
    tool_budget: int | None = Field(default=None)

    @model_validator(mode="after")
    def validate_job(self) -> "ScheduledJob":
        if self.run_count < 0:
            raise ValueError("run_count must be >= 0")
        if self.max_runs is not None and self.max_runs < 1:
            raise ValueError("max_runs must be >= 1")
        if self.tool_budget is not None and self.tool_budget < 1:
            raise ValueError("tool_budget must be >= 1")
        _parse_datetime(self.created_at)
        if self.last_run is not None:
            _parse_datetime(self.last_run)
        if self.next_run is not None:
            _parse_datetime(self.next_run)
        if self.type == "absolute":
            if set(self.spec) != {"at"}:
                raise ValueError("absolute jobs require spec={'at': ISO_TIMESTAMP}")
            _parse_datetime(str(self.spec["at"]))
        elif self.type == "cron":
            allowed = {"cron", "tz"}
            if not set(self.spec).issubset(allowed) or "cron" not in self.spec:
                raise ValueError("cron jobs require spec={'cron': ..., 'tz': ...?}")
            cron = self.spec.get("cron")
            if not isinstance(cron, str) or not croniter.is_valid(cron):
                raise ValueError("recurring schedules require a valid cron expression")
            tz_name = self.spec.get("tz")
            if tz_name is not None and not isinstance(tz_name, str):
                raise ValueError("cron schedules require a string timezone name")
            _coerce_timezone(tz_name if isinstance(tz_name, str) else None)
        else:
            if set(self.spec) != {"channel", "filter"}:
                raise ValueError("event jobs require spec={'channel': ..., 'filter': {...}}")
            channel = self.spec.get("channel")
            if not isinstance(channel, str) or not channel.strip():
                raise ValueError("event schedules require a non-empty channel")
            validate_structured_filter(self.spec.get("filter"))
        return self


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
        self._jobs: dict[str, ScheduledJob] = {}
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

    def list_jobs(self) -> list[ScheduledJob]:
        return [self._jobs[key] for key in sorted(self._jobs)]

    def get_job(self, job_id: str) -> ScheduledJob | None:
        return self._jobs.get(job_id)

    def schedule_job(self, job: ScheduledJob | dict[str, Any]) -> datetime | None:
        scheduled_job = job if isinstance(job, ScheduledJob) else ScheduledJob.model_validate(job)
        job_type = scheduled_job.type
        if job_type == "absolute":
            return self._schedule_absolute(scheduled_job)
        if job_type == "cron":
            return self._schedule_cron(scheduled_job)
        self._jobs[scheduled_job.id] = scheduled_job
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

    async def _dispatch(self, job: ScheduledJob, *, fired_at: datetime, payload: dict[str, Any] | None = None) -> None:
        maybe_awaitable = self.dispatch_callback(job, fired_at)
        if inspect.isawaitable(maybe_awaitable):
            await maybe_awaitable
        if payload is not None:
            self.log.debug("event job fired job_id=%s payload=%s", job.id, payload)

    def _schedule_absolute(self, job: ScheduledJob) -> datetime:
        when = _parse_datetime(job.spec.get("at", ""))
        self._jobs[job.id] = job
        self._scheduler.add_job(
            self._fire_scheduled_job,
            trigger=DateTrigger(run_date=when),
            id=job.id,
            replace_existing=True,
            args=[job.id],
        )
        return when

    def _schedule_cron(self, job: ScheduledJob) -> datetime:
        cron = job.spec.get("cron")
        tz_name = job.spec.get("tz")
        timezone_info = _coerce_timezone(tz_name if isinstance(tz_name, str) else None)
        trigger = CronTrigger.from_crontab(str(cron), timezone=timezone_info)
        self._jobs[job.id] = job
        self._scheduler.add_job(
            self._fire_scheduled_job,
            trigger=trigger,
            id=job.id,
            replace_existing=True,
            args=[job.id],
        )
        next_run = self.next_run(job.id)
        if next_run is None:
            raise RuntimeError(f"cron job '{job.id}' did not produce a next run")
        return next_run

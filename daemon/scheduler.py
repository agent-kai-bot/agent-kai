"""Daemon-resident job scheduler primitives."""

from __future__ import annotations

import fcntl
import inspect
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from croniter import croniter
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from config import WORKSPACES_DIR

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
JobType = Literal["absolute", "cron", "event"]
JobStatus = Literal["active", "paused", "completed", "failed", "cancelled"]
JobConcurrency = Literal["skip", "queue"]
DispatchCallback = Callable[["ScheduledJob", datetime], Awaitable[None] | None]
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]
SCHEDULER_ROOT_DIR = Path(WORKSPACES_DIR) / "scheduler"
SCHEDULER_JOBS_PATH = SCHEDULER_ROOT_DIR / "jobs.json"

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


def _truncate_preview(text: str | None, limit: int = 160) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."


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


@contextmanager
def _json_file_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield handle
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_dict_unlocked(path: Path, payload: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


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


class DaemonEventBus:
    """Small async pub/sub bus for daemon-scoped events."""

    def __init__(self) -> None:
        self._subscribers: list[EventCallback] = []

    def subscribe(self, callback: EventCallback) -> EventCallback:
        self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback: EventCallback) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        for callback in list(self._subscribers):
            maybe_awaitable = callback(channel, dict(payload))
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable


class Scheduler:
    """Thin wrapper around APScheduler for daemon-managed jobs."""

    def __init__(
        self,
        *,
        dispatch_callback: DispatchCallback,
        timezone_name: str = "UTC",
        apscheduler_factory: Callable[..., AsyncIOScheduler] | None = None,
        jobs_path: Path = SCHEDULER_JOBS_PATH,
        event_bus: DaemonEventBus | None = None,
    ) -> None:
        self.dispatch_callback = dispatch_callback
        self.timezone_name = timezone_name
        self._apscheduler_factory = apscheduler_factory or AsyncIOScheduler
        self._scheduler = self._apscheduler_factory(timezone=_coerce_timezone(timezone_name))
        self.jobs_path = jobs_path
        self.event_bus = event_bus
        self._jobs: dict[str, ScheduledJob] = {}
        self._started = False
        self._event_callback: EventCallback | None = None
        self.log = logging.getLogger(__name__)

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        self.load_jobs()
        self._scheduler.start()
        self._register_loaded_jobs()
        if self.event_bus is not None and self._event_callback is None:
            self._event_callback = self.event_bus.subscribe(self.handle_event)
        self._started = True

    async def shutdown(self) -> None:
        if not self._started:
            return
        if self.event_bus is not None and self._event_callback is not None:
            self.event_bus.unsubscribe(self._event_callback)
            self._event_callback = None
        self._scheduler.shutdown(wait=False)
        self._started = False

    def list_jobs(self) -> list[ScheduledJob]:
        return [self._jobs[key] for key in sorted(self._jobs)]

    def get_job(self, job_id: str) -> ScheduledJob | None:
        return self._jobs.get(job_id)

    def load_jobs(self) -> list[ScheduledJob]:
        payload = _read_json_dict(self.jobs_path)
        raw_jobs = payload.get("jobs")
        jobs: list[ScheduledJob] = []
        if not isinstance(raw_jobs, dict):
            self._jobs = {}
            return jobs
        loaded: dict[str, ScheduledJob] = {}
        for job_id, raw_job in raw_jobs.items():
            if not isinstance(job_id, str) or not isinstance(raw_job, dict):
                continue
            try:
                job = ScheduledJob.model_validate(raw_job)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("dropping invalid persisted job %s: %s", job_id, exc)
                continue
            loaded[job.id] = job
            jobs.append(job)
        self._jobs = loaded
        return jobs

    def schedule_job(
        self,
        job: ScheduledJob | dict[str, Any],
        *,
        persist: bool = True,
    ) -> datetime | None:
        scheduled_job = job if isinstance(job, ScheduledJob) else ScheduledJob.model_validate(job)
        job_type = scheduled_job.type
        if job_type == "absolute":
            next_run = self._schedule_absolute(scheduled_job)
        elif job_type == "cron":
            next_run = self._schedule_cron(scheduled_job)
        else:
            self._jobs[scheduled_job.id] = scheduled_job
            next_run = None
        if persist:
            self._persist_jobs()
        return next_run

    def remove_job(self, job_id: str) -> bool:
        existed = self._jobs.pop(job_id, None) is not None
        try:
            self._scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001
            self._persist_jobs()
            return existed
        self._persist_jobs()
        return True

    def pause_job(self, job_id: str) -> None:
        self._scheduler.pause_job(job_id)
        job = self._jobs[job_id]
        self._jobs[job_id] = job.model_copy(update={"status": "paused", "next_run": None})
        self._persist_jobs()

    def resume_job(self, job_id: str) -> None:
        self._scheduler.resume_job(job_id)
        next_run = self.next_run(job_id)
        job = self._jobs[job_id]
        self._jobs[job_id] = job.model_copy(
            update={
                "status": "active",
                "next_run": next_run.isoformat() if next_run is not None else None,
            }
        )
        self._persist_jobs()

    def next_run(self, job_id: str) -> datetime | None:
        scheduled = self._scheduler.get_job(job_id)
        if scheduled is None:
            return None
        return getattr(scheduled, "next_run_time", None)

    async def fire_event_job(self, job_id: str, payload: dict[str, Any]) -> None:
        job = self._jobs[job_id]
        await self._dispatch(job, fired_at=_utc_now(), payload=payload)

    async def handle_event(self, channel: str, payload: dict[str, Any]) -> None:
        for job in list(self._jobs.values()):
            if job.type != "event" or job.status != "active":
                continue
            job_channel = str(job.spec.get("channel") or "")
            if job_channel != channel:
                continue
            filter_spec = job.spec.get("filter")
            if not isinstance(filter_spec, dict):
                continue
            if not matches_structured_filter(payload, filter_spec):
                continue
            await self.fire_event_job(job.id, payload)

    def update_job(self, job_id: str, **updates: Any) -> ScheduledJob:
        job = self._jobs[job_id]
        updated = job.model_copy(update=updates)
        self._jobs[job_id] = updated
        self._persist_jobs()
        return updated

    def record_completion(
        self,
        job_id: str,
        *,
        fired_at: datetime,
        result_preview: str | None = None,
    ) -> ScheduledJob:
        job = self._jobs[job_id]
        run_count = job.run_count + 1
        next_run = self.next_run(job_id)
        next_run_iso = next_run.isoformat() if next_run is not None else None
        status: JobStatus = "active"
        if job.type in {"absolute", "event"}:
            next_run_iso = None
        if job.type == "absolute":
            status = "completed"
        if job.max_runs is not None and run_count >= job.max_runs:
            status = "completed"
            next_run_iso = None
            with suppress(Exception):
                self._scheduler.remove_job(job_id)
        return self.update_job(
            job_id,
            last_run=fired_at.isoformat(),
            last_result_preview=_truncate_preview(result_preview),
            next_run=next_run_iso,
            run_count=run_count,
            status=status,
        )

    def record_failure(
        self,
        job_id: str,
        *,
        fired_at: datetime,
        error: str,
    ) -> ScheduledJob:
        job = self._jobs[job_id]
        if job.type == "absolute":
            with suppress(Exception):
                self._scheduler.remove_job(job_id)
        return self.update_job(
            job_id,
            last_run=fired_at.isoformat(),
            last_result_preview=_truncate_preview(error),
            next_run=None if job.type in {"absolute", "event"} else job.next_run,
            status="failed",
        )

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
        self._jobs[job.id] = job.model_copy(update={"next_run": when.isoformat(), "status": "active"})
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
        self._jobs[job.id] = job.model_copy(update={"status": "active"})
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
        self._jobs[job.id] = self._jobs[job.id].model_copy(update={"next_run": next_run.isoformat()})
        return next_run

    def _persist_jobs(self) -> None:
        self.jobs_path.parent.mkdir(parents=True, exist_ok=True)
        with _json_file_lock(self.jobs_path):
            payload = _read_json_dict(self.jobs_path)
            existing = payload.get("jobs")
            jobs_payload = existing if isinstance(existing, dict) else {}
            current_jobs = {
                job_id: job.model_dump(mode="json")
                for job_id, job in self._jobs.items()
            }
            jobs_payload.update(current_jobs)
            for job_id in list(jobs_payload):
                if job_id not in current_jobs:
                    jobs_payload.pop(job_id, None)
            payload["version"] = 1
            payload["jobs"] = jobs_payload
            _write_json_dict_unlocked(self.jobs_path, payload)

    def _register_loaded_jobs(self) -> None:
        for job in list(self._jobs.values()):
            if job.status != "active":
                continue
            if job.type in {"absolute", "cron"}:
                self.schedule_job(job, persist=False)

"""Daemon-resident job scheduler primitives."""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import json
import logging
import os
import re
import tempfile
import uuid
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

from config import WORKSPACES_DIR, normalize_reasoning_effort

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
JobType = Literal["absolute", "cron", "event"]
JobStatus = Literal["active", "paused", "completed", "failed", "cancelled"]
JobConcurrency = Literal["skip", "queue"]
DispatchCallback = Callable[["ScheduledJob", datetime], Awaitable[None] | None]
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]
SchedulerEventCallback = Callable[..., Awaitable[None] | None]
SCHEDULER_ROOT_DIR = Path(WORKSPACES_DIR) / "scheduler"
SCHEDULER_JOBS_PATH = SCHEDULER_ROOT_DIR / "jobs.json"
SCHEDULER_V2_ROUTING_KEY = "v2_routing"
SCHEDULER_V2_ROUTING_FIELDS = frozenset(
    {"target_agent_role", "reasoning_effort", "thinking_level", "extra_env"}
)

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
    target_agent_role: NonEmptyString | None = None
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    extra_env: dict[str, str] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_routing_overrides(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        normalized = dict(values)
        canonical_values: dict[str, str] = {}
        for field_name in ("reasoning_effort", "thinking_level"):
            raw_value = normalized.get(field_name)
            if raw_value is None:
                continue
            canonical = normalize_reasoning_effort(raw_value)
            if canonical is None:
                raise ValueError(f"invalid {field_name}: {raw_value!r}")
            normalized[field_name] = canonical
            canonical_values[field_name] = canonical
        if (
            canonical_values.get("reasoning_effort") is not None
            and canonical_values.get("thinking_level") is not None
            and canonical_values["reasoning_effort"] != canonical_values["thinking_level"]
        ):
            raise ValueError("reasoning_effort and thinking_level must match when both are set")
        extra_env = normalized.get("extra_env")
        if extra_env is not None and not isinstance(extra_env, dict):
            raise ValueError("extra_env must be an object")
        return normalized

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

    @property
    def effective_reasoning_effort(self) -> str | None:
        return self.reasoning_effort or self.thinking_level

    def routing_overrides(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for field_name in sorted(SCHEDULER_V2_ROUTING_FIELDS):
            value = getattr(self, field_name)
            if value is None:
                continue
            if field_name == "extra_env" and not value:
                continue
            payload[field_name] = value
        return payload

    def has_routing_overrides(self) -> bool:
        return bool(self.routing_overrides())


class ScheduledJobRoutingOverride(BaseModel):
    """Rollback-safe v2 routing sidecar entry for one scheduled job."""

    model_config = ConfigDict(extra="forbid")

    target_agent_role: NonEmptyString | None = None
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    extra_env: dict[str, str] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_overrides(cls, values: Any) -> Any:
        return ScheduledJob.normalize_routing_overrides(values)

    @property
    def effective_reasoning_effort(self) -> str | None:
        return self.reasoning_effort or self.thinking_level

    def routing_overrides(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for field_name in sorted(SCHEDULER_V2_ROUTING_FIELDS):
            value = getattr(self, field_name)
            if value is None:
                continue
            if field_name == "extra_env" and not value:
                continue
            payload[field_name] = value
        return payload


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
        event_callback: SchedulerEventCallback | None = None,
        session_job_limit: int = 50,
        catch_up_window_seconds: int = 300,
    ) -> None:
        self.dispatch_callback = dispatch_callback
        self.timezone_name = timezone_name
        self._apscheduler_factory = apscheduler_factory or AsyncIOScheduler
        self._scheduler = self._apscheduler_factory(timezone=_coerce_timezone(timezone_name))
        self.jobs_path = jobs_path
        self.event_bus = event_bus
        self.event_callback = event_callback
        self.session_job_limit = session_job_limit
        self.catch_up_window_seconds = catch_up_window_seconds
        self._jobs: dict[str, ScheduledJob] = {}
        self._started = False
        self._event_callback: EventCallback | None = None
        self._startup_tasks: set[asyncio.Task] = set()
        self.log = logging.getLogger(__name__)

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        self.load_jobs()
        self._scheduler.start()
        if self.event_bus is not None and self._event_callback is None:
            self._event_callback = self.event_bus.subscribe(self.handle_event)
        self._recover_loaded_jobs()
        self._started = True

    async def shutdown(self) -> None:
        if not self._started:
            return
        if self.event_bus is not None and self._event_callback is not None:
            self.event_bus.unsubscribe(self._event_callback)
            self._event_callback = None
        for task in list(self._startup_tasks):
            task.cancel()
        self._startup_tasks.clear()
        self._scheduler.shutdown(wait=False)
        self._started = False

    def list_jobs(self) -> list[ScheduledJob]:
        return [self._jobs[key] for key in sorted(self._jobs)]

    def list_jobs_for_session(self, session_name: str | None = None) -> list[ScheduledJob]:
        jobs = self.list_jobs()
        if session_name is None:
            return jobs
        return [job for job in jobs if job.owner_session == session_name]

    def active_job_count(self, session_name: str) -> int:
        return sum(
            1
            for job in self._jobs.values()
            if job.owner_session == session_name and job.status == "active"
        )

    def get_job(self, job_id: str) -> ScheduledJob | None:
        return self._jobs.get(job_id)

    def create_absolute_job(
        self,
        *,
        when: str,
        prompt: str,
        owner_session: str,
        created_by: Literal["user", "agent"],
        max_runs: int | None = 1,
        concurrency: JobConcurrency = "queue",
        tool_budget: int | None = None,
        target_agent_role: str | None = None,
        reasoning_effort: str | None = None,
        thinking_level: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> ScheduledJob:
        self._enforce_session_job_limit(owner_session)
        job = ScheduledJob.model_validate(
            {
                "id": self._next_job_id(),
                "type": "absolute",
                "spec": {"at": when},
                "prompt": prompt,
                "owner_session": owner_session,
                "created_at": _utc_now().replace(microsecond=0).isoformat(),
                "created_by": created_by,
                "max_runs": max_runs,
                "concurrency": concurrency,
                "tool_budget": tool_budget,
                **self._routing_create_payload(
                    target_agent_role=target_agent_role,
                    reasoning_effort=reasoning_effort,
                    thinking_level=thinking_level,
                    extra_env=extra_env,
                ),
            }
        )
        self.schedule_job(job)
        self._emit_event("created", self._jobs[job.id])
        return self._jobs[job.id]

    def create_recurring_job(
        self,
        *,
        cron: str,
        prompt: str,
        owner_session: str,
        created_by: Literal["user", "agent"],
        max_runs: int | None = None,
        concurrency: JobConcurrency = "queue",
        tool_budget: int | None = None,
        timezone_name: str | None = None,
        target_agent_role: str | None = None,
        reasoning_effort: str | None = None,
        thinking_level: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> ScheduledJob:
        self._enforce_session_job_limit(owner_session)
        job = ScheduledJob.model_validate(
            {
                "id": self._next_job_id(),
                "type": "cron",
                "spec": {"cron": cron, "tz": timezone_name or self.timezone_name},
                "prompt": prompt,
                "owner_session": owner_session,
                "created_at": _utc_now().replace(microsecond=0).isoformat(),
                "created_by": created_by,
                "max_runs": max_runs,
                "concurrency": concurrency,
                "tool_budget": tool_budget,
                **self._routing_create_payload(
                    target_agent_role=target_agent_role,
                    reasoning_effort=reasoning_effort,
                    thinking_level=thinking_level,
                    extra_env=extra_env,
                ),
            }
        )
        self.schedule_job(job)
        self._emit_event("created", self._jobs[job.id])
        return self._jobs[job.id]

    def create_event_job(
        self,
        *,
        condition: dict[str, Any],
        prompt: str,
        owner_session: str,
        created_by: Literal["user", "agent"],
        max_runs: int | None = None,
        concurrency: JobConcurrency = "queue",
        tool_budget: int | None = None,
        target_agent_role: str | None = None,
        reasoning_effort: str | None = None,
        thinking_level: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> ScheduledJob:
        self._enforce_session_job_limit(owner_session)
        spec = {
            "channel": condition.get("channel"),
            "filter": condition.get("filter"),
        }
        job = ScheduledJob.model_validate(
            {
                "id": self._next_job_id(),
                "type": "event",
                "spec": spec,
                "prompt": prompt,
                "owner_session": owner_session,
                "created_at": _utc_now().replace(microsecond=0).isoformat(),
                "created_by": created_by,
                "max_runs": max_runs,
                "concurrency": concurrency,
                "tool_budget": tool_budget,
                **self._routing_create_payload(
                    target_agent_role=target_agent_role,
                    reasoning_effort=reasoning_effort,
                    thinking_level=thinking_level,
                    extra_env=extra_env,
                ),
            }
        )
        self.schedule_job(job)
        self._emit_event("created", self._jobs[job.id])
        return self._jobs[job.id]

    def load_jobs(self) -> list[ScheduledJob]:
        payload = _read_json_dict(self.jobs_path)
        raw_jobs = payload.get("jobs")
        jobs: list[ScheduledJob] = []
        if not isinstance(raw_jobs, dict):
            self._jobs = {}
            return jobs
        raw_routing = payload.get(SCHEDULER_V2_ROUTING_KEY, {})
        if raw_routing is None:
            raw_routing = {}
        if not isinstance(raw_routing, dict):
            self.log.warning(
                "dropping invalid scheduler %s sidecar: expected object",
                SCHEDULER_V2_ROUTING_KEY,
            )
            raw_routing = {}
        loaded: dict[str, ScheduledJob] = {}
        for job_id, raw_job in raw_jobs.items():
            if not isinstance(job_id, str) or not isinstance(raw_job, dict):
                continue
            merged_job = dict(raw_job)
            raw_override = raw_routing.get(job_id)
            if raw_override is not None:
                if not isinstance(raw_override, dict):
                    self.log.warning(
                        "dropping invalid scheduler routing sidecar for job %s: expected object",
                        job_id,
                    )
                else:
                    try:
                        override = ScheduledJobRoutingOverride.model_validate(raw_override)
                    except Exception as exc:  # noqa: BLE001
                        self.log.warning(
                            "dropping invalid scheduler routing sidecar for job %s: %s",
                            job_id,
                            exc,
                        )
                    else:
                        merged_job.update(override.routing_overrides())
            try:
                job = ScheduledJob.model_validate(merged_job)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("dropping invalid persisted job %s: %s", job_id, exc)
                continue
            loaded[job.id] = job
            jobs.append(job)
        for job_id in sorted(raw_routing):
            if job_id not in raw_jobs:
                self.log.warning(
                    "skipping scheduler routing sidecar for unknown job %s",
                    job_id,
                )
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

    def cancel_job(self, job_id: str) -> ScheduledJob:
        with suppress(Exception):
            self._scheduler.remove_job(job_id)
        updated = self.update_job(job_id, status="cancelled", next_run=None)
        self._emit_event("cancelled", updated)
        return updated

    def pause_all_jobs(self, session_name: str | None = None) -> list[ScheduledJob]:
        paused: list[ScheduledJob] = []
        for job in list(self._jobs.values()):
            if session_name is not None and job.owner_session != session_name:
                continue
            if job.status != "active":
                continue
            paused.append(self.pause_job(job.id))
        return paused

    def pause_job(self, job_id: str) -> ScheduledJob:
        job = self._jobs[job_id]
        if job.type in {"absolute", "cron"}:
            self._scheduler.pause_job(job_id)
        updated = self.update_job(job_id, status="paused", next_run=None)
        self._emit_event("paused", updated)
        return updated

    def resume_job(self, job_id: str) -> ScheduledJob:
        job = self._jobs[job_id]
        if job.type in {"absolute", "cron"}:
            self._scheduler.resume_job(job_id)
            next_run = self.next_run(job_id)
            updated = self.update_job(
                job_id,
                status="active",
                next_run=next_run.isoformat() if next_run is not None else None,
            )
        else:
            updated = self.update_job(job_id, status="active")
        self._emit_event("resumed", updated)
        return updated

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
        updated = self.update_job(
            job_id,
            last_run=fired_at.isoformat(),
            last_result_preview=_truncate_preview(result_preview),
            next_run=next_run_iso,
            run_count=run_count,
            status=status,
        )
        self._emit_event("completed", updated, result_preview=updated.last_result_preview)
        return updated

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
        updated = self.update_job(
            job_id,
            last_run=fired_at.isoformat(),
            last_result_preview=_truncate_preview(error),
            next_run=None if job.type in {"absolute", "event"} else job.next_run,
            status="failed",
        )
        self._emit_event("failed", updated, error=error)
        return updated

    def notify_triggered(self, job_id: str, *, fired_at: datetime) -> None:
        job = self._jobs[job_id]
        self._emit_event("triggered", job, fired_at=fired_at.isoformat())

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
                job_id: job.model_dump(mode="json", exclude=SCHEDULER_V2_ROUTING_FIELDS)
                for job_id, job in self._jobs.items()
            }
            jobs_payload.update(current_jobs)
            for job_id in list(jobs_payload):
                if job_id not in current_jobs:
                    jobs_payload.pop(job_id, None)
            existing_routing = payload.get(SCHEDULER_V2_ROUTING_KEY)
            routing_payload = existing_routing if isinstance(existing_routing, dict) else {}
            current_routing = {
                job_id: routing
                for job_id, job in self._jobs.items()
                if (routing := job.routing_overrides())
            }
            routing_payload.update(current_routing)
            for job_id in list(routing_payload):
                if job_id not in current_jobs or job_id not in current_routing:
                    routing_payload.pop(job_id, None)
            payload["version"] = 1
            payload["jobs"] = jobs_payload
            if routing_payload:
                payload[SCHEDULER_V2_ROUTING_KEY] = routing_payload
            else:
                payload.pop(SCHEDULER_V2_ROUTING_KEY, None)
            _write_json_dict_unlocked(self.jobs_path, payload)

    @staticmethod
    def _routing_create_payload(
        *,
        target_agent_role: str | None = None,
        reasoning_effort: str | None = None,
        thinking_level: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if target_agent_role is not None:
            payload["target_agent_role"] = target_agent_role
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if thinking_level is not None:
            payload["thinking_level"] = thinking_level
        if extra_env is not None:
            payload["extra_env"] = extra_env
        return payload

    def _recover_loaded_jobs(self) -> None:
        now = _utc_now()
        for job in list(self._jobs.values()):
            if job.status != "active":
                continue
            if job.type == "event":
                continue

            persisted_next_run = None
            if job.next_run:
                persisted_next_run = _parse_datetime(job.next_run)

            if job.type == "absolute":
                run_at = persisted_next_run or _parse_datetime(str(job.spec.get("at", "")))
                if run_at <= now:
                    age = (now - run_at).total_seconds()
                    if age <= self.catch_up_window_seconds:
                        self._schedule_catch_up(job.id)
                    else:
                        self.update_job(job.id, status="completed", next_run=None)
                    continue
                self.schedule_job(job, persist=False)
                continue

            self.schedule_job(job, persist=False)
            if persisted_next_run is None or persisted_next_run > now:
                continue
            age = (now - persisted_next_run).total_seconds()
            if age <= self.catch_up_window_seconds:
                self._schedule_catch_up(job.id)

    def _schedule_catch_up(self, job_id: str) -> None:
        task = asyncio.create_task(self._fire_scheduled_job(job_id))
        self._startup_tasks.add(task)
        task.add_done_callback(self._startup_tasks.discard)

    @staticmethod
    def _next_job_id() -> str:
        stamp = _utc_now().strftime("%Y_%m_%d_%H%M%S")
        return f"job_{stamp}_{uuid.uuid4().hex[:6]}"

    def _enforce_session_job_limit(self, owner_session: str) -> None:
        if self.active_job_count(owner_session) >= self.session_job_limit:
            raise ValueError(
                f"session '{owner_session}' already has {self.session_job_limit} active scheduled jobs"
            )

    def _emit_event(self, event_type: str, job: ScheduledJob, **payload: Any) -> None:
        if self.event_callback is None:
            return
        maybe_awaitable = self.event_callback(event_type, job=job, **payload)
        if inspect.isawaitable(maybe_awaitable):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.create_task(maybe_awaitable)

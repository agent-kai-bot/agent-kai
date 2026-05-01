"""Taskboard webhook dispatcher for daemon-hosted auto-fire sessions."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from agent.prompt_renderer import render_taskboard_fire_prompt
from agent.taskboard_status_router import roles_to_fire

LOGGER = logging.getLogger(__name__)

BACKPRESSURE_SUBJECT = "ops.alerts.taskboard_dispatcher_backpressure"
SPAWN_FAILED_SUBJECT = "ops.alerts.taskboard_dispatcher.spawn_failed"
DISPATCHER_SOURCE = "taskboard_dispatcher"
ACTIVE_SESSION_STATUSES = ("accepted", "spawning", "starting", "running")
AUDIT_PENDING_STATUSES = ("spawned", "spawn_failed", "stuck_aborted")
DEFAULT_MAX_CONCURRENT_SPAWNS = 6
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_SWEEP_INTERVAL_SECONDS = 60.0
DEFAULT_STUCK_AFTER_SECONDS = 60 * 60
SECRET_ENV_VARS = (
    "TASKBOARD_BEARER_TOKEN",
    "TASKBOARD_SESSION_TOKEN",
    "OPENCLAW_GATEWAY_TOKEN",
    "OPENCLAW_TOKEN",
)


class TaskboardTaskClient(Protocol):
    """Protocol for fetching taskboard state and posting audit comments.

    Example:
        Test clients can implement ``fetch_task`` and
        ``post_audit_comment`` with an in-memory dictionary and pass
        themselves to :class:`TaskboardDispatcher`.
    """

    def fetch_task(self, task_id: int) -> dict[str, Any] | Awaitable[dict[str, Any]]:
        """Fetch the latest task state.

        Args:
            task_id: Taskboard task id.

        Returns:
            Latest task payload as a dictionary.
        """

    def post_audit_comment(
        self,
        task_id: int,
        content: str,
    ) -> Any | Awaitable[Any]:
        """Post a dispatcher audit comment on a task.

        Args:
            task_id: Taskboard task id.
            content: Comment body to post.

        Returns:
            Optional taskboard client result.
        """


class TaskboardSessionManager(Protocol):
    """Protocol for spawning and aborting taskboard sessions.

    Example:
        A test double can record ``spawn`` keyword arguments and return a
        deterministic session id.
    """

    def spawn(self, **kwargs: Any) -> str | dict[str, Any] | Awaitable[str | dict[str, Any]]:
        """Spawn a taskboard session.

        Args:
            **kwargs: Spawn metadata including role, model, profile, prompt,
                task payload, task id, and fire generation.

        Returns:
            Session id or a dictionary containing one.
        """

    def abort(self, session_id: str) -> Any | Awaitable[Any]:
        """Abort an active taskboard session.

        Args:
            session_id: Session identifier returned by ``spawn``.

        Returns:
            Optional abort result from the implementation.
        """


@dataclass(frozen=True)
class TaskboardRoleRoute:
    """Resolved auto-fire role and model tier.

    Attributes:
        role: Canonical taskboard display role.
        agent_id: Local taskboard gateway agent id.
        model: Model family selected by policy.
        profile: Reasoning/profile tier selected by policy.

    Example:
        ``resolve_taskboard_role("Developer").profile`` is ``"xhigh"``.
    """

    role: str
    agent_id: str
    model: str
    profile: str


@dataclass(frozen=True)
class PendingWebhookRow:
    """One pending taskboard webhook queue row.

    Attributes:
        row_id: Stable row identifier used for updates.
        payload: Parsed webhook payload.
        received_at: Raw received timestamp if present.
        dispatch_status: Current dispatcher status for this row.
        session_id: Session id recorded on the queue row, if present.
        last_error: Last dispatcher error recorded on the queue row, if present.
        audit_posted_at: Timestamp proving the taskboard audit comment posted.
    """

    row_id: Any
    payload: dict[str, Any]
    received_at: str | None = None
    dispatch_status: str | None = None
    session_id: str | None = None
    last_error: str | None = None
    audit_posted_at: str | None = None


@dataclass(frozen=True)
class StuckSession:
    """A dispatcher-originated session that exceeded the stuck threshold.

    Attributes:
        session_id: Session identifier to abort.
        webhook_pending_id: Optional queue row id linked to the session.
        task_id: Taskboard task id linked to the session, if known.
        fire_generation: Taskboard fire generation linked to the session.
        agent_id: Dispatcher agent id linked to the session.
    """

    session_id: str
    webhook_pending_id: str | None
    task_id: int | None
    fire_generation: int | None
    agent_id: str | None


@dataclass(frozen=True)
class _QueueSchema:
    table: str
    id_column: str
    payload_column: str
    processed_column: str | None
    status_column: str | None
    received_column: str | None
    session_column: str | None
    error_column: str | None
    audit_posted_column: str | None


ROLE_ROUTES: dict[str, TaskboardRoleRoute] = {
    "architect": TaskboardRoleRoute(
        role="Architect",
        agent_id="architect",
        model="codex",
        profile="xhigh",
    ),
    "developer": TaskboardRoleRoute(
        role="Developer",
        agent_id="developer",
        model="codex",
        profile="xhigh",
    ),
    "code reviewer": TaskboardRoleRoute(
        role="Code Reviewer",
        agent_id="code-reviewer",
        model="claude",
        profile="high",
    ),
    "security auditor": TaskboardRoleRoute(
        role="Security Auditor",
        agent_id="security-auditor",
        model="claude",
        profile="high",
    ),
    "qa agent": TaskboardRoleRoute(
        role="QA Agent",
        agent_id="qa-agent",
        model="claude",
        profile="high",
    ),
}


def resolve_taskboard_role(role: str) -> TaskboardRoleRoute:
    """Resolve a taskboard role into an agent id and model tier.

    Args:
        role: Taskboard ``task.agent`` value.

    Returns:
        Resolved role route with agent id, model family, and profile tier.

    Raises:
        ValueError: If the role is empty or unknown.

    Example:
        ``resolve_taskboard_role("Code Reviewer").model`` returns
        ``"claude"``.
    """

    normalized = _normalize_role(role)
    route = ROLE_ROUTES.get(normalized)
    if route is None:
        raise ValueError(f"unknown taskboard role: {role}")
    return route


class DefaultTaskboardTaskClient:
    """Taskboard HTTP client adapter used by the live daemon dispatcher.

    Args:
        base_url: Optional taskboard base URL. Defaults to ``TASKBOARD_URL``.
        bearer_token: Optional bearer token. Defaults to taskboard token
            environment variables already used by KAI taskboard tools.
        timeout_seconds: Per-request HTTP timeout.

    Example:
        ``DefaultTaskboardTaskClient().fetch_task(123)`` returns a task dict
        when the configured taskboard is reachable.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        bearer_token: str | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        self.base_url = base_url or os.getenv("TASKBOARD_URL", "http://localhost:8080")
        self.bearer_token = (
            bearer_token
            if bearer_token is not None
            else (
                os.getenv("TASKBOARD_BEARER_TOKEN", "").strip()
                or os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
                or os.getenv("OPENCLAW_TOKEN", "").strip()
            )
        )
        self.timeout_seconds = timeout_seconds

    def fetch_task(self, task_id: int) -> dict[str, Any]:
        """Fetch and normalize one taskboard task.

        Args:
            task_id: Taskboard task id.

        Returns:
            Latest task payload.

        Raises:
            RuntimeError: If the taskboard client reports a failed request.
            ValueError: If the response cannot be interpreted as a task.
        """

        from agent.taskboard_tools import TaskboardClient, TaskboardContext

        client = TaskboardClient(
            TaskboardContext(
                base_url=self.base_url,
                bearer_token=self.bearer_token,
            ),
            timeout_seconds=self.timeout_seconds,
        )
        raw = client.get_task(task_id)
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("taskboard response was not JSON") from exc
        if not envelope.get("ok"):
            raise RuntimeError(str(envelope.get("error") or envelope.get("body")))
        return _extract_task(envelope.get("body"))

    def post_audit_comment(self, task_id: int, content: str) -> dict[str, Any]:
        """Post one dispatcher audit comment to the taskboard.

        Args:
            task_id: Taskboard task id.
            content: Audit comment body.

        Returns:
            Parsed taskboard response envelope.

        Raises:
            RuntimeError: If the taskboard reports a failed response.
            ValueError: If the response cannot be interpreted as JSON.
        """

        from agent.taskboard_tools import TaskboardClient, TaskboardContext

        client = TaskboardClient(
            TaskboardContext(
                base_url=self.base_url,
                bearer_token=self.bearer_token,
            ),
            timeout_seconds=self.timeout_seconds,
        )
        raw = client.comment(
            task_id,
            _audit_actor_for_content(content),
            content,
        )
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("taskboard response was not JSON") from exc
        if not envelope.get("ok"):
            raise RuntimeError(str(envelope.get("error") or envelope.get("body")))
        return envelope


class DaemonTaskboardSpawner:
    """Session spawn adapter backed by :class:`daemon.server.DaemonServer`.

    Args:
        daemon_server: Running daemon server instance.

    Example:
        The daemon app builds this adapter during startup and gives it to
        :class:`TaskboardDispatcher`.
    """

    def __init__(self, daemon_server: Any) -> None:
        self.daemon_server = daemon_server

    async def spawn(self, **kwargs: Any) -> str:
        """Create a live daemon session and submit the rendered prompt.

        Args:
            **kwargs: Spawn metadata produced by :class:`TaskboardDispatcher`.

        Returns:
            Session id used by the daemon runtime.
        """

        session_id = str(kwargs["session_id"])
        agent_id = str(kwargs["agent_id"])
        prompt = str(kwargs["prompt"])
        managed = await self.daemon_server.get_or_create_session(
            session_id,
            create_if_missing=True,
        )
        managed.session.attach_runtime(
            bus=self.daemon_server.bus,
            agent_name=agent_id,
            signal_consumer=self.daemon_server.signal_consumer,
            scheduler=self.daemon_server.scheduler,
        )
        managed.session.taskboard_dispatcher = {
            "role": kwargs.get("role"),
            "model": kwargs.get("model"),
            "profile": kwargs.get("profile"),
            "task_id": kwargs.get("task_id"),
            "fire_generation": kwargs.get("fire_generation"),
        }
        if hasattr(managed.session, "start_auto_mode"):
            managed.session.start_auto_mode(max_iterations=20, readonly=False)
        task = asyncio.create_task(
            self.daemon_server.run_input(
                managed,
                prompt,
                source="taskboard",
                job_id=session_id,
            )
        )
        managed.current_input_task = task
        task.add_done_callback(_consume_task_exception)
        return session_id

    async def abort(self, session_id: str) -> None:
        """Abort a live daemon session if it is still running.

        Args:
            session_id: Session identifier to abort.

        Returns:
            None.
        """

        try:
            await self.daemon_server.stop_session_run(session_id)
        except KeyError:
            return


class TaskboardDispatcher:
    """Async dispatcher for taskboard webhook queue rows.

    Args:
        db_path: SQLite database path containing ``webhook_pending``.
        task_client: Client used to re-fetch latest task state.
        session_manager: Spawn surface used for accepted sessions.
        nats_bus: Optional bus used for best-effort alert publication.
        max_concurrent_spawns: Active dispatcher session cap.
        poll_interval_seconds: Queue polling cadence.
        sweep_interval_seconds: Stuck-session sweep cadence.
        stuck_after_seconds: Age after which active dispatcher sessions are
            considered stuck.
        clock: Optional UTC clock for tests.

    Example:
        ``await dispatcher.run_once()`` drains currently eligible queue rows.
    """

    def __init__(
        self,
        *,
        db_path: Path | str,
        task_client: TaskboardTaskClient | None = None,
        session_manager: TaskboardSessionManager | None = None,
        nats_bus: Any | None = None,
        max_concurrent_spawns: int | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        sweep_interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
        stuck_after_seconds: int = DEFAULT_STUCK_AFTER_SECONDS,
        clock: Callable[[], datetime] | None = None,
        agent_runs_client: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.task_client = task_client or DefaultTaskboardTaskClient()
        if session_manager is None:
            raise ValueError("session_manager is required")
        self.session_manager = session_manager
        self.nats_bus = nats_bus
        self.max_concurrent_spawns = _resolve_max_concurrent(max_concurrent_spawns)
        self.poll_interval_seconds = poll_interval_seconds
        self.sweep_interval_seconds = sweep_interval_seconds
        self.stuck_after_seconds = stuck_after_seconds
        self.clock = clock or _utc_now
        self._store = _TaskboardQueueStore(self.db_path, clock=self.clock)
        self._stop_event = asyncio.Event()
        self._last_sweep_at = datetime.min.replace(tzinfo=timezone.utc)
        # agent_runs ledger client (Phase 1 of epic #10028, taskboard task #10223).
        # When None, the dispatcher initialises one from env at first use.
        # Best-effort: ledger writes never raise into the spawn flow.
        if agent_runs_client is None:
            from agent.agent_runs_client import AgentRunsClient

            agent_runs_client = AgentRunsClient.from_env()
        self._agent_runs_client = agent_runs_client

    async def run(self) -> None:
        """Run the polling loop until :meth:`stop` is called.

        Returns:
            None.
        """

        while not self._stop_event.is_set():
            await self.run_once()
            now = self.clock()
            if (now - self._last_sweep_at).total_seconds() >= self.sweep_interval_seconds:
                await self.sweep_stuck_sessions()
                self._last_sweep_at = now
            # Phase 1 (#10223): close the agent_runs ledger loop. The reaper
            # scans run_*.json artifacts written by completed sessions, derives
            # terminal status via agent.run_outcome, PATCHes the matching
            # ledger row, and posts the [KAI] FAILED/COMPLETED audit comment.
            # Same cadence as queue polling — cheap (file scan + best-effort
            # HTTP) and keeps the ledger fresh without a separate process.
            await self._reap_run_outcomes()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                continue

    async def _reap_run_outcomes(self) -> None:
        """Run one sweep of the run-outcome reaper. Best-effort; never raises.

        The reaper has its own SQLite-backed state store so per-run id is
        only acted on once even across dispatcher restarts. Failures here
        cannot be allowed to wedge the queue-poll loop.
        """
        try:
            from agent.run_outcome_reaper import reap_directory

            reap_directory(client=self._agent_runs_client)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "run_outcome_reaper sweep failed: %s",
                _redact_known_secrets(str(exc)),
            )

    def stop(self) -> None:
        """Request the background polling loop to stop.

        Returns:
            None.
        """

        self._stop_event.set()

    async def run_once(self) -> dict[str, int]:
        """Process one FIFO batch of pending taskboard events.

        Returns:
            Counts keyed by dispatch status for rows handled in this tick.
        """

        counts: dict[str, int] = {}
        for row in self._store.pending_audit_rows():
            posted = await self._post_audit_for_row(row)
            if posted:
                self._store.mark_audit_posted(row)
                counts["audit_posted"] = counts.get("audit_posted", 0) + 1
        await self._publish_backpressure_alarm_if_needed()
        pending_rows = self._store.pending_rows()
        active_count = self._store.active_session_count()
        for row in pending_rows:
            if active_count >= self.max_concurrent_spawns:
                break
            status = await self._process_row(row)
            counts[status] = counts.get(status, 0) + 1
            if status == "spawned":
                active_count = self._store.active_session_count()
        return counts

    async def sweep_stuck_sessions(self) -> int:
        """Abort stale dispatcher sessions and mark their queue rows.

        Returns:
            Number of sessions marked as stuck-aborted.
        """

        stuck_sessions = self._store.stuck_sessions(self.stuck_after_seconds)
        for session in stuck_sessions:
            result = self.session_manager.abort(session.session_id)
            if inspect.isawaitable(result):
                await result
            self._store.mark_session_aborted(session.session_id)
            if session.webhook_pending_id is not None:
                self._store.mark_processed_by_id(
                    session.webhook_pending_id,
                    "stuck_aborted",
                    session_id=session.session_id,
                )
            if session.task_id is not None:
                content = _stuck_session_comment(
                    task_id=session.task_id,
                    session_id=session.session_id,
                )
                posted = await self._post_audit_comment(session.task_id, content)
                if posted and session.webhook_pending_id is not None:
                    self._store.mark_audit_posted_by_id(session.webhook_pending_id)
            LOGGER.warning(
                "taskboard_fire_stuck_aborted session_id=%s",
                session.session_id,
            )
        return len(stuck_sessions)

    async def _process_row(self, row: PendingWebhookRow) -> str:
        reserved_key: tuple[int, int, str] | None = None
        try:
            from_status = row.payload.get("from_status")
            to_status = row.payload.get("to_status")
            role_names = roles_to_fire(
                str(from_status) if from_status is not None else None,
                str(to_status) if to_status is not None else None,
            )
            if not role_names:
                self._store.mark_processed(row, "no_op_transition")
                return "no_op_transition"

            payload_task = _extract_task(row.payload)
            task_id = _extract_task_id(row.payload, payload_task)
            fire_generation = _extract_fire_generation(row.payload, payload_task)
            if fire_generation is None:
                raise ValueError("taskboard payload is missing fire_generation")
            latest_task = await self._fetch_latest_task(task_id)

            session_results: list[tuple[TaskboardRoleRoute, str]] = []
            duplicate_count = 0
            for role_text in role_names:
                try:
                    route = resolve_taskboard_role(role_text)
                except ValueError:
                    LOGGER.warning(
                        "taskboard_fire_unknown_role task_id=%s role=%s",
                        task_id,
                        role_text,
                    )
                    self._store.mark_processed(row, "unknown_role")
                    return "unknown_role"

                reserved = self._store.reserve_session(
                    task_id=task_id,
                    fire_generation=fire_generation,
                    agent_id=route.agent_id,
                    webhook_pending_id=str(row.row_id),
                )
                if not reserved:
                    existing_session = self._store.session_for_key(
                        task_id=task_id,
                        fire_generation=fire_generation,
                        agent_id=route.agent_id,
                    )
                    if (
                        existing_session
                        and existing_session.get("webhook_pending_id") == str(row.row_id)
                        and existing_session.get("session_id")
                    ):
                        session_results.append((route, str(existing_session["session_id"])))
                        continue
                    duplicate_count += 1
                    continue
                reserved_key = (task_id, fire_generation, route.agent_id)

                prompt = render_taskboard_fire_prompt(route.role, latest_task)
                requested_session_id = _build_session_id(
                    task_id=task_id,
                    fire_generation=fire_generation,
                    agent_id=route.agent_id,
                )

                # Phase 1 (#10223): record `queued` row in the agent_runs
                # ledger before spawn. Best-effort; ledger errors don't block
                # the dispatch flow. We pass the run_id through to the spawn
                # so a future reaper can PATCH terminal status using it.
                ledger_run_id = self._record_agent_run_queued(
                    task_id=task_id,
                    fire_generation=fire_generation,
                    role=route.agent_id,
                    session_id=requested_session_id,
                    trigger_event_id=str(row.row_id),
                    model=route.model,
                    profile=route.profile,
                )

                try:
                    spawn_result = self.session_manager.spawn(
                        session_id=requested_session_id,
                        task_id=task_id,
                        fire_generation=fire_generation,
                        role=route.role,
                        agent_id=route.agent_id,
                        model=route.model,
                        profile=route.profile,
                        prompt=prompt,
                        task=latest_task,
                    )
                    if inspect.isawaitable(spawn_result):
                        spawn_result = await spawn_result
                except Exception as exc:  # noqa: BLE001
                    self._store.mark_session_failed(*reserved_key)
                    error_message = _redact_known_secrets(str(exc))
                    self._store.mark_processed(row, "spawn_failed", error=error_message)
                    # Phase 1 (#10223): mark the ledger row failed so the
                    # operator UX shows a terminal outcome instead of a
                    # forever-queued ghost.
                    self._record_agent_run_terminal(
                        run_id=ledger_run_id,
                        status="failed",
                        failure_class="tool_unknown_failure",
                        failure_detail=f"spawn raised: {error_message}",
                    )
                    await self._post_spawn_failure_audit(
                        row=row,
                        task_id=task_id,
                        fire_generation=fire_generation,
                        role=route.agent_id,
                        error=exc,
                    )
                    reserved_key = None
                    return "spawn_failed"
                session_id = _normalize_spawn_session_id(
                    spawn_result,
                    default=requested_session_id,
                )
                self._store.finalize_session(
                    task_id=task_id,
                    fire_generation=fire_generation,
                    agent_id=route.agent_id,
                    session_id=session_id,
                )
                # Phase 1 (#10223): PATCH the ledger row to `spawning` and
                # capture the resolved session_id. Terminal status is written
                # later by the run-outcome reaper (#10229 follow-up) once the
                # run JSON is observable.
                self._record_agent_run_spawning(
                    run_id=ledger_run_id, session_id=session_id
                )
                session_results.append((route, session_id))
                reserved_key = None

                LOGGER.info(
                    "taskboard_fire_spawned task_id=%d fire_generation=%d role=%s session_id=%s",
                    task_id,
                    fire_generation,
                    route.role,
                    session_id,
                )

            if not session_results:
                status = "duplicate" if duplicate_count else "no_op_transition"
                self._store.mark_processed(row, status)
                return status

            session_ids = [session_id for _, session_id in session_results]
            row_session_id = (
                session_ids[0] if len(session_ids) == 1 else ",".join(session_ids)
            )
            self._store.mark_processed(row, "spawned", session_id=row_session_id)
            all_audits_posted = True
            for route, session_id in session_results:
                posted = await self._post_audit_comment(
                    task_id,
                    _spawn_success_comment(
                        task_id=task_id,
                        role=route.agent_id,
                        session_id=session_id,
                        model=route.model,
                        profile=route.profile,
                    ),
                )
                all_audits_posted = all_audits_posted and posted
            if all_audits_posted:
                self._store.mark_audit_posted(row)
            return "spawned"
        except Exception as exc:  # noqa: BLE001
            error_message = _redact_known_secrets(str(exc))
            LOGGER.exception(
                "taskboard_fire_rejected row_id=%s error=%s",
                row.row_id,
                error_message,
            )
            if reserved_key is not None:
                self._store.mark_session_failed(*reserved_key)
            self._store.mark_processed(row, "rejected", error=error_message)
            return "rejected"

    async def _fetch_latest_task(self, task_id: int) -> dict[str, Any]:
        result = self.task_client.fetch_task(task_id)
        if inspect.isawaitable(result):
            result = await result
        return _extract_task(result)

    async def _post_audit_for_row(self, row: PendingWebhookRow) -> bool:
        status = row.dispatch_status or ""
        payload_task = _extract_task(row.payload)
        task_id = _extract_task_id(row.payload, payload_task)
        fire_generation = _extract_fire_generation(row.payload, payload_task) or 0
        role_text = str(payload_task.get("agent") or "")
        try:
            route = resolve_taskboard_role(role_text)
            role = route.agent_id
            model = route.model
            profile = route.profile
        except ValueError:
            role = _normalize_role(role_text) or "unknown"
            model = "unknown"
            profile = "unknown"

        if status == "spawned":
            sessions = self._store.sessions_for_row(row.row_id)
            if not sessions:
                fallback_session_id = row.session_id or self._store.session_id_for_row(
                    row.row_id
                )
                if fallback_session_id:
                    sessions = [(fallback_session_id, role, model, profile)]
            if not sessions:
                LOGGER.warning(
                    "taskboard_audit_comment_missing_session row_id=%s",
                    row.row_id,
                )
                return False
            all_posted = True
            for session_id, session_role, session_model, session_profile in sessions:
                posted = await self._post_audit_comment(
                    task_id,
                    _spawn_success_comment(
                        task_id=task_id,
                        role=session_role,
                        session_id=session_id,
                        model=session_model,
                        profile=session_profile,
                    ),
                )
                all_posted = all_posted and posted
            return all_posted
        if status == "spawn_failed":
            error_message = row.last_error or "unknown error"
            return await self._post_audit_comment(
                task_id,
                _spawn_failure_comment(
                    task_id=task_id,
                    error_message=error_message,
                ),
            )
        if status == "stuck_aborted":
            session_id = row.session_id or self._store.session_id_for_row(row.row_id)
            if not session_id:
                LOGGER.warning(
                    "taskboard_audit_comment_missing_session row_id=%s",
                    row.row_id,
                )
                return False
            return await self._post_audit_comment(
                task_id,
                _stuck_session_comment(task_id=task_id, session_id=session_id),
            )
        LOGGER.debug(
            "taskboard_audit_comment_skip_status row_id=%s status=%s generation=%s",
            row.row_id,
            status,
            fire_generation,
        )
        return False

    async def _post_spawn_failure_audit(
        self,
        *,
        row: PendingWebhookRow,
        task_id: int,
        fire_generation: int,
        role: str,
        error: Exception,
    ) -> None:
        error_message = _redact_known_secrets(str(error))
        posted = await self._post_audit_comment(
            task_id,
            _spawn_failure_comment(task_id=task_id, error_message=error_message),
        )
        if posted:
            self._store.mark_audit_posted(row)
        await self._publish_spawn_failed_alert(
            task_id=task_id,
            fire_generation=fire_generation,
            role=role,
            error=error,
        )

    def _record_agent_run_queued(
        self,
        *,
        task_id: int,
        fire_generation: int,
        role: str,
        session_id: str,
        trigger_event_id: str,
        model: str | None = None,
        profile: str | None = None,
    ) -> int | None:
        """Write a ``queued`` row to the taskboard ``agent_runs`` ledger.

        Best-effort: returns the new run_id on success, ``None`` on failure
        (network, ledger disabled, validation). Never raises.
        """
        client = self._agent_runs_client
        if client is None or not getattr(client, "enabled", False):
            return None
        try:
            return client.create(
                {
                    "task_id": int(task_id),
                    "role": str(role),
                    "source_component": "kai-dispatcher",
                    "status": "queued",
                    "session_id": str(session_id),
                    "fire_generation": int(fire_generation),
                    "trigger_event_id": str(trigger_event_id),
                    "model": model,
                    "profile": profile,
                }
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "agent_runs_queued_failed task_id=%s role=%s error=%s",
                task_id,
                role,
                _redact_known_secrets(str(exc)),
            )
            return None

    def _record_agent_run_spawning(
        self, *, run_id: int | None, session_id: str
    ) -> None:
        """PATCH the ledger row to ``spawning`` once the daemon accepts the spawn.

        Best-effort: silently no-ops when the client is disabled or the row
        couldn't be created earlier. Status drift is recoverable later via the
        terminal-status reaper.
        """
        client = self._agent_runs_client
        if client is None or run_id is None or not getattr(client, "enabled", False):
            return
        try:
            client.patch(
                run_id,
                {"status": "spawning", "session_id": str(session_id)},
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "agent_runs_spawning_failed run_id=%s error=%s",
                run_id,
                _redact_known_secrets(str(exc)),
            )

    def _record_agent_run_terminal(
        self,
        *,
        run_id: int | None,
        status: str,
        failure_class: str | None,
        failure_detail: str | None,
    ) -> None:
        """PATCH the ledger row to a terminal status.

        Used today by the dispatcher only for spawn-time exceptions; the
        general-case terminal write lives in the run-outcome reaper
        (follow-up #10229) which derives outcomes from agent run JSONs.
        """
        client = self._agent_runs_client
        if client is None or run_id is None or not getattr(client, "enabled", False):
            return
        body: dict[str, Any] = {"status": status}
        if failure_class is not None:
            body["failure_class"] = failure_class
        if failure_detail is not None:
            body["failure_detail"] = failure_detail
        try:
            client.patch(run_id, body)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "agent_runs_terminal_failed run_id=%s status=%s error=%s",
                run_id,
                status,
                _redact_known_secrets(str(exc)),
            )

    async def _post_audit_comment(self, task_id: int, content: str) -> bool:
        try:
            result = self.task_client.post_audit_comment(task_id, content)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "taskboard_audit_comment_failed task_id=%s error=%s",
                task_id,
                _redact_known_secrets(str(exc)),
            )
            return False

    async def _publish_spawn_failed_alert(
        self,
        *,
        task_id: int,
        fire_generation: int,
        role: str,
        error: Exception,
    ) -> None:
        payload = {
            "task_id": task_id,
            "fire_generation": fire_generation,
            "role": role,
            "error_class": error.__class__.__name__,
            "error_message": _redact_known_secrets(str(error)),
            "ts": _utc_iso(self.clock()),
        }
        try:
            if self.nats_bus is not None and hasattr(self.nats_bus, "publish"):
                result = self.nats_bus.publish(SPAWN_FAILED_SUBJECT, payload)
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("taskboard_dispatcher_spawn_failed_alert_failed error=%s", exc)

    async def _publish_backpressure_alarm_if_needed(self) -> None:
        depth = self._store.stale_pending_count(older_than_seconds=60)
        if depth <= 10:
            return
        payload = {
            "depth": depth,
            "threshold": 10,
            "source": DISPATCHER_SOURCE,
        }
        try:
            if self.nats_bus is not None and hasattr(self.nats_bus, "publish"):
                result = self.nats_bus.publish(BACKPRESSURE_SUBJECT, payload)
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("taskboard_dispatcher_backpressure_alarm_failed error=%s", exc)
        LOGGER.warning(
            "taskboard_dispatcher_backpressure_alarm depth=%d threshold=10",
            depth,
        )


class _TaskboardQueueStore:
    def __init__(self, db_path: Path, *, clock: Callable[[], datetime]) -> None:
        self.db_path = db_path
        self.clock = clock

    def pending_rows(self) -> list[PendingWebhookRow]:
        if not self.db_path.exists():
            return []
        with self._connect(create=False) as conn:
            schema = self._queue_schema(conn)
            if schema is None:
                return []
            where = self._pending_where(schema)
            order = (
                f"ORDER BY {schema.received_column} ASC, {schema.id_column} ASC"
                if schema.received_column
                else f"ORDER BY {schema.id_column} ASC"
            )
            rows = conn.execute(
                f"SELECT * FROM {schema.table} WHERE {where} {order}"
            ).fetchall()
            return [
                PendingWebhookRow(
                    row_id=row[schema.id_column],
                    payload=_parse_payload(row[schema.payload_column]),
                    received_at=(
                        str(row[schema.received_column])
                        if schema.received_column
                        else None
                    ),
                    dispatch_status=(
                        str(row[schema.status_column])
                        if schema.status_column and row[schema.status_column] is not None
                        else None
                    ),
                    session_id=(
                        str(row[schema.session_column])
                        if schema.session_column and row[schema.session_column] is not None
                        else None
                    ),
                    last_error=(
                        str(row[schema.error_column])
                        if schema.error_column and row[schema.error_column] is not None
                        else None
                    ),
                    audit_posted_at=(
                        str(row[schema.audit_posted_column])
                        if schema.audit_posted_column
                        and row[schema.audit_posted_column] is not None
                        else None
                    ),
                )
                for row in rows
            ]

    def pending_audit_rows(self) -> list[PendingWebhookRow]:
        if not self.db_path.exists():
            return []
        with self._connect(create=False) as conn:
            self._ensure_audit_columns(conn)
            schema = self._queue_schema(conn)
            if (
                schema is None
                or schema.status_column is None
                or schema.audit_posted_column is None
            ):
                return []
            placeholders = ",".join("?" for _ in AUDIT_PENDING_STATUSES)
            order = (
                f"ORDER BY {schema.received_column} ASC, {schema.id_column} ASC"
                if schema.received_column
                else f"ORDER BY {schema.id_column} ASC"
            )
            rows = conn.execute(
                f"SELECT * FROM {schema.table}"
                f" WHERE {schema.status_column} IN ({placeholders})"
                f" AND {schema.audit_posted_column} IS NULL {order}",
                AUDIT_PENDING_STATUSES,
            ).fetchall()
            return [
                PendingWebhookRow(
                    row_id=row[schema.id_column],
                    payload=_parse_payload(row[schema.payload_column]),
                    received_at=(
                        str(row[schema.received_column])
                        if schema.received_column
                        else None
                    ),
                    dispatch_status=(
                        str(row[schema.status_column])
                        if row[schema.status_column] is not None
                        else None
                    ),
                    session_id=(
                        str(row[schema.session_column])
                        if schema.session_column and row[schema.session_column] is not None
                        else None
                    ),
                    last_error=(
                        str(row[schema.error_column])
                        if schema.error_column and row[schema.error_column] is not None
                        else None
                    ),
                    audit_posted_at=(
                        str(row[schema.audit_posted_column])
                        if row[schema.audit_posted_column] is not None
                        else None
                    ),
                )
                for row in rows
            ]

    def stale_pending_count(self, *, older_than_seconds: int) -> int:
        if not self.db_path.exists():
            return 0
        with self._connect(create=False) as conn:
            schema = self._queue_schema(conn)
            if schema is None:
                return 0
            where = self._pending_where(schema)
            params: tuple[Any, ...] = ()
            if schema.received_column:
                cutoff = _utc_iso(
                    self.clock() - timedelta(seconds=older_than_seconds)
                )
                where = f"{where} AND {schema.received_column} < ?"
                params = (cutoff,)
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM {schema.table} WHERE {where}",
                params,
            ).fetchone()
            return int(row["count"] if row is not None else 0)

    def mark_processed(
        self,
        row: PendingWebhookRow,
        status: str,
        *,
        session_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.mark_processed_by_id(
            row.row_id,
            status,
            session_id=session_id,
            error=error,
        )

    def mark_processed_by_id(
        self,
        row_id: Any,
        status: str,
        *,
        session_id: str | None = None,
        error: str | None = None,
    ) -> None:
        if not self.db_path.exists():
            return
        with self._connect(create=False) as conn:
            schema = self._queue_schema(conn)
            if schema is None:
                return
            updates: list[str] = []
            params: list[Any] = []
            if schema.processed_column:
                updates.append(f"{schema.processed_column} = ?")
                params.append(_utc_iso(self.clock()))
            if schema.status_column:
                updates.append(f"{schema.status_column} = ?")
                params.append(status)
            if session_id is not None and schema.session_column:
                updates.append(f"{schema.session_column} = ?")
                params.append(session_id)
            if error is not None and schema.error_column:
                updates.append(f"{schema.error_column} = ?")
                params.append(error[:2000])
            if not updates:
                return
            params.append(row_id)
            conn.execute(
                f"UPDATE {schema.table} SET {', '.join(updates)}"
                f" WHERE {schema.id_column} = ?",
                tuple(params),
            )

    def mark_audit_posted(self, row: PendingWebhookRow) -> None:
        self.mark_audit_posted_by_id(row.row_id)

    def mark_audit_posted_by_id(self, row_id: Any) -> None:
        if not self.db_path.exists():
            return
        with self._connect(create=False) as conn:
            self._ensure_audit_columns(conn)
            schema = self._queue_schema(conn)
            if schema is None or schema.audit_posted_column is None:
                return
            conn.execute(
                f"UPDATE {schema.table} SET {schema.audit_posted_column} = ?"
                f" WHERE {schema.id_column} = ?",
                (_utc_iso(self.clock()), row_id),
            )

    def session_id_for_row(self, row_id: Any) -> str | None:
        if not self.db_path.exists():
            return None
        with self._connect(create=False) as conn:
            if not self._table_exists(conn, "sessions"):
                return None
            row = conn.execute(
                "SELECT session_id FROM sessions"
                " WHERE webhook_pending_id = ?"
                " AND session_id IS NOT NULL"
                " ORDER BY updated_at DESC LIMIT 1",
                (str(row_id),),
            ).fetchone()
            if row is None or row["session_id"] is None:
                return None
            return str(row["session_id"])

    def sessions_for_row(self, row_id: Any) -> list[tuple[str, str, str, str]]:
        if not self.db_path.exists():
            return []
        with self._connect(create=False) as conn:
            if not self._table_exists(conn, "sessions"):
                return []
            rows = conn.execute(
                "SELECT session_id, agent_id FROM sessions"
                " WHERE webhook_pending_id = ?"
                " AND session_id IS NOT NULL"
                " ORDER BY agent_id ASC",
                (str(row_id),),
            ).fetchall()
        sessions: list[tuple[str, str, str, str]] = []
        for row in rows:
            session_id = str(row["session_id"])
            agent_id = str(row["agent_id"] or "unknown")
            try:
                route = resolve_taskboard_role(agent_id)
                role = route.agent_id
                model = route.model
                profile = route.profile
            except ValueError:
                role = _normalize_role(agent_id) or "unknown"
                model = "unknown"
                profile = "unknown"
            sessions.append((session_id, role, model, profile))
        return sessions

    def session_for_key(
        self,
        *,
        task_id: int,
        fire_generation: int,
        agent_id: str,
    ) -> dict[str, str | None] | None:
        if not self.db_path.exists():
            return None
        with self._connect(create=False) as conn:
            if not self._table_exists(conn, "sessions"):
                return None
            row = conn.execute(
                "SELECT session_id, webhook_pending_id FROM sessions"
                " WHERE taskboard_task_id = ?"
                " AND fire_generation = ?"
                " AND agent_id = ?"
                " ORDER BY updated_at DESC LIMIT 1",
                (task_id, fire_generation, agent_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "session_id": (
                    str(row["session_id"]) if row["session_id"] is not None else None
                ),
                "webhook_pending_id": (
                    str(row["webhook_pending_id"])
                    if row["webhook_pending_id"] is not None
                    else None
                ),
            }

    def active_session_count(self) -> int:
        if not self.db_path.exists():
            return 0
        with self._connect(create=False) as conn:
            if not self._table_exists(conn, "sessions"):
                return 0
            placeholders = ",".join("?" for _ in ACTIVE_SESSION_STATUSES)
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM sessions"
                " WHERE source = ?"
                f" AND status IN ({placeholders})",
                (DISPATCHER_SOURCE, *ACTIVE_SESSION_STATUSES),
            ).fetchone()
            return int(row["count"] if row is not None else 0)

    def reserve_session(
        self,
        *,
        task_id: int,
        fire_generation: int,
        agent_id: str,
        webhook_pending_id: str,
    ) -> bool:
        self.ensure_sessions_schema()
        now = _utc_iso(self.clock())
        try:
            with self._connect(create=True) as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id,
                        taskboard_task_id,
                        fire_generation,
                        agent_id,
                        source,
                        status,
                        webhook_pending_id,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        None,
                        task_id,
                        fire_generation,
                        agent_id,
                        DISPATCHER_SOURCE,
                        "spawning",
                        webhook_pending_id,
                        now,
                        now,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def finalize_session(
        self,
        *,
        task_id: int,
        fire_generation: int,
        agent_id: str,
        session_id: str,
    ) -> None:
        now = _utc_iso(self.clock())
        with self._connect(create=True) as conn:
            conn.execute(
                """
                UPDATE sessions
                SET session_id = ?, status = ?, updated_at = ?
                WHERE taskboard_task_id = ?
                  AND fire_generation = ?
                  AND agent_id = ?
                """,
                (
                    session_id,
                    "running",
                    now,
                    task_id,
                    fire_generation,
                    agent_id,
                ),
            )

    def stuck_sessions(self, older_than_seconds: int) -> list[StuckSession]:
        if not self.db_path.exists():
            return []
        cutoff = _utc_iso(self.clock() - timedelta(seconds=older_than_seconds))
        with self._connect(create=False) as conn:
            if not self._table_exists(conn, "sessions"):
                return []
            placeholders = ",".join("?" for _ in ACTIVE_SESSION_STATUSES)
            rows = conn.execute(
                "SELECT session_id, webhook_pending_id, taskboard_task_id,"
                " fire_generation, agent_id FROM sessions"
                " WHERE source = ?"
                f" AND status IN ({placeholders})"
                " AND created_at < ?"
                " AND session_id IS NOT NULL",
                (DISPATCHER_SOURCE, *ACTIVE_SESSION_STATUSES, cutoff),
            ).fetchall()
            return [
                StuckSession(
                    session_id=str(row["session_id"]),
                    webhook_pending_id=(
                        str(row["webhook_pending_id"])
                        if row["webhook_pending_id"] is not None
                        else None
                    ),
                    task_id=(
                        int(row["taskboard_task_id"])
                        if row["taskboard_task_id"] is not None
                        else None
                    ),
                    fire_generation=(
                        int(row["fire_generation"])
                        if row["fire_generation"] is not None
                        else None
                    ),
                    agent_id=(
                        str(row["agent_id"]) if row["agent_id"] is not None else None
                    ),
                )
                for row in rows
            ]

    def mark_session_aborted(self, session_id: str) -> None:
        if not self.db_path.exists():
            return
        now = _utc_iso(self.clock())
        with self._connect(create=False) as conn:
            if not self._table_exists(conn, "sessions"):
                return
            conn.execute(
                "UPDATE sessions"
                " SET status = ?, updated_at = ?, aborted_at = ?"
                " WHERE session_id = ?",
                ("aborted", now, now, session_id),
            )

    def mark_session_failed(
        self,
        task_id: int,
        fire_generation: int,
        agent_id: str,
    ) -> None:
        if not self.db_path.exists():
            return
        now = _utc_iso(self.clock())
        with self._connect(create=False) as conn:
            if not self._table_exists(conn, "sessions"):
                return
            conn.execute(
                "UPDATE sessions"
                " SET status = ?, updated_at = ?"
                " WHERE taskboard_task_id = ?"
                " AND fire_generation = ?"
                " AND agent_id = ?",
                ("failed", now, task_id, fire_generation, agent_id),
            )

    def ensure_sessions_schema(self) -> None:
        with self._connect(create=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT UNIQUE,
                    taskboard_task_id INTEGER,
                    fire_generation INTEGER,
                    agent_id TEXT,
                    source TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    webhook_pending_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    aborted_at TEXT
                )
                """
            )
            existing = self._columns(conn, "sessions")
            additions = {
                "session_id": "TEXT",
                "taskboard_task_id": "INTEGER",
                "fire_generation": "INTEGER",
                "agent_id": "TEXT",
                "source": "TEXT",
                "status": "TEXT",
                "webhook_pending_id": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
                "aborted_at": "TEXT",
            }
            for column, definition in additions.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {definition}")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_taskboard_fire_agent
                ON sessions (taskboard_task_id, fire_generation, agent_id)
                WHERE taskboard_task_id IS NOT NULL
                  AND fire_generation IS NOT NULL
                  AND agent_id IS NOT NULL
                """
            )

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        if not create and not self.db_path.exists():
            raise FileNotFoundError(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _queue_schema(self, conn: sqlite3.Connection) -> _QueueSchema | None:
        for table in ("webhook_pending", "webhook_deliveries"):
            if not self._table_exists(conn, table):
                continue
            columns = self._columns(conn, table)
            id_column = _first_existing(columns, ("id", "delivery_id", "event_id"))
            payload_column = _first_existing(columns, ("payload", "payload_json"))
            if id_column is None or payload_column is None:
                continue
            return _QueueSchema(
                table=table,
                id_column=id_column,
                payload_column=payload_column,
                processed_column=_first_existing(
                    columns,
                    ("processed_at", "completed_at"),
                ),
                status_column="dispatch_status" if "dispatch_status" in columns else None,
                received_column="received_at" if "received_at" in columns else None,
                session_column="session_id" if "session_id" in columns else None,
                error_column="last_error" if "last_error" in columns else None,
                audit_posted_column=(
                    "audit_posted_at" if "audit_posted_at" in columns else None
                ),
            )
        return None

    def _ensure_audit_columns(self, conn: sqlite3.Connection) -> None:
        for table in ("webhook_pending", "webhook_deliveries"):
            if not self._table_exists(conn, table):
                continue
            columns = self._columns(conn, table)
            if "audit_posted_at" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN audit_posted_at TIMESTAMPTZ")
            refreshed = self._columns(conn, table)
            if "dispatch_status" in refreshed:
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_audit_pending"
                    f" ON {table} (dispatch_status, audit_posted_at)"
                )

    @staticmethod
    def _pending_where(schema: _QueueSchema) -> str:
        if schema.processed_column:
            return f"{schema.processed_column} IS NULL"
        if schema.status_column:
            return f"{schema.status_column} IN ('accepted', 'pending')"
        return "1 = 0"

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _normalize_role(role: str) -> str:
    text = re.sub(r"[-_]+", " ", str(role or "").strip().lower())
    return re.sub(r"\s+", " ", text)


def _audit_actor_for_content(content: str) -> str:
    if content.startswith("[System]"):
        return "System"
    return "Orchestrator"


def _spawn_success_comment(
    *,
    task_id: int,
    role: str,
    session_id: str,
    model: str,
    profile: str,
) -> str:
    return (
        f"[Orchestrator] Fired {role} agent for #{task_id} "
        f"(session_id={session_id}, model={model}, profile={profile})"
    )


def _spawn_failure_comment(*, task_id: int, error_message: str) -> str:
    safe_error = _redact_known_secrets(error_message).replace("\n", " ")[:500]
    return (
        f"[System] spawn failed for #{task_id}: {safe_error}; "
        f"retry with agent-ops fire {task_id}"
    )


def _stuck_session_comment(*, task_id: int, session_id: str) -> str:
    return (
        f"[System] sweeper aborted stuck session for #{task_id} after 60min "
        f"(session_id={session_id})"
    )


def _redact_known_secrets(message: str) -> str:
    redacted = str(message)
    for env_name in SECRET_ENV_VARS:
        secret = os.getenv(env_name, "").strip()
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _resolve_max_concurrent(value: int | None) -> int:
    if value is not None:
        return max(0, int(value))
    raw = os.getenv("MAX_CONCURRENT_TASKBOARD_SPAWNS", "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENT_SPAWNS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_SPAWNS


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _first_existing(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _parse_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("webhook payload must be a JSON object")


def _extract_task(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("task payload must be a JSON object")
    task = payload.get("task")
    if isinstance(task, dict):
        return task
    body = payload.get("body")
    if isinstance(body, dict):
        return _extract_task(body)
    if "id" in payload:
        return payload
    if "task_id" in payload:
        task = dict(payload)
        task["id"] = payload["task_id"]
        return task
    raise ValueError("task payload does not contain a task id")


def _extract_task_id(payload: dict[str, Any], task: dict[str, Any]) -> int:
    value = payload.get("task_id", task.get("id"))
    if value is None:
        raise ValueError("taskboard payload is missing task id")
    return int(value)


def _extract_fire_generation(
    payload: dict[str, Any],
    task: dict[str, Any] | None = None,
) -> int | None:
    task = task or payload
    value = payload.get("fire_generation", task.get("fire_generation"))
    if value is None:
        return None
    return int(value)


def _build_session_id(*, task_id: int, fire_generation: int, agent_id: str) -> str:
    safe_agent = re.sub(r"[^a-zA-Z0-9_.-]+", "-", agent_id).strip("-")
    return f"taskboard-{task_id}-{fire_generation}-{safe_agent}"


def _normalize_spawn_session_id(result: Any, *, default: str) -> str:
    if isinstance(result, str) and result:
        return result
    if isinstance(result, dict):
        for key in ("session_id", "sessionId", "sessionKey", "childSessionKey"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        details = result.get("details")
        if isinstance(details, dict):
            return _normalize_spawn_session_id(details, default=default)
    return default


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        LOGGER.exception("taskboard spawned session task failed", exc_info=exc)

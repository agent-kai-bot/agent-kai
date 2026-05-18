"""OpenClaw-compatible gateway that lets the taskboard drive this agent."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status

import config
from agent.taskboard_tools import TaskboardContext
from daemon.core import DEFAULT_AUTO_MAX_ITERATIONS, Session, max_auto_iterations_cap
from taskboard_gateway.agent_map import resolve_agent_id
from taskboard_gateway.config import (
    allow_unauthenticated_local,
    gateway_token,
    runs_dir,
)
from taskboard_gateway.models import (
    CronWakeRequest,
    RunSummary,
    SessionListArgs,
    SessionSendArgs,
    SessionSpawnArgs,
    StatusResponse,
    ToolInvokeResponse,
    ToolsInvokeRequest,
)
from taskboard_gateway.runs import RunStore, TaskboardRun, extract_session_binding

RunExecutor = Callable[[TaskboardRun, RunStore], Awaitable[None]]
MessageExecutor = Callable[..., Awaitable[str]]


def _coerce_positive_int(raw: Any) -> int | None:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _resolve_gateway_max_iterations(agent_name: str | None) -> int:
    agent_cfg = config.AGENTS.get(agent_name or "", {})
    configured = None
    if isinstance(agent_cfg, dict):
        configured = _coerce_positive_int(agent_cfg.get("max_iterations"))
    requested = configured or DEFAULT_AUTO_MAX_ITERATIONS
    return max(1, min(max_auto_iterations_cap(), requested))


def _is_local_client(request: Request) -> bool:
    """Return whether the request came from a loopback/test client.

    Args:
        request: FastAPI request object.

    Returns:
        True for localhost and TestClient requests.
    """

    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def _require_auth(request: Request) -> None:
    """Enforce bearer authentication when a gateway token is configured.

    Args:
        request: FastAPI request object.

    Raises:
        HTTPException: If authentication is required and missing or invalid.
    """

    token = gateway_token()
    if not token:
        return
    if allow_unauthenticated_local() and _is_local_client(request):
        return
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or value.strip() != token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="gateway bearer token required",
        )


def _run_summary(run: TaskboardRun) -> RunSummary:
    """Convert a durable run into a session-list payload.

    Args:
        run: Run record to summarize.

    Returns:
        OpenClaw-compatible run/session summary.
    """

    return RunSummary(
        key=run.session_key,
        agentId=run.requested_agent_id,
        status=run.status,
        label=run.label,
        runId=run.run_id,
        taskId=run.task_id,
        createdAt=run.created_at,
        updatedAt=run.updated_at,
        endedAt=run.ended_at,
        display=f"{run.requested_agent_id} {run.label}".strip(),
    )


def _agent_from_session_key(session_key: str) -> str:
    """Infer a local agent name from an OpenClaw session key.

    Args:
        session_key: Session key sent by the taskboard.

    Returns:
        Local agent name to attach.
    """

    if session_key.startswith("agent:"):
        parts = session_key.split(":", 2)
        if len(parts) >= 2 and parts[1]:
            return resolve_agent_id(parts[1]).local_agent_name
    return resolve_agent_id("main").local_agent_name


async def execute_session_message(
    *,
    session_key: str,
    message: str,
    local_agent_name: str,
    timeout_seconds: int | None = None,
) -> str:
    """Execute a synchronous message against a local daemon session.

    Args:
        session_key: Session key/name to load.
        message: User/taskboard message.
        local_agent_name: Local agent config name.
        timeout_seconds: Optional timeout for the full response.

    Returns:
        Final assistant reply text.
    """

    session = Session(session_key)
    session.load()
    session.attach_runtime(agent_name=local_agent_name)

    async def _collect_reply() -> str:
        """Collect the final event from one non-auto agent turn."""

        final_text = ""
        async for event in session.stream_agent_events(
            message,
            source="taskboard",
        ):
            if event.get("type") == "final":
                final_text = str(event.get("data") or "")
        session.save()
        return final_text

    if timeout_seconds:
        return await asyncio.wait_for(
            _collect_reply(),
            timeout=max(1, int(timeout_seconds)),
        )
    return await _collect_reply()


async def execute_run_with_local_session(run: TaskboardRun, store: RunStore) -> None:
    """Execute one accepted taskboard run in a local daemon session.

    Args:
        run: Accepted taskboard run.
        store: Durable run store used to update state and events.
    """

    latest = await asyncio.to_thread(store.get, run.run_id)
    if latest and latest.status == "aborted":
        return

    await asyncio.to_thread(store.update_status, run, "starting")
    session = Session(run.session_key)
    session.load()
    session_token, session_generation = extract_session_binding(run.prompt)
    session.taskboard_context = TaskboardContext(
        base_url=os.getenv("TASKBOARD_URL", "http://localhost:8080"),
        bearer_token=(
            os.getenv("TASKBOARD_BEARER_TOKEN", "").strip()
            or os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
            or os.getenv("OPENCLAW_TOKEN", "").strip()
        ),
        session_token=session_token,
        session_generation=session_generation,
        agent_name=run.requested_agent_id,
        task_id=run.task_id,
    )
    session.attach_runtime(agent_name=run.local_agent_name)
    session.start_auto_mode(
        max_iterations=_resolve_gateway_max_iterations(run.local_agent_name),
        readonly=False,
        heartbeat_subscribed=False,
    )
    await asyncio.to_thread(store.update_status, run, "running")

    final_text = ""
    try:
        async for event in session.stream_agent_events(
            run.prompt,
            source="taskboard",
            job_id=run.run_id,
        ):
            await asyncio.to_thread(store.append_event, run, event)
            if event.get("type") == "final":
                final_text = str(event.get("data") or "")
            latest = await asyncio.to_thread(store.get, run.run_id)
            if latest and latest.status == "aborted":
                session.stop_auto_mode("aborted by taskboard gateway")
                return
        session.save()
        await asyncio.to_thread(
            store.update_status,
            run,
            "completed",
            final_text=final_text,
        )
    except Exception as exc:  # noqa: BLE001
        session.save()
        await asyncio.to_thread(store.update_status, run, "failed", error=str(exc))


def create_gateway_app(
    *,
    store: RunStore | None = None,
    executor: RunExecutor | None = None,
    message_executor: MessageExecutor | None = None,
) -> FastAPI:
    """Create the taskboard compatibility gateway app.

    Args:
        store: Optional run store, primarily for tests.
        executor: Optional run executor, primarily for tests.
        message_executor: Optional synchronous message executor for tests.

    Returns:
        Configured FastAPI application.
    """

    run_store = store or RunStore(runs_dir())
    run_executor = executor or execute_run_with_local_session
    session_message_executor = message_executor or execute_session_message
    active_tasks: dict[str, asyncio.Task[None]] = {}

    app = FastAPI(title="Taskboard Agent Gateway")
    app.state.run_store = run_store
    app.state.active_tasks = active_tasks

    async def schedule_run(run: TaskboardRun) -> None:
        """Schedule a run on the app event loop.

        Args:
            run: Run to execute asynchronously.
        """

        async def _runner() -> None:
            """Execute a scheduled run and clear active-task bookkeeping."""

            try:
                await run_executor(run, run_store)
            finally:
                active_tasks.pop(run.run_id, None)

        active_tasks[run.run_id] = asyncio.create_task(_runner())

    @app.get("/api/status", response_model=StatusResponse)
    async def status_endpoint(request: Request) -> StatusResponse:
        """Return gateway status.

        Args:
            request: FastAPI request object.

        Returns:
            Status response with run count.
        """

        _require_auth(request)
        return StatusResponse(status="ok", runs=len(run_store.list_runs(limit=None)))

    @app.post("/tools/invoke", response_model=ToolInvokeResponse)
    async def tools_invoke(
        payload: ToolsInvokeRequest,
        request: Request,
    ) -> ToolInvokeResponse:
        """Invoke an OpenClaw-compatible gateway tool.

        Args:
            payload: Tool invocation request.
            request: FastAPI request object.

        Returns:
            OpenClaw-compatible response envelope.
        """

        _require_auth(request)
        tool = payload.tool.strip()
        try:
            match tool:
                case "sessions_spawn":
                    args = SessionSpawnArgs.model_validate(payload.args)
                    route = resolve_agent_id(args.agentId)
                    run = run_store.create_run(
                        requested_agent_id=route.requested_agent_id,
                        local_agent_name=route.local_agent_name,
                        prompt=args.task,
                        label=args.label,
                        cleanup=args.cleanup,
                    )
                    await schedule_run(run)
                    return ToolInvokeResponse(
                        ok=True,
                        result={
                            "content": "accepted",
                            "details": {
                                "status": "accepted",
                                "childSessionKey": run.session_key,
                                "runId": run.run_id,
                            },
                        },
                    )
                case "sessions_send":
                    args = SessionSendArgs.model_validate(payload.args)
                    run = run_store.get_by_session_key(args.sessionKey)
                    if "SYSTEM: ABORT" in args.message.upper() and run is not None:
                        task = active_tasks.get(run.run_id)
                        if task and not task.done():
                            task.cancel()
                        run_store.update_status(
                            run,
                            "aborted",
                            error="aborted by sessions_send",
                        )
                        return ToolInvokeResponse(
                            ok=True,
                            result={
                                "content": "aborted",
                                "details": {
                                    "status": "aborted",
                                    "sessionKey": run.session_key,
                                    "runId": run.run_id,
                                },
                            },
                        )
                    local_agent_name = (
                        run.local_agent_name
                        if run is not None
                        else _agent_from_session_key(args.sessionKey)
                    )
                    if run is not None:
                        run_store.append_followup(run, args.message)
                    task = active_tasks.get(run.run_id) if run is not None else None
                    if task and not task.done():
                        return ToolInvokeResponse(
                            ok=True,
                            result={
                                "content": "queued",
                                "details": {
                                    "status": "queued",
                                    "sessionKey": args.sessionKey,
                                    "runId": run.run_id,
                                },
                            },
                        )
                    reply = await session_message_executor(
                        session_key=args.sessionKey,
                        message=args.message,
                        local_agent_name=local_agent_name,
                        timeout_seconds=args.timeoutSeconds,
                    )
                    return ToolInvokeResponse(
                        ok=True,
                        result={
                            "content": reply,
                            "details": {
                                "status": "completed",
                                "sessionKey": args.sessionKey,
                                "runId": run.run_id if run is not None else None,
                                "reply": reply,
                            },
                        },
                    )
                case "sessions_list":
                    args = SessionListArgs.model_validate(payload.args)
                    sessions = [
                        _run_summary(run).model_dump()
                        for run in run_store.list_runs(limit=args.limit)
                    ]
                    return ToolInvokeResponse(
                        ok=True,
                        result={
                            "content": {"sessions": sessions},
                            "details": {"status": "ok", "sessions": sessions},
                        },
                    )
                case _:
                    return ToolInvokeResponse(
                        ok=False,
                        error=f"unsupported tool: {tool}",
                    )
        except asyncio.TimeoutError:
            return ToolInvokeResponse(ok=False, error="session send timed out")
        except ValueError as exc:
            return ToolInvokeResponse(ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ToolInvokeResponse(
                ok=False,
                error=f"gateway execution failed: {exc}",
            )

    @app.post("/api/sessions/{session_key:path}/abort")
    async def abort_session(session_key: str, request: Request) -> dict[str, Any]:
        """Abort a taskboard-spawned session.

        Args:
            session_key: Session key returned by ``sessions_spawn``.
            request: FastAPI request object.

        Returns:
            Abort confirmation payload.

        Raises:
            HTTPException: If the session key is unknown.
        """

        _require_auth(request)
        run = run_store.get_by_session_key(session_key)
        if run is None:
            raise HTTPException(status_code=404, detail="session not found")
        task = active_tasks.get(run.run_id)
        if task and not task.done():
            task.cancel()
        run_store.update_status(run, "aborted", error="aborted by gateway request")
        return {"ok": True, "sessionKey": session_key, "runId": run.run_id}

    @app.post("/api/cron/wake")
    async def cron_wake(
        payload: CronWakeRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Accept taskboard wake notifications as background main-agent runs.

        Args:
            payload: Wake request from the taskboard.
            request: FastAPI request object.

        Returns:
            Accepted run metadata.
        """

        _require_auth(request)
        route = resolve_agent_id("main")
        run = run_store.create_run(
            requested_agent_id="main",
            local_agent_name=route.local_agent_name,
            prompt=payload.text or payload.action,
            label="cron-wake",
            cleanup="keep",
        )
        await schedule_run(run)
        return {
            "ok": True,
            "status": "accepted",
            "sessionKey": run.session_key,
            "runId": run.run_id,
        }

    return app

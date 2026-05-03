"""Out-of-process daemon taskboard worker.

The taskboard dispatcher uses this module for workspace-enabled sessions so
agent file/git tools inherit an isolated current working directory instead of
mutating the daemon/operator checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from agent.taskboard_tools import TaskboardContext
from daemon.core import Session
from nats_bus.bus import NatsBus
from taskboard_gateway.config import runs_dir
from taskboard_gateway.runs import RunStore, TaskboardRun


EVENT_SUBJECT = "daemon.session.event"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _optional_int(raw: str | None) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _run_label(*, task_id: str, role: str, session_id: str) -> str:
    safe_session = session_id.replace(":", "-")
    return f"task-{task_id}-{role}-{safe_session}"


@contextlib.contextmanager
def _cwd(path: Path):
    """Temporarily change cwd for repo-root-relative run-store paths."""
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def create_taskboard_run(
    *,
    session_id: str,
    prompt: str,
    agent_id: str,
    task_id: str,
) -> tuple[RunStore, TaskboardRun]:
    """Create the durable run_*.json artifact watched by the reaper."""

    store = _run_store()
    run = store.create_run(
        requested_agent_id=agent_id,
        local_agent_name=agent_id,
        prompt=prompt,
        label=_run_label(task_id=task_id, role=agent_id, session_id=session_id),
        cleanup="keep",
    )
    # Dispatcher-created ledger rows are keyed by the daemon session id, not
    # the gateway-generated session key. Preserve that id in the artifact so
    # run_outcome_reaper can PATCH the existing row instead of backfilling a
    # duplicate row.
    run.session_key = session_id
    try:
        run.task_id = int(task_id)
    except ValueError:
        pass
    _store_save(store, run)
    return store, run


async def _publish_event(
    bus: NatsBus | None,
    session_id: str,
    topic: str,
    payload: Mapping[str, Any],
) -> None:
    message = {
        "session": session_id,
        "topic": topic,
        "payload": dict(payload),
    }
    path = _event_path(session_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(message, ensure_ascii=False) + "\n")
    if bus is None or not bus.is_connected:
        return
    await bus.publish(EVENT_SUBJECT, message)


def _event_path(session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in session_id)
    return Path(tempfile.gettempdir()) / "kai-taskboard-worker-events" / f"{safe}.jsonl"


def _run_store() -> RunStore:
    with _cwd(Path(__file__).resolve().parents[1]):
        return RunStore(runs_dir())


def _store_save(store: RunStore, run: TaskboardRun) -> None:
    with _cwd(Path(__file__).resolve().parents[1]):
        store.save(run)


def _store_update_status(
    store: RunStore,
    run: TaskboardRun,
    status: str,
    **kwargs: Any,
) -> TaskboardRun:
    with _cwd(Path(__file__).resolve().parents[1]):
        return store.update_status(run, status, **kwargs)


def _store_append_event(
    store: RunStore,
    run: TaskboardRun,
    event: dict[str, Any],
) -> TaskboardRun:
    with _cwd(Path(__file__).resolve().parents[1]):
        return store.append_event(run, event)


def _wrap_publish_event(publish_event, *, bus: NatsBus | None, session_id: str):
    def _wrapped(topic: str, payload: dict[str, Any] | None = None):
        event = publish_event(topic, payload)
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            loop.create_task(_publish_event(bus, session_id, topic, payload or {}))
        return event

    return _wrapped


async def run_worker(args: argparse.Namespace) -> int:
    primary_repo = Path(_require_env("KAI_PRIMARY_REPO")).expanduser().resolve()
    if not primary_repo.is_dir():
        raise RuntimeError(f"KAI_PRIMARY_REPO is not a directory: {primary_repo}")
    os.chdir(primary_repo)

    workspace_path = _require_env("KAI_TICKET_WORKSPACE")
    workspace_manifest = _require_env("KAI_WORKSPACE_MANIFEST")
    task_id = _require_env("KAI_TASK_ID")
    role = _require_env("KAI_ROLE")

    env_payload = {
        "workspace_path": workspace_path,
        "primary_repo_path": str(primary_repo),
        "workspace_manifest_path": workspace_manifest,
        "task_id": task_id,
        "role": role,
    }

    store, run = create_taskboard_run(
        session_id=args.session_id,
        prompt=args.prompt,
        agent_id=args.agent_id,
        task_id=task_id,
    )
    await asyncio.to_thread(_store_update_status, store, run, "starting")

    bus: NatsBus | None = None
    try:
        bus = NatsBus(url=args.nats_url, agent_name=f"{args.agent_id}-worker")
        await bus.connect()
    except Exception:  # noqa: BLE001 - event streaming is best-effort.
        bus = None

    session = Session(args.session_id)
    final_text = ""
    try:
        await _publish_event(bus, args.session_id, "worker.started", env_payload)
        session.load()
        session.publish_event = _wrap_publish_event(
            session.publish_event,
            bus=bus,
            session_id=args.session_id,
        )
        session.taskboard_context = TaskboardContext(
            base_url=os.getenv("TASKBOARD_URL", "http://localhost:8080"),
            bearer_token=(
                os.getenv("TASKBOARD_BEARER_TOKEN", "").strip()
                or os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
                or os.getenv("OPENCLAW_TOKEN", "").strip()
            ),
            session_token=os.getenv("TASKBOARD_SESSION_TOKEN", "").strip(),
            session_generation=_optional_int(os.getenv("TASKBOARD_SESSION_GENERATION")),
            agent_name=args.agent_id,
        )
        session.taskboard_dispatcher = {
            "role": args.role,
            "model": args.model,
            "profile": args.profile,
            "task_id": task_id,
            "fire_generation": args.fire_generation,
            "workspace_path": workspace_path,
            "worktree_path": str(primary_repo),
            "primary_repo_path": str(primary_repo),
            "workspace_manifest_path": workspace_manifest,
        }
        session.attach_runtime(bus=bus, agent_name=args.agent_id)
        session.start_auto_mode(max_iterations=args.max_iterations, readonly=False)
        await asyncio.to_thread(_store_update_status, store, run, "running")

        async for event in session.stream_agent_events(
            args.prompt,
            source="taskboard",
            job_id=run.run_id,
        ):
            await asyncio.to_thread(_store_append_event, store, run, event)
            etype = str(event.get("type") or "")
            data = event.get("data")
            payload = data if isinstance(data, dict) else {"value": data}
            topic = f"agent.{etype}"
            if etype == "auto_stopped":
                topic = "auto.stopped"
            await _publish_event(bus, args.session_id, topic, payload)
            if etype == "final":
                final_text = str(data or "")
        session.save()
        await asyncio.to_thread(
            _store_update_status,
            store,
            run,
            "completed",
            final_text=final_text,
        )
        await _publish_event(
            bus,
            args.session_id,
            "worker.completed",
            {"run_id": run.run_id},
        )
        return 0
    except asyncio.CancelledError:
        session.save()
        await asyncio.to_thread(
            _store_update_status,
            store,
            run,
            "aborted",
            error="worker cancelled",
        )
        await _publish_event(
            bus,
            args.session_id,
            "agent.error",
            {"value": "worker cancelled"},
        )
        return 130
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            session.save()
        await asyncio.to_thread(_store_update_status, store, run, "failed", error=str(exc))
        await _publish_event(bus, args.session_id, "agent.error", {"value": str(exc)})
        return 1
    finally:
        if bus is not None:
            with contextlib.suppress(Exception):
                await bus.disconnect()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--profile", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--fire-generation", default="")
    parser.add_argument("--max-iterations", type=int, default=200)
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--prompt-file", required=True)
    args = parser.parse_args(argv)
    args.prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run_worker(args))


if __name__ == "__main__":
    sys.exit(main())

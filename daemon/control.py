"""Helpers for starting and monitoring the local daemon process."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from config import DEFAULT_AGENT, NATS_URL, PROJECT_ROOT, WORKSPACES_DIR
from daemon.server import (
    DEFAULT_DAEMON_HOST,
    DEFAULT_DAEMON_PORT,
    DEFAULT_DAEMON_WS_URL,
)

DEFAULT_DAEMON_HEALTH_PATH = "/api/health"
DEFAULT_DAEMON_HTTP_URL = f"http://{DEFAULT_DAEMON_HOST}:{DEFAULT_DAEMON_PORT}"
DEFAULT_DAEMON_HEALTH_URL = f"{DEFAULT_DAEMON_HTTP_URL}{DEFAULT_DAEMON_HEALTH_PATH}"
DAEMON_PID_PATH = Path(WORKSPACES_DIR) / "kaid.pid"
DAEMON_LOG_PATH = Path(PROJECT_ROOT) / "logs" / "kaid.log"
DEFAULT_DAEMON_START_TIMEOUT = 10.0
DEFAULT_DAEMON_POLL_INTERVAL = 0.1


@dataclass(frozen=True)
class DaemonStartResult:
    """Outcome of ensuring the local daemon is available."""

    remote_url: str
    pid: int | None = None
    already_running: bool = False


@dataclass(frozen=True)
class DaemonStatus:
    """Current view of the daemon's runtime state."""

    running: bool
    healthy: bool
    managed: bool
    pid: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class DaemonStopResult:
    """Outcome of a stop request issued through kaictl."""

    stopped: bool
    pid: int | None = None
    already_stopped: bool = False
    detail: str = ""


def build_daemon_command(
    *,
    agent_name: str = DEFAULT_AGENT,
    nats_url: str = NATS_URL,
    log_level: str | None = None,
    python_executable: str | None = None,
    entrypoint: str | None = None,
) -> list[str]:
    """Build the foreground daemon command used by auto-spawn helpers."""
    command = [
        python_executable or sys.executable,
        entrypoint or str(Path(PROJECT_ROOT) / "main.py"),
        "--daemon",
        "--name",
        agent_name,
        "--nats-url",
        nats_url,
    ]
    if log_level:
        command.extend(["--log-level", log_level])
    return command


def read_daemon_pid(pid_path: Path = DAEMON_PID_PATH) -> int | None:
    """Return the daemon pid from disk if the pid file is present and valid."""
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def write_daemon_pid(pid: int, pid_path: Path = DAEMON_PID_PATH) -> None:
    """Persist the daemon pid for later stop/status commands."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{pid}\n", encoding="utf-8")


def clear_daemon_pid(pid_path: Path = DAEMON_PID_PATH) -> None:
    """Remove the daemon pid file when the managed process stops."""
    with suppress(FileNotFoundError):
        pid_path.unlink()


def pid_is_running(pid: int) -> bool:
    """Return True when the target pid currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def daemon_healthcheck(
    *,
    health_url: str = DEFAULT_DAEMON_HEALTH_URL,
    timeout: float = 0.5,
) -> dict[str, Any] | None:
    """Fetch the daemon health payload when the local HTTP API is reachable."""
    try:
        response = requests.get(health_url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("status") != "ok":
        return None
    return payload


def wait_for_daemon_health(
    *,
    health_url: str = DEFAULT_DAEMON_HEALTH_URL,
    timeout: float = DEFAULT_DAEMON_START_TIMEOUT,
    poll_interval: float = DEFAULT_DAEMON_POLL_INTERVAL,
    process: subprocess.Popen[str] | None = None,
) -> dict[str, Any]:
    """Poll the daemon health endpoint until it reports ready or times out."""
    deadline = time.monotonic() + timeout
    request_timeout = max(0.1, min(1.0, poll_interval))
    while time.monotonic() < deadline:
        payload = daemon_healthcheck(health_url=health_url, timeout=request_timeout)
        if payload is not None:
            return payload
        if process is not None:
            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(
                    f"daemon exited before becoming healthy (exit code {exit_code})"
                )
        time.sleep(poll_interval)
    raise TimeoutError(f"daemon did not become healthy within {timeout:.1f}s")


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Best-effort shutdown for a detached daemon that failed to start cleanly."""
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)


def start_local_daemon(
    *,
    agent_name: str = DEFAULT_AGENT,
    nats_url: str = NATS_URL,
    log_level: str | None = None,
    health_url: str = DEFAULT_DAEMON_HEALTH_URL,
    remote_url: str = DEFAULT_DAEMON_WS_URL,
    pid_path: Path = DAEMON_PID_PATH,
    log_path: Path = DAEMON_LOG_PATH,
    startup_timeout: float = DEFAULT_DAEMON_START_TIMEOUT,
    poll_interval: float = DEFAULT_DAEMON_POLL_INTERVAL,
    python_executable: str | None = None,
    entrypoint: str | None = None,
) -> DaemonStartResult:
    """Start the local daemon in the background when it is not already reachable."""
    if daemon_healthcheck(health_url=health_url) is not None:
        pid = read_daemon_pid(pid_path)
        if pid is not None and not pid_is_running(pid):
            clear_daemon_pid(pid_path)
            pid = None
        return DaemonStartResult(
            remote_url=remote_url,
            pid=pid,
            already_running=True,
        )

    command = build_daemon_command(
        agent_name=agent_name,
        nats_url=nats_url,
        log_level=log_level,
        python_executable=python_executable,
        entrypoint=entrypoint,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

    try:
        wait_for_daemon_health(
            health_url=health_url,
            timeout=startup_timeout,
            poll_interval=poll_interval,
            process=process,
        )
    except Exception:
        _terminate_process(process)
        raise

    write_daemon_pid(process.pid, pid_path)
    return DaemonStartResult(
        remote_url=remote_url,
        pid=process.pid,
        already_running=False,
    )


def ensure_local_daemon_started(
    *,
    agent_name: str = DEFAULT_AGENT,
    nats_url: str = NATS_URL,
    log_level: str | None = None,
) -> str:
    """Return the local daemon websocket URL, starting the daemon if needed."""
    result = start_local_daemon(
        agent_name=agent_name,
        nats_url=nats_url,
        log_level=log_level,
    )
    return result.remote_url


def get_daemon_status(
    *,
    pid_path: Path = DAEMON_PID_PATH,
    health_url: str = DEFAULT_DAEMON_HEALTH_URL,
) -> DaemonStatus:
    """Summarize the daemon state for status output."""
    pid = read_daemon_pid(pid_path)
    if pid is not None and not pid_is_running(pid):
        clear_daemon_pid(pid_path)
        pid = None

    health = daemon_healthcheck(health_url=health_url)
    if health is not None and pid is not None:
        return DaemonStatus(
            running=True,
            healthy=True,
            managed=True,
            pid=pid,
            detail=f"running (pid {pid})",
        )
    if health is not None:
        return DaemonStatus(
            running=True,
            healthy=True,
            managed=False,
            detail="running (not managed by kaictl)",
        )
    if pid is not None:
        return DaemonStatus(
            running=True,
            healthy=False,
            managed=True,
            pid=pid,
            detail=f"pid {pid} exists but the health endpoint is unavailable",
        )
    return DaemonStatus(
        running=False,
        healthy=False,
        managed=False,
        detail="stopped",
    )


def stop_local_daemon(
    *,
    pid_path: Path = DAEMON_PID_PATH,
    health_url: str = DEFAULT_DAEMON_HEALTH_URL,
    timeout: float = DEFAULT_DAEMON_START_TIMEOUT,
    poll_interval: float = DEFAULT_DAEMON_POLL_INTERVAL,
) -> DaemonStopResult:
    """Stop the kaictl-managed daemon process if one is running."""
    pid = read_daemon_pid(pid_path)
    if pid is None:
        if daemon_healthcheck(health_url=health_url) is not None:
            return DaemonStopResult(
                stopped=False,
                already_stopped=False,
                detail="daemon is running but not managed by kaictl",
            )
        return DaemonStopResult(
            stopped=False,
            already_stopped=True,
            detail="daemon is already stopped",
        )

    if not pid_is_running(pid):
        clear_daemon_pid(pid_path)
        return DaemonStopResult(
            stopped=False,
            pid=pid,
            already_stopped=True,
            detail=f"removed stale pid file for {pid}",
        )

    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            clear_daemon_pid(pid_path)
            return DaemonStopResult(
                stopped=True,
                pid=pid,
                detail=f"stopped pid {pid}",
            )
        time.sleep(poll_interval)

    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            clear_daemon_pid(pid_path)
            return DaemonStopResult(
                stopped=True,
                pid=pid,
                detail=f"stopped pid {pid} with SIGKILL",
            )
        time.sleep(poll_interval)

    return DaemonStopResult(
        stopped=False,
        pid=pid,
        detail=f"failed to stop pid {pid}",
    )


def read_log_tail(
    *,
    log_path: Path = DAEMON_LOG_PATH,
    lines: int = 100,
) -> str:
    """Return the tail of the daemon log file."""
    if not log_path.exists():
        return ""
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        return "".join(handle.readlines()[-max(lines, 0):])


def _print_log_stream(*, log_path: Path, lines: int) -> int:
    """Print the daemon log tail and optionally follow new lines."""
    if not log_path.exists():
        print(f"No daemon log at {log_path}")
        return 1
    tail = read_log_tail(log_path=log_path, lines=lines)
    if tail:
        print(tail, end="")
    return 0


def build_cli_parser() -> argparse.ArgumentParser:
    """Build the kaictl CLI parser."""
    parser = argparse.ArgumentParser(prog="kaictl", description="Control the local kaid daemon")
    subcommands = parser.add_subparsers(dest="command", required=True)

    start = subcommands.add_parser("start", help="Start the local daemon if needed")
    start.add_argument("--name", default=DEFAULT_AGENT, help=f"Agent name (default: {DEFAULT_AGENT})")
    start.add_argument("--nats-url", default=NATS_URL, help=f"NATS URL (default: {NATS_URL})")
    start.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override log level from config",
    )

    subcommands.add_parser("stop", help="Stop the kaictl-managed daemon")
    subcommands.add_parser("status", help="Show the daemon status")

    logs = subcommands.add_parser("logs", help="Print the daemon log tail")
    logs.add_argument("-n", "--lines", type=int, default=100, help="Lines of log history to print")

    # Phase 1 (#10223): list agent runs from the taskboard agent_runs ledger.
    # Reads /api/tasks/{id}/agent-runs and /api/agent-runs?status=...; the
    # auth comes from TASKBOARD_BEARER_TOKEN env, the host from TASKBOARD_URL.
    runs = subcommands.add_parser(
        "runs",
        help="List agent_runs ledger entries (taskboard-side)",
    )
    runs_group = runs.add_mutually_exclusive_group(required=True)
    runs_group.add_argument(
        "--task",
        type=int,
        help="Show runs for a single task id",
    )
    runs_group.add_argument(
        "--status",
        type=str,
        help="Cross-task list filtered by status (e.g. running, endpoint_failed)",
    )
    runs.add_argument(
        "--role",
        type=str,
        default=None,
        help="Optional role filter (developer, code-reviewer, security-auditor, qa-agent, architect, orchestrator)",
    )
    runs.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Limit (1..200, default 50)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the kaictl command-line interface."""
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        result = start_local_daemon(
            agent_name=args.name,
            nats_url=args.nats_url,
            log_level=args.log_level,
        )
        if result.already_running:
            print(f"kaid is already running at {result.remote_url}")
        else:
            print(f"Started kaid (pid {result.pid}) at {result.remote_url}")
        return 0

    if args.command == "stop":
        result = stop_local_daemon()
        print(result.detail)
        return 0 if result.stopped or result.already_stopped else 1

    if args.command == "status":
        status = get_daemon_status()
        print(status.detail)
        return 0

    if args.command == "logs":
        return _print_log_stream(log_path=DAEMON_LOG_PATH, lines=args.lines)

    if args.command == "runs":
        return _print_agent_runs(
            task=args.task,
            status=args.status,
            role=args.role,
            limit=args.limit,
        )

    parser.error(f"unknown command: {args.command}")
    return 2


# ---------------------------------------------------------------------------
# `kaictl runs` — Phase 1 of epic #10028 (taskboard task #10223)
# ---------------------------------------------------------------------------


def _print_agent_runs(
    *,
    task: int | None,
    status: str | None,
    role: str | None,
    limit: int,
) -> int:
    """Render a table of agent_runs rows fetched from the taskboard."""
    from agent.agent_runs_client import AgentRunsClient

    client = AgentRunsClient.from_env()
    if not client.enabled:
        print(
            "kaictl runs requires TASKBOARD_URL + TASKBOARD_BEARER_TOKEN env "
            "vars (taskboard agent_runs ledger). See epic #10028."
        )
        return 1

    try:
        if task is not None:
            rows = client.list_for_task(task, role=role, status=status, limit=limit)
        else:
            rows = client.list_by_status(status, limit=limit) if status else None
    except ValueError as exc:
        print(f"invalid argument: {exc}")
        return 2

    if rows is None:
        print(
            "kaictl runs: failed to query the taskboard ledger "
            "(check TASKBOARD_URL and TASKBOARD_BEARER_TOKEN)"
        )
        return 1
    if not rows:
        print("(no runs)")
        return 0

    # Compact table for terminal use; switch to JSON if --json is added later.
    headers = (
        "id",
        "task",
        "role",
        "status",
        "fail",
        "started",
        "session",
    )
    widths = {h: len(h) for h in headers}
    formatted: list[dict[str, str]] = []
    for row in rows:
        cells = {
            "id": str(row.get("id", "")),
            "task": str(row.get("task_id", "")),
            "role": str(row.get("role", "")),
            "status": str(row.get("status", "")),
            "fail": str(row.get("failure_class") or "-"),
            "started": str(row.get("started_at") or row.get("created_at") or "")[:19],
            "session": str(row.get("session_id") or "-")[:48],
        }
        formatted.append(cells)
        for h in headers:
            widths[h] = max(widths[h], len(cells[h]))
    line = "  ".join(h.ljust(widths[h]) for h in headers)
    print(line)
    print("  ".join("-" * widths[h] for h in headers))
    for cells in formatted:
        print("  ".join(cells[h].ljust(widths[h]) for h in headers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

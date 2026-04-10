"""Helpers for starting and monitoring the local daemon process."""

from __future__ import annotations

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

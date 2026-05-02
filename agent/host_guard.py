"""Cross-host target verification and forbidden-host enforcement for shell actions."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
_VERIFY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify-host-target.sh"


@dataclass(frozen=True)
class HostTarget:
    """One parsed cross-host target discovered in a shell command."""

    tool: str
    host_expr: str
    host: str


@dataclass(frozen=True)
class HostGuardResult:
    """Result of forbidden-host validation and host verification."""

    blocked: bool
    output: str = ""
    exit_code: int | None = None


_REMOTE_SPEC_RE = re.compile(r"^(?:[^@\s]+@)?(?P<host>[^:/\s]+):.+$")


def parse_forbidden_hosts(value: str | None = None) -> set[str]:
    """Return normalized forbidden hosts from env/config text."""

    raw = value if value is not None else os.getenv("KAI_FORBIDDEN_HOSTS", "")
    hosts: set[str] = set()
    for part in re.split(r"[\s,]+", raw.strip()):
        cleaned = part.strip().lower()
        if cleaned:
            hosts.add(cleaned)
    return hosts


def is_local_host(host: str) -> bool:
    """Return whether a host string resolves to a localhost-style target."""

    value = (host or "").strip().lower().strip("[]")
    return value in _LOCAL_HOSTS


def extract_cross_host_targets(command: str) -> list[HostTarget]:
    """Extract non-localhost remote targets from a shell command string."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return []

    found: list[HostTarget] = []
    seen: set[tuple[str, str]] = set()

    def add(tool: str, host_expr: str, host: str) -> None:
        normalized_host = host.strip().lower().strip("[]")
        if not normalized_host or is_local_host(normalized_host):
            return
        key = (tool, normalized_host)
        if key in seen:
            return
        seen.add(key)
        found.append(HostTarget(tool=tool, host_expr=host_expr, host=normalized_host))

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "ssh":
            i += 1
            while i < len(tokens):
                candidate = tokens[i]
                if candidate == "--":
                    i += 1
                    break
                if candidate.startswith("-"):
                    if candidate in {"-b", "-c", "-D", "-E", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w"} and i + 1 < len(tokens):
                        i += 2
                        continue
                    i += 1
                    continue
                add("ssh", candidate, _host_from_ssh_target(candidate))
                break
        elif token == "scp":
            for candidate in tokens[i + 1 :]:
                if candidate in {"&&", "||", ";", "|"}:
                    break
                if candidate.startswith("-"):
                    continue
                remote_host = _host_from_scp_spec(candidate)
                if remote_host:
                    add("scp", candidate, remote_host)
        elif token == "curl":
            for candidate in tokens[i + 1 :]:
                if candidate.startswith("-"):
                    continue
                parsed = urlparse(candidate)
                if parsed.scheme in {"http", "https"} and parsed.hostname:
                    add("curl", candidate, parsed.hostname)
        elif token == "docker":
            if i + 1 < len(tokens) and tokens[i + 1] in {"-H", "--host"} and i + 2 < len(tokens):
                candidate = tokens[i + 2]
                host = _host_from_docker_host(candidate)
                if host:
                    add("docker", candidate, host)
        i += 1
    return found


def verify_command_targets(command: str) -> HostGuardResult:
    """Verify all cross-host targets and fail closed on forbidden hosts."""

    targets = extract_cross_host_targets(command)
    if not targets:
        return HostGuardResult(blocked=False, output="")

    forbidden = parse_forbidden_hosts()
    logs: list[str] = []
    for target in targets:
        if target.host in forbidden:
            logs.append(
                "[host-verify] blocked: target host "
                f"'{target.host}' is forbidden by KAI_FORBIDDEN_HOSTS"
            )
            return HostGuardResult(blocked=True, output="\n".join(logs), exit_code=2)
        logs.append(_run_verify_script(target.host_expr))
    return HostGuardResult(blocked=False, output="\n".join(logs).strip())


def _host_from_ssh_target(value: str) -> str:
    candidate = value.rsplit("@", 1)[-1]
    return candidate.strip().strip("[]")


def _host_from_scp_spec(value: str) -> str | None:
    match = _REMOTE_SPEC_RE.match(value)
    if not match:
        return None
    return match.group("host").strip().strip("[]")


def _host_from_docker_host(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.hostname:
        return parsed.hostname
    if "://" not in value and ":" in value:
        return value.rsplit(":", 1)[0].strip().strip("[]")
    stripped = value.strip().strip("[]")
    return stripped or None


def _run_verify_script(host_expr: str) -> str:
    result = subprocess.run(
        [str(_VERIFY_SCRIPT), host_expr],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout or ""
    if result.stderr:
        if output and not output.endswith("\n"):
            output += "\n"
        output += f"[stderr]\n{result.stderr}"
    output = output.strip()
    if result.returncode != 0:
        suffix = f"\n[exit code: {result.returncode}]"
        return f"{output}{suffix}" if output else suffix.lstrip("\n")
    return output

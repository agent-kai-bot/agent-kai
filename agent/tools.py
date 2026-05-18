"""Agent tools for the local AI agent."""

import asyncio
import io
import logging
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from html.parser import HTMLParser

import requests
from langchain_core.tools import StructuredTool

LOGGER = logging.getLogger("agent.tools")

from config import (
    DOCKER_SANDBOX_ALLOWED_NETWORKS,
    DOCKER_SANDBOX_CPUS,
    DOCKER_SANDBOX_DEFAULT_NETWORK,
    DOCKER_SANDBOX_DEFAULT_TIMEOUT,
    DOCKER_SANDBOX_IMAGE,
    DOCKER_SANDBOX_MAX_TIMEOUT,
    DOCKER_SANDBOX_MEMORY,
    DOCKER_SANDBOX_MOUNT_WORKSPACE_DEFAULT,
    DOCKER_SANDBOX_PIDS,
    DOCKER_SANDBOX_TMPFS_SIZE,
    DOCKER_SANDBOX_USER,
    VALID_REASONING_EFFORTS,
    get_max_file_read_chars,
    get_max_output_chars,
    get_shell_timeout_seconds,
    normalize_reasoning_effort,
)
from agent.runtime_utils import (
    current_session_worktree,
    session_subprocess_env,
    session_worktree_context,
)


def _truncate_output(output: str) -> str:
    limit = get_max_output_chars()
    if len(output) > limit:
        return output[:limit] + f"\n... [truncated at {limit} chars]"
    return output


def _truncate_read_text(text: str, *, prefix: str = "\n") -> str:
    limit = get_max_file_read_chars()
    if len(text) > limit:
        return text[:limit] + f"{prefix}... [truncated at {limit} chars]"
    return text


# ── File Read ────────────────────────────────────────────────

def _file_read(path: str) -> str:
    """Read the contents of a file at the given path."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return f"Error: '{path}' is not a file or does not exist."
    try:
        limit = get_max_file_read_chars()
        with open(path, "r", errors="replace") as f:
            content = f.read(limit)
        if len(content) == limit:
            content += f"\n\n... [truncated at {limit} chars]"
        return content
    except Exception as e:
        return f"Error reading file: {e}"


file_read = StructuredTool.from_function(
    func=_file_read,
    name="file_read",
    description="Read the contents of a file. Input: path (string).",
)


# ── File Write ───────────────────────────────────────────────

def _file_write(path: str, content: str) -> str:
    """Write content to a file, creating directories if needed."""
    path = os.path.expanduser(path)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


file_write = StructuredTool.from_function(
    func=_file_write,
    name="file_write",
    description="Write content to a file, creating parent directories if needed. Inputs: path (string), content (string).",
)


# ── File Edit ────────────────────────────────────────────────

def _file_edit(path: str, old_string: str, new_string: str) -> str:
    """Edit a file by replacing an exact string match with new content."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return f"Error: '{path}' does not exist."
    try:
        with open(path, "r") as f:
            content = f.read()
        count = content.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path}. Make sure it matches exactly (including whitespace)."
        if count > 1:
            return f"Error: old_string matches {count} times in {path}. Provide a longer/more unique string to match exactly once."
        new_content = content.replace(old_string, new_string, 1)
        with open(path, "w") as f:
            f.write(new_content)
        old_lines = old_string.count("\n") + 1
        new_lines = new_string.count("\n") + 1
        return f"Replaced {old_lines} line(s) with {new_lines} line(s) in {path}"
    except Exception as e:
        return f"Error editing file: {e}"


file_edit = StructuredTool.from_function(
    func=_file_edit,
    name="file_edit",
    description=(
        "Edit a file by replacing an exact string match. This is safer than rewriting the whole file. "
        "Inputs: path (string), old_string (the exact text to find — must match once), "
        "new_string (the replacement text). Use file_read first to see the current content."
    ),
)


# ── Shell Exec ───────────────────────────────────────────────

def _shell_exec(command: str) -> str:
    """Execute a shell command and return stdout + stderr."""
    timeout = get_shell_timeout_seconds()
    try:
        cwd = current_session_worktree() or None
        env = session_subprocess_env(worktree=cwd)
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        if not output.strip():
            output = "(no output)"
        return _truncate_output(output)
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error executing command: {e}"


shell_exec = StructuredTool.from_function(
    func=_shell_exec,
    name="shell_exec",
    description="Execute a shell command and return its output. Input: command (string).",
)


# ── Python Exec ──────────────────────────────────────────────

def _python_exec(code: str) -> str:
    """Execute Python code and return stdout output."""
    stdout_capture = io.StringIO()
    namespace = {"__builtins__": __builtins__}
    try:
        with redirect_stdout(stdout_capture):
            exec(code, namespace)
        output = stdout_capture.getvalue()
        if not output.strip():
            output = "(no output)"
        return _truncate_output(output)
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


python_exec = StructuredTool.from_function(
    func=_python_exec,
    name="python_exec",
    description="Execute Python code and return the printed output. Input: code (string).",
)


# ── Web Fetch ────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    """Simple HTML tag stripper."""

    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return " ".join(self._parts)


def _web_fetch(url: str) -> str:
    """Fetch a URL and return its text content (HTML tags stripped)."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "LocalAIAgent/1.0"})
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type:
            stripper = _HTMLStripper()
            stripper.feed(resp.text)
            text = stripper.get_text()
        else:
            text = resp.text
        return _truncate_read_text(text)
    except Exception as e:
        return f"Error fetching URL: {e}"


web_fetch = StructuredTool.from_function(
    func=_web_fetch,
    name="web_fetch",
    description="Fetch a URL and return its text content. HTML tags are stripped. Input: url (string).",
)


# ── Codex CLI (frontier model access) ────────────────────────

CODEX_PATH = os.path.expanduser("~/.npm-global/bin/codex")
CODEX_TIMEOUT = 28800  # 8 hours


def _codex_exec(prompt: str, working_directory: str = "") -> str:
    """Run a prompt through the OpenAI Codex CLI agent (frontier model)."""
    if not os.path.isfile(CODEX_PATH):
        return f"Error: codex CLI not found at {CODEX_PATH}"

    cmd = [CODEX_PATH, "exec", prompt, "--full-auto", "--skip-git-repo-check"]
    cwd = working_directory or current_session_worktree() or None
    if cwd and not os.path.isdir(cwd):
        return f"Error: directory '{cwd}' does not exist."

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CODEX_TIMEOUT,
            cwd=cwd,
            env=session_subprocess_env(worktree=current_session_worktree()),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        if not output.strip():
            output = "(no output)"
        return _truncate_output(output)
    except subprocess.TimeoutExpired:
        return f"Error: codex timed out after {CODEX_TIMEOUT}s"
    except Exception as e:
        return f"Error running codex: {e}"


codex_exec = StructuredTool.from_function(
    func=_codex_exec,
    name="codex_exec",
    description=(
        "Run a task through the OpenAI Codex CLI agent, which uses a frontier cloud model (e.g. o3). "
        "Use this for complex tasks that need a more powerful model: advanced coding, deep analysis, "
        "multi-file refactors, or anything beyond your local capabilities. "
        "Inputs: prompt (string, the task description), "
        "working_directory (string, optional, the directory to run in)."
    ),
)


# ── Claude CLI (frontier model access) ───────────────────────

CLAUDE_PATH = os.path.expanduser("~/.local/bin/claude")
CLAUDE_TIMEOUT = 28800  # 8 hours


def _claude_exec(prompt: str, working_directory: str = "", model: str = "") -> str:
    """Run a prompt through the Claude Code CLI agent (Anthropic frontier model)."""
    if not os.path.isfile(CLAUDE_PATH):
        return f"Error: claude CLI not found at {CLAUDE_PATH}"

    cmd = [CLAUDE_PATH, "-p", "--dangerously-skip-permissions", prompt]
    if model:
        cmd.extend(["--model", model])
    cwd = working_directory or current_session_worktree() or None
    if cwd and not os.path.isdir(cwd):
        return f"Error: directory '{cwd}' does not exist."

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=cwd,
            env=session_subprocess_env(worktree=current_session_worktree()),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        if not output.strip():
            output = "(no output)"
        return _truncate_output(output)
    except subprocess.TimeoutExpired:
        return f"Error: claude timed out after {CLAUDE_TIMEOUT}s"
    except Exception as e:
        return f"Error running claude: {e}"


claude_exec = StructuredTool.from_function(
    func=_claude_exec,
    name="claude_exec",
    description=(
        "Run a task through the Claude Code CLI agent (Anthropic frontier model, e.g. Claude Opus/Sonnet). "
        "Use this for complex tasks that need a more powerful model: advanced reasoning, large refactors, "
        "deep code analysis, or anything beyond your local capabilities. "
        "Inputs: prompt (string, the task description), "
        "working_directory (string, optional, the directory to run in), "
        "model (string, optional, e.g. 'sonnet' or 'opus')."
    ),
)


# ── Docker Sandbox ───────────────────────────────────────────
#
# Run arbitrary commands inside an ephemeral, locked-down container. This
# is the safe counterpart to ``shell_exec`` / ``python_exec`` for code the
# agent doesn't fully trust (output of frontier-model escalation,
# untrusted user input, pip installs that would otherwise pollute the
# host). Every run gets a fresh container, strict resource limits,
# dropped capabilities, no network by default, and auto-cleanup via
# ``--rm``. The root filesystem is read-only; a small tmpfs is mounted
# at ``/tmp`` for scratch writes.
#
# The tool is created per-agent via ``create_docker_sandbox_tool`` so
# the sub-agent's workspace directory can be bind-mounted as ``/work``
# without the LLM having to know the host path. Sub-agents write a
# file with ``file_write``, then run it in the sandbox — the output
# comes back the same way ``shell_exec`` does.

_DOCKER_PATH = shutil.which("docker")


def _docker_sandbox(
    command: str,
    image: str = DOCKER_SANDBOX_IMAGE,
    timeout: int = DOCKER_SANDBOX_DEFAULT_TIMEOUT,
    network: str = DOCKER_SANDBOX_DEFAULT_NETWORK,
    workdir: str | None = None,
    mount_workspace: bool = DOCKER_SANDBOX_MOUNT_WORKSPACE_DEFAULT,
    workspace_host_path: str | None = None,
) -> str:
    """Run a shell command inside a locked-down docker sandbox.

    Args:
        command: Shell command to run inside the container. Passed to ``sh -c``.
        image: Container image. Defaults to the configured sandbox image.
        timeout: Wall-clock timeout in seconds. Capped at the configured max.
        network: Docker network mode. Must be one of the allowed values
            (``none`` by default for offline runs, ``bridge`` to allow
            outbound network).
        workdir: Working directory inside the container. Defaults to
            ``/work`` when a workspace is mounted, ``/tmp`` otherwise
            (the tmpfs scratch area — the only writable place in a
            mount-less run).
        mount_workspace: If True and a workspace host path is available,
            bind-mount it as ``/work`` read-write.
        workspace_host_path: Bound at tool-creation time by
            ``create_docker_sandbox_tool``; callers normally don't set
            this directly.

    Returns:
        Combined stdout + stderr output with exit code, or an error
        string if docker is unavailable or the container failed to
        start.
    """
    if not _DOCKER_PATH:
        return "Error: docker CLI not found in PATH; cannot run docker_sandbox."

    if network not in DOCKER_SANDBOX_ALLOWED_NETWORKS:
        return (
            f"Error: network mode '{network}' is not allowed. "
            f"Must be one of: {', '.join(DOCKER_SANDBOX_ALLOWED_NETWORKS)}."
        )

    # Clamp the timeout so an LLM can't ask for an 8-hour run.
    timeout = max(1, min(int(timeout), DOCKER_SANDBOX_MAX_TIMEOUT))

    # Decide whether to mount the workspace. This also decides the
    # container user: with a mount we run as the host uid:gid so file
    # permissions on the bound directory work naturally; without a
    # mount we stay with the strict default (nobody) since there's
    # nothing on the host to interact with anyway.
    mount_arg: str | None = None
    if mount_workspace and workspace_host_path:
        host_path = os.path.abspath(os.path.expanduser(workspace_host_path))
        if not os.path.isdir(host_path):
            return (
                f"Error: workspace path '{host_path}' does not exist; "
                "cannot mount it into the sandbox."
            )
        mount_arg = f"--mount=type=bind,source={host_path},target=/work"

    if workdir is None:
        workdir = "/work" if mount_arg else "/tmp"

    if mount_arg:
        # Host-uid/gid so bind-mount files are readable + writable.
        # All the other hardening (cap-drop, no-new-privileges,
        # read-only root fs, network=none by default) still applies,
        # so the sandbox stays sandboxed.
        user_flag = f"--user={os.getuid()}:{os.getgid()}"
    else:
        user_flag = f"--user={DOCKER_SANDBOX_USER}"

    cmd: list[str] = [
        _DOCKER_PATH,
        "run",
        "--rm",
        # Harden the runtime.
        f"--network={network}",
        user_flag,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        f"--tmpfs=/tmp:rw,noexec,nosuid,size={DOCKER_SANDBOX_TMPFS_SIZE}",
        f"--memory={DOCKER_SANDBOX_MEMORY}",
        f"--cpus={DOCKER_SANDBOX_CPUS}",
        f"--pids-limit={DOCKER_SANDBOX_PIDS}",
        # Don't leak the host env into the container.
        "--env=HOME=/tmp",
        "--env=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"--workdir={workdir}",
    ]

    if mount_arg:
        cmd.append(mount_arg)

    cmd.extend([image, "sh", "-c", command])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            f"Error: sandbox timed out after {timeout}s. The container "
            "was killed; no output was captured."
        )
    except FileNotFoundError:
        return "Error: docker CLI not found; cannot run docker_sandbox."
    except Exception as exc:  # noqa: BLE001
        return f"Error launching sandbox: {exc}"

    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    if result.returncode != 0:
        parts.append(f"[exit code: {result.returncode}]")
    output = "\n".join(p for p in parts if p).rstrip()
    if not output:
        output = "(no output)"
    return _truncate_output(output)


def create_docker_sandbox_tool(workspace_host_path: str | None = None):
    """Create a ``docker_sandbox`` tool bound to an optional workspace path.

    The returned tool bakes in the sub-agent's workspace so the LLM
    never has to know (or be trusted with) a host filesystem path. When
    the main agent calls this without a workspace, sandbox runs are
    fully isolated with only a tmpfs scratch area.
    """

    def _run(
        command: str,
        image: str = DOCKER_SANDBOX_IMAGE,
        timeout: int = DOCKER_SANDBOX_DEFAULT_TIMEOUT,
        network: str = DOCKER_SANDBOX_DEFAULT_NETWORK,
    ) -> str:
        return _docker_sandbox(
            command=command,
            image=image,
            timeout=timeout,
            network=network,
            mount_workspace=bool(workspace_host_path),
            workspace_host_path=workspace_host_path,
        )

    description = (
        "Run a shell command inside a locked-down, ephemeral docker container. "
        "Use this instead of shell_exec when running untrusted code, frontier-model "
        "output, pip installs, or anything you don't want touching the host. "
        "The container is auto-removed on exit, runs as nobody with dropped "
        f"capabilities and a read-only root fs, uses {DOCKER_SANDBOX_MEMORY} memory / "
        f"{DOCKER_SANDBOX_CPUS} CPU, and has no outbound network by default. "
        "Inputs: "
        "command (string, shell command to run via sh -c), "
        f"image (string, optional, default '{DOCKER_SANDBOX_IMAGE}'), "
        f"timeout (int, optional, default {DOCKER_SANDBOX_DEFAULT_TIMEOUT}s, max {DOCKER_SANDBOX_MAX_TIMEOUT}s), "
        f"network (string, optional, '{DOCKER_SANDBOX_DEFAULT_NETWORK}' for isolated, "
        "'bridge' for outbound network access)."
    )
    if workspace_host_path:
        description += (
            " The agent's workspace is bind-mounted read-write at /work so files "
            "written with file_write appear inside the container."
        )
    else:
        description += (
            " No workspace mount is configured — the container is fully isolated "
            "with only a small tmpfs at /tmp for scratch writes."
        )

    return StructuredTool.from_function(
        func=_run,
        name="docker_sandbox",
        description=description,
    )


# ── NATS Publish ─────────────────────────────────────────────

def create_nats_publish_tool(bus):
    """Create the nats_publish tool bound to a NatsBus instance."""

    def _nats_publish_sync(subject: str, message: str) -> str:
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(
                bus.publish(subject, {"message": message, "from": bus.agent_name}),
                loop,
            )
            future.result(timeout=5)
            return f"Published to '{subject}'"
        except Exception as e:
            return f"Error publishing to NATS: {e}"

    async def _nats_publish_async(subject: str, message: str) -> str:
        try:
            await bus.publish(subject, {"message": message, "from": bus.agent_name})
            return f"Published to '{subject}'"
        except Exception as e:
            return f"Error publishing to NATS: {e}"

    return StructuredTool.from_function(
        func=_nats_publish_sync,
        coroutine=_nats_publish_async,
        name="nats_publish",
        description="Publish a message to the NATS message bus. Inputs: subject (string, e.g. 'agent.researcher.request'), message (string).",
    )


# ── NATS Request (send task to agent, wait for reply) ────────

# Default wall-clock timeout for nats_request. 8 hours matches the
# codex_exec / claude_exec frontier-escalation tools (CODEX_TIMEOUT,
# CLAUDE_TIMEOUT) — sub-agents doing real review work can legitimately
# run for hours when chained through fallback endpoints, multi-step
# reasoning at xhigh effort, or large backtest sweeps. The previous
# 120s default was producing false "timed out" errors on routine
# analyst reviews. The hard cap and the default are now identical
# because there's no compelling reason to pick a smaller default
# than the max — if you really want a faster bail-out, pass an
# explicit timeout_seconds.
NATS_REQUEST_DEFAULT_TIMEOUT = 28800.0  # 8 hours
NATS_REQUEST_MAX_TIMEOUT = 28800.0      # 8 hours


def _clamp_request_timeout(value: float | int | None) -> float:
    """Clamp the user-supplied timeout into the allowed range."""
    if value is None or value <= 0:
        return NATS_REQUEST_DEFAULT_TIMEOUT
    return float(min(NATS_REQUEST_MAX_TIMEOUT, max(1.0, value)))


def create_nats_request_tool(bus):
    """Create the nats_request tool bound to a NatsBus instance."""

    def _nats_request_sync(
        agent_name: str,
        task: str,
        timeout_seconds: int = 0,
    ) -> str:
        timeout = _clamp_request_timeout(timeout_seconds)
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(
                bus.request(
                    f"agent.{agent_name}.request",
                    {"task": task, "from": bus.agent_name},
                    timeout=timeout,
                ),
                loop,
            )
            # The outer future timeout adds a small buffer over the
            # inner bus.request timeout so the inner one wins on a
            # natural timeout (clearer error message).
            result = future.result(timeout=timeout + 5)
            return result.get("response", str(result))
        except Exception as e:
            # Log the raw exception class+message so we can root-cause from
            # the daemon log instead of relying on the LLM's paraphrase.
            # Common case: NoRespondersError == sub-agent never spawned /
            # subscription not active yet.
            LOGGER.warning(
                "nats_request to agent=%s failed: %s: %s",
                agent_name,
                type(e).__name__,
                e,
            )
            return f"Error requesting from agent '{agent_name}': {type(e).__name__}: {e}"

    async def _nats_request_async(
        agent_name: str,
        task: str,
        timeout_seconds: int = 0,
    ) -> str:
        timeout = _clamp_request_timeout(timeout_seconds)
        try:
            result = await bus.request(
                f"agent.{agent_name}.request",
                {"task": task, "from": bus.agent_name},
                timeout=timeout,
            )
            return result.get("response", str(result))
        except Exception as e:
            LOGGER.warning(
                "nats_request to agent=%s failed: %s: %s",
                agent_name,
                type(e).__name__,
                e,
            )
            return f"Error requesting from agent '{agent_name}': {type(e).__name__}: {e}"

    return StructuredTool.from_function(
        func=_nats_request_sync,
        coroutine=_nats_request_async,
        name="nats_request",
        description=(
            "Send a task to a named sub-agent and wait for its reply. "
            "The agent must already be running — call spawn_agent first "
            "if it isn't. The reply is delivered synchronously over NATS "
            "request/reply, so this is the ONLY way to receive a sub-agent's "
            "output (do NOT pass tasks via spawn_agent — that path is "
            "fire-and-forget and the reply is lost).\n\n"
            "Inputs:\n"
            "  agent_name (string) — e.g. 'analyst', 'trader', 'risk-manager'\n"
            "  task (string) — the work the sub-agent should do\n"
            f"  timeout_seconds (int, optional) — wall-clock timeout. "
            f"Default {int(NATS_REQUEST_DEFAULT_TIMEOUT)}s (8 hours — same as the "
            f"codex/claude escalation tools), max {int(NATS_REQUEST_MAX_TIMEOUT)}s. "
            "Pass a smaller value if you want a faster bail-out (e.g. "
            "timeout_seconds=600 for a 10-minute cap on a routine query). "
            "Most callers should leave this unset and trust the default."
        ),
    )


# ── Spawn Agent ──────────────────────────────────────────────

def create_spawn_agent_tool(sub_agent_manager):
    """Create the spawn_agent tool bound to a SubAgentManager.

    The spawn tool DELIBERATELY does not accept an initial task. The
    earlier signature took a ``task`` argument that was dispatched via
    ``bus.publish`` (fire-and-forget) — the LLM expected a reply,
    didn't get one, then sent a follow-up via ``nats_request`` while
    the sub-agent was still busy with the published task. The
    follow-up wall-clocked past the request timeout because the
    sub-agent was queued. Net effect: the LLM saw "request timed
    out" even though the work succeeded — except the response went
    to a topic nobody was listening on.

    The fix is to remove the initial-task parameter entirely so the
    LLM is forced into the only correct pattern:

        spawn_agent(name)            # spawn returns when ready
        nats_request(name, task)     # explicit request/reply, gets the response back
    """

    def _spawn_sync(name: str) -> str:
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(
                sub_agent_manager.spawn(name),
                loop,
            )
            return future.result(timeout=10)
        except Exception as e:
            LOGGER.warning(
                "spawn_agent name=%s failed: %s: %s",
                name,
                type(e).__name__,
                e,
            )
            return f"Error spawning agent: {type(e).__name__}: {e}"

    async def _spawn_async(name: str) -> str:
        try:
            return await sub_agent_manager.spawn(name)
        except Exception as e:
            LOGGER.warning(
                "spawn_agent name=%s failed: %s: %s",
                name,
                type(e).__name__,
                e,
            )
            return f"Error spawning agent: {type(e).__name__}: {e}"

    return StructuredTool.from_function(
        func=_spawn_sync,
        coroutine=_spawn_async,
        name="spawn_agent",
        description=(
            "Spawn a background sub-agent that listens on NATS for tasks. "
            "Returns when the sub-agent is ready to receive requests.\n\n"
            "IMPORTANT: this does NOT send a task to the new agent. To "
            "actually run work on the sub-agent, call nats_request(name, "
            "task) immediately after spawn_agent returns. spawn_agent only "
            "sets up the sub-agent process and its NATS subscription — "
            "task delivery and reply handling are nats_request's job.\n\n"
            "Idempotent: spawning an already-running agent is a no-op.\n\n"
            "Inputs:\n"
            "  name (string) — unique sub-agent name like 'analyst', 'trader'"
        ),
    )


# ── List Agents ──────────────────────────────────────────────

def create_list_agents_tool(sub_agent_manager):
    """Create the list_agents tool."""

    def _list_agents() -> str:
        agents = sub_agent_manager.agents
        if not agents:
            # Show available predefined agents from config
            from config import AGENTS
            predefined = [f"  {name}: {cfg.get('description', '')}"
                          for name, cfg in AGENTS.items() if name != "kai"]
            lines = ["No sub-agents running.", "", "Available predefined agents (use spawn_agent):"]
            lines.extend(predefined)
            return "\n".join(lines)
        lines = ["Running sub-agents:"]
        for name, agent in agents.items():
            ws = agent.workspace or "none"
            lines.append(f"  {name} (workspace: {ws})")
        return "\n".join(lines)

    return StructuredTool.from_function(
        func=_list_agents,
        name="list_agents",
        description="List all currently running sub-agents, or show available predefined agents if none are running.",
    )


# ── Tool Registry ────────────────────────────────────────────

def _get_crypto_tools(signal_consumer=None):
    """Import and return crypto tools (lazy to avoid import errors if data_api not running).

    Args:
        signal_consumer: Optional ``SignalConsumer`` instance. When
            provided, the ``get_signals`` tool is appended so agents
            can query the live signal feed.
    """
    try:
        from agent.crypto_tools import ALL_CRYPTO_TOOLS, create_get_signals_tool
        tools = list(ALL_CRYPTO_TOOLS)
        if signal_consumer is not None:
            tools.append(create_get_signals_tool(signal_consumer))
        # Backtest tool — lets agents validate TA strategies over historical data
        try:
            from agent.backtest_tool import run_backtest
            tools.append(run_backtest)
        except Exception:
            pass
        return tools
    except Exception:
        return []


def _format_scheduled_job_summary(job) -> str:
    next_run = job.next_run or ("event-driven" if job.type == "event" else "n/a")
    route = ""
    overrides = getattr(job, "routing_overrides", lambda: {})()
    if overrides:
        parts = []
        if overrides.get("target_agent_role"):
            parts.append(f"target_agent_role={overrides['target_agent_role']}")
        if overrides.get("reasoning_effort") or overrides.get("thinking_level"):
            parts.append(
                f"reasoning_effort={overrides.get('reasoning_effort') or overrides.get('thinking_level')}"
            )
        if parts:
            route = " " + " ".join(parts)
    return (
        f"{job.id} [{job.status}] session={job.owner_session} "
        f"type={job.type} next={next_run}{route} prompt={job.prompt}"
    )


def create_scheduler_tools(scheduler, session):
    """Create scheduler-management tools bound to the current session."""
    session_obj = session

    def _resolve_session(target_session: str | None = None) -> str:
        name = target_session or getattr(session_obj, "name", "")
        return str(name).strip()

    def _validate_scheduler_overrides(
        *,
        reasoning_effort: str | None = None,
        thinking_level: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[str | None, str | None, dict[str, str] | None]:
        normalized: dict[str, str] = {}
        for field_name, raw_value in (
            ("reasoning_effort", reasoning_effort),
            ("thinking_level", thinking_level),
        ):
            if raw_value is None:
                continue
            canonical = normalize_reasoning_effort(raw_value)
            if canonical is None:
                raise ValueError(
                    f"invalid {field_name} '{raw_value}'; valid: {', '.join(VALID_REASONING_EFFORTS)}"
                )
            normalized[field_name] = canonical
        if (
            normalized.get("reasoning_effort") is not None
            and normalized.get("thinking_level") is not None
            and normalized["reasoning_effort"] != normalized["thinking_level"]
        ):
            raise ValueError("reasoning_effort and thinking_level must match when both are set")
        normalized_env = None
        if extra_env is not None:
            if not isinstance(extra_env, dict):
                raise ValueError("extra_env must be an object")
            normalized_env = {
                str(key): str(value)
                for key, value in extra_env.items()
                if key and value is not None
            }
        return (
            normalized.get("reasoning_effort"),
            normalized.get("thinking_level"),
            normalized_env,
        )

    def _loop_guard(job_type: str, target_session: str, prompt: str, spec: dict | None = None) -> str | None:
        if getattr(session_obj, "current_source", "user") != "scheduler":
            return None
        current_job_id = getattr(session_obj, "current_job_id", None)
        current_job = scheduler.get_job(current_job_id) if current_job_id else None
        if current_job is None:
            return None
        if current_job.owner_session != target_session or current_job.type != job_type:
            return None
        if current_job.prompt.strip() != prompt.strip():
            return None
        if spec is not None and job_type != "absolute" and current_job.spec != spec:
            return None
        return (
            f"Refusing to create a likely self-scheduling loop from job {current_job.id}. "
            "Confirm it manually with /schedule add if you really want it."
        )

    def _schedule_at(
        when: str,
        prompt: str,
        session: str | None = None,
        tool_budget: int | None = None,
        target_agent_role: str | None = None,
        reasoning_effort: str | None = None,
        thinking_level: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        owner_session = _resolve_session(session)
        normalized_reasoning, normalized_thinking, normalized_env = _validate_scheduler_overrides(
            reasoning_effort=reasoning_effort,
            thinking_level=thinking_level,
            extra_env=extra_env,
        )
        warning = _loop_guard("absolute", owner_session, prompt)
        if warning:
            return warning
        job = scheduler.create_absolute_job(
            when=when,
            prompt=prompt,
            owner_session=owner_session,
            created_by="agent",
            tool_budget=tool_budget,
            target_agent_role=target_agent_role,
            reasoning_effort=normalized_reasoning,
            thinking_level=normalized_thinking,
            extra_env=normalized_env,
        )
        return f"Scheduled {job.id} at {job.next_run} for session {job.owner_session}."

    def _schedule_recurring(
        cron: str,
        prompt: str,
        session: str | None = None,
        max_runs: int | None = None,
        tool_budget: int | None = None,
        target_agent_role: str | None = None,
        reasoning_effort: str | None = None,
        thinking_level: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        owner_session = _resolve_session(session)
        normalized_reasoning, normalized_thinking, normalized_env = _validate_scheduler_overrides(
            reasoning_effort=reasoning_effort,
            thinking_level=thinking_level,
            extra_env=extra_env,
        )
        spec = {"cron": cron, "tz": scheduler.timezone_name}
        warning = _loop_guard("cron", owner_session, prompt, spec)
        if warning:
            return warning
        job = scheduler.create_recurring_job(
            cron=cron,
            prompt=prompt,
            owner_session=owner_session,
            created_by="agent",
            max_runs=max_runs,
            tool_budget=tool_budget,
            target_agent_role=target_agent_role,
            reasoning_effort=normalized_reasoning,
            thinking_level=normalized_thinking,
            extra_env=normalized_env,
        )
        return f"Scheduled recurring job {job.id} next={job.next_run} for session {job.owner_session}."

    def _schedule_when(
        condition: dict,
        prompt: str,
        session: str | None = None,
        max_runs: int | None = None,
        tool_budget: int | None = None,
        target_agent_role: str | None = None,
        reasoning_effort: str | None = None,
        thinking_level: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        owner_session = _resolve_session(session)
        normalized_reasoning, normalized_thinking, normalized_env = _validate_scheduler_overrides(
            reasoning_effort=reasoning_effort,
            thinking_level=thinking_level,
            extra_env=extra_env,
        )
        spec = {
            "channel": condition.get("channel"),
            "filter": condition.get("filter"),
        }
        warning = _loop_guard("event", owner_session, prompt, spec)
        if warning:
            return warning
        job = scheduler.create_event_job(
            condition=condition,
            prompt=prompt,
            owner_session=owner_session,
            created_by="agent",
            max_runs=max_runs,
            tool_budget=tool_budget,
            target_agent_role=target_agent_role,
            reasoning_effort=normalized_reasoning,
            thinking_level=normalized_thinking,
            extra_env=normalized_env,
        )
        return f"Scheduled event job {job.id} on channel {job.spec['channel']} for session {job.owner_session}."

    def _list_scheduled_jobs(target_session: str | None = None) -> str:
        session_name = _resolve_session(target_session) if target_session is not None else getattr(session_obj, "name", "")
        jobs = [
            job
            for job in scheduler.list_jobs_for_session(session_name)
            if job.status in {"active", "paused"}
        ]
        if not jobs:
            return f"No scheduled jobs for session {session_name}."
        return "\n".join(_format_scheduled_job_summary(job) for job in jobs)

    def _cancel_scheduled_job(job_id: str) -> str:
        job = scheduler.get_job(job_id)
        if job is None:
            return f"Scheduled job {job_id} was not found."
        updated = scheduler.cancel_job(job_id)
        return f"Cancelled scheduled job {updated.id}."

    def _pause_scheduled_job(job_id: str) -> str:
        job = scheduler.get_job(job_id)
        if job is None:
            return f"Scheduled job {job_id} was not found."
        updated = scheduler.pause_job(job_id)
        return f"Paused scheduled job {updated.id}."

    def _resume_scheduled_job(job_id: str) -> str:
        job = scheduler.get_job(job_id)
        if job is None:
            return f"Scheduled job {job_id} was not found."
        updated = scheduler.resume_job(job_id)
        return f"Resumed scheduled job {updated.id}."

    return [
        StructuredTool.from_function(
            func=_schedule_at,
            name="schedule_at",
            description=(
                "Schedule a one-shot prompt at an ISO 8601 timestamp. Inputs: when, prompt, optional "
                "session, tool_budget, target_agent_role, reasoning_effort, thinking_level, extra_env."
            ),
        ),
        StructuredTool.from_function(
            func=_schedule_recurring,
            name="schedule_recurring",
            description=(
                "Schedule a recurring prompt on a cron expression. Inputs: cron, prompt, optional "
                "session, max_runs, tool_budget, target_agent_role, reasoning_effort, thinking_level, extra_env."
            ),
        ),
        StructuredTool.from_function(
            func=_schedule_when,
            name="schedule_when",
            description=(
                "Schedule a prompt on an event condition. Inputs: condition object, prompt, optional "
                "session, max_runs, tool_budget, target_agent_role, reasoning_effort, thinking_level, extra_env."
            ),
        ),
        StructuredTool.from_function(
            func=_list_scheduled_jobs,
            name="list_scheduled_jobs",
            description="List scheduled jobs for the current session or an optional session override.",
        ),
        StructuredTool.from_function(
            func=_cancel_scheduled_job,
            name="cancel_scheduled_job",
            description="Cancel a scheduled job by id.",
        ),
        StructuredTool.from_function(
            func=_pause_scheduled_job,
            name="pause_scheduled_job",
            description="Pause a scheduled job by id.",
        ),
        StructuredTool.from_function(
            func=_resume_scheduled_job,
            name="resume_scheduled_job",
            description="Resume a scheduled job by id.",
        ),
    ]


def create_chart_view_tools(session):
    """Create chart-view tools bound to the current daemon session.

    Args:
        session: Daemon ``Session`` instance whose UI state should be
            inspected or changed.

    Returns:
        Tools that let the agent inspect and mutate the user's chart.
    """

    def _get_chart_view() -> dict:
        """Return the current chart view for this session."""
        return session.chart_view_payload()

    def _set_chart_view(
        symbol: str | None = None,
        timeframe: str | None = None,
        source: str | None = None,
        mode: str | None = None,
    ) -> dict:
        """Update the current chart view for this session.

        Args:
            symbol: Optional chart symbol such as ``BTC`` or ``ETH``.
            timeframe: Optional timeframe such as ``1m``, ``15m`` or ``1h``.
            source: Optional source, currently ``kai-api`` or ``coinbase``.
            mode: Optional layout mode: ``full``, ``half``, ``mini`` or
                ``hide``.

        Returns:
            Updated chart-view payload.
        """
        chart = session.set_chart_view(
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            mode=mode,
        )
        session.save()
        return chart

    return [
        StructuredTool.from_function(
            func=_get_chart_view,
            name="get_chart_view",
            description=(
                "Return the user's current chart view. Use this before "
                "answering questions about the visible chart or before "
                "changing only one chart field."
            ),
        ),
        StructuredTool.from_function(
            func=_set_chart_view,
            name="set_chart_view",
            description=(
                "Change the user's visible chart in the web UI. Inputs are "
                "optional and can be combined: symbol (e.g. BTC, ETH, "
                "SOL), timeframe (1m, 5m, 15m, 1h, 4h, 1d, 1w), source "
                "(kai-api or coinbase), and mode (full, half, mini, hide). "
                "Use this when the user asks to switch symbols, timeframes, "
                "sources, or chart size."
            ),
        ),
    ]


def create_watchlist_tools(session):
    """Create watchlist tools bound to the current daemon session.

    Args:
        session: Daemon ``Session`` instance whose watchlist state should be
            inspected or changed.

    Returns:
        Tools that let the agent inspect and mutate the user's watchlist.
    """

    def _get_watchlist() -> dict:
        """Return the current watchlist for this session."""
        return session.watchlist_payload()

    def _add_watchlist_symbol(symbol: str) -> dict:
        """Add one symbol to the user's watchlist."""
        watchlist = session.add_watchlist_symbol(symbol)
        session.save()
        return watchlist

    def _remove_watchlist_symbol(symbol: str) -> dict:
        """Remove one symbol from the user's watchlist."""
        watchlist = session.remove_watchlist_symbol(symbol)
        session.save()
        return watchlist

    def _set_watchlist(symbols: list[str]) -> dict:
        """Replace the user's watchlist with a normalized symbol list."""
        watchlist = session.set_watchlist_symbols(symbols)
        session.save()
        return watchlist

    return [
        StructuredTool.from_function(
            func=_get_watchlist,
            name="get_watchlist",
            description="Return the user's current web UI watchlist symbols.",
        ),
        StructuredTool.from_function(
            func=_add_watchlist_symbol,
            name="add_watchlist_symbol",
            description=(
                "Add a symbol to the user's web UI watchlist. Input: symbol "
                "(for example BTC, ETH, SOL, or BIO). Use when the user asks "
                "to add a coin/token to the watchlist."
            ),
        ),
        StructuredTool.from_function(
            func=_remove_watchlist_symbol,
            name="remove_watchlist_symbol",
            description=(
                "Remove a symbol from the user's web UI watchlist. Input: "
                "symbol."
            ),
        ),
        StructuredTool.from_function(
            func=_set_watchlist,
            name="set_watchlist",
            description=(
                "Replace the user's web UI watchlist. Input: symbols, a list "
                "of symbols."
            ),
        ),
    ]


def create_tools(
    bus=None,
    sub_agent_manager=None,
    signal_consumer=None,
    scheduler=None,
    session=None,
):
    """Create and return all agent tools."""
    from agent.forgejo_tools import create_forgejo_tools
    from agent.sdlc_results import create_sdlc_result_tools
    from agent.strategy_agent_tools import create_strategy_tools
    from agent.taskboard_tools import create_taskboard_tools

    tools = [
        file_read,
        file_write,
        file_edit,
        shell_exec,
        python_exec,
        web_fetch,
        codex_exec,
        claude_exec,
    ]
    # Main agent ("kai") has no workspace, so the sandbox is fully isolated.
    tools.append(create_docker_sandbox_tool(workspace_host_path=None))
    tools.extend(_get_crypto_tools(signal_consumer=signal_consumer))
    if session is not None:
        tools.extend(create_strategy_tools(session))
        tools.extend(create_chart_view_tools(session))
        tools.extend(create_watchlist_tools(session))
    if bus:
        tools.append(create_nats_publish_tool(bus))
        tools.append(create_nats_request_tool(bus))
    if sub_agent_manager:
        tools.append(create_spawn_agent_tool(sub_agent_manager))
        tools.append(create_list_agents_tool(sub_agent_manager))
    if scheduler is not None and session is not None:
        tools.extend(create_scheduler_tools(scheduler, session))
    tools.extend(create_taskboard_tools(getattr(session, "taskboard_context", None)))
    tools.extend(create_forgejo_tools(getattr(session, "forgejo_context", None)))
    tools.extend(create_sdlc_result_tools())
    from agent.polymarket_tools import create_polymarket_tools
    tools.extend(create_polymarket_tools())
    return tools


def create_sub_agent_tools(bus, workspace_host_path: str | None = None, signal_consumer=None):
    """Create the limited toolset for sub-agents (no spawning).

    Args:
        bus: NATS bus instance (or None for bus-less runs).
        workspace_host_path: Absolute host path to the sub-agent's
            workspace directory. When provided, the docker_sandbox tool
            bind-mounts it as ``/work`` so the agent can write files via
            ``file_write`` and run them sandboxed in the same step.
        signal_consumer: Optional ``SignalConsumer`` for the get_signals tool.
    """
    from agent.forgejo_tools import create_forgejo_tools
    from agent.sdlc_results import create_sdlc_result_tools
    from agent.taskboard_tools import create_taskboard_tools

    tools = [
        file_read,
        file_write,
        file_edit,
        shell_exec,
        python_exec,
        web_fetch,
        codex_exec,
        claude_exec,
    ]
    tools.append(create_docker_sandbox_tool(workspace_host_path=workspace_host_path))
    tools.extend(_get_crypto_tools(signal_consumer=signal_consumer))
    if bus:
        tools.append(create_nats_publish_tool(bus))
    tools.extend(create_taskboard_tools())
    tools.extend(create_forgejo_tools())
    tools.extend(create_sdlc_result_tools())
    return tools

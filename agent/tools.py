"""Agent tools for the local AI agent."""

import asyncio
import io
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from html.parser import HTMLParser

import requests
from langchain_core.tools import StructuredTool

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
    MAX_FILE_READ_CHARS,
    MAX_OUTPUT_CHARS,
    SHELL_TIMEOUT_SECONDS,
)


# ── File Read ────────────────────────────────────────────────

def _file_read(path: str) -> str:
    """Read the contents of a file at the given path."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return f"Error: '{path}' is not a file or does not exist."
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read(MAX_FILE_READ_CHARS)
        if len(content) == MAX_FILE_READ_CHARS:
            content += f"\n\n... [truncated at {MAX_FILE_READ_CHARS} chars]"
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
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
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
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated at {MAX_OUTPUT_CHARS} chars]"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {SHELL_TIMEOUT_SECONDS}s"
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
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated at {MAX_OUTPUT_CHARS} chars]"
        return output
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
        if len(text) > MAX_FILE_READ_CHARS:
            text = text[:MAX_FILE_READ_CHARS] + f"\n... [truncated at {MAX_FILE_READ_CHARS} chars]"
        return text
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
    cwd = working_directory or None
    if cwd and not os.path.isdir(cwd):
        return f"Error: directory '{cwd}' does not exist."

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CODEX_TIMEOUT,
            cwd=cwd,
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
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated at {MAX_OUTPUT_CHARS} chars]"
        return output
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
    cwd = working_directory or None
    if cwd and not os.path.isdir(cwd):
        return f"Error: directory '{cwd}' does not exist."

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=cwd,
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
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated at {MAX_OUTPUT_CHARS} chars]"
        return output
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
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated at {MAX_OUTPUT_CHARS} chars]"
    return output


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

def create_nats_request_tool(bus):
    """Create the nats_request tool bound to a NatsBus instance."""

    def _nats_request_sync(agent_name: str, task: str) -> str:
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(
                bus.request(
                    f"agent.{agent_name}.request",
                    {"task": task, "from": bus.agent_name},
                    timeout=120.0,
                ),
                loop,
            )
            result = future.result(timeout=125)
            return result.get("response", str(result))
        except Exception as e:
            return f"Error requesting from agent '{agent_name}': {e}"

    async def _nats_request_async(agent_name: str, task: str) -> str:
        try:
            result = await bus.request(
                f"agent.{agent_name}.request",
                {"task": task, "from": bus.agent_name},
                timeout=120.0,
            )
            return result.get("response", str(result))
        except Exception as e:
            return f"Error requesting from agent '{agent_name}': {e}"

    return StructuredTool.from_function(
        func=_nats_request_sync,
        coroutine=_nats_request_async,
        name="nats_request",
        description=(
            "Send a task to a named sub-agent and wait for its reply. "
            "The agent must already be running (use spawn_agent first). "
            "Inputs: agent_name (string, e.g. 'researcher'), task (string, the task description)."
        ),
    )


# ── Spawn Agent ──────────────────────────────────────────────

def create_spawn_agent_tool(sub_agent_manager):
    """Create the spawn_agent tool bound to a SubAgentManager."""

    def _spawn_sync(name: str, task: str = "", system_prompt: str = "") -> str:
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(
                sub_agent_manager.spawn(
                    name,
                    system_prompt=system_prompt or None,
                    initial_task=task or None,
                ),
                loop,
            )
            return future.result(timeout=10)
        except Exception as e:
            return f"Error spawning agent: {e}"

    async def _spawn_async(name: str, task: str = "", system_prompt: str = "") -> str:
        try:
            return await sub_agent_manager.spawn(
                name,
                system_prompt=system_prompt or None,
                initial_task=task or None,
            )
        except Exception as e:
            return f"Error spawning agent: {e}"

    return StructuredTool.from_function(
        func=_spawn_sync,
        coroutine=_spawn_async,
        name="spawn_agent",
        description=(
            "Spawn a background sub-agent that listens on NATS for tasks. "
            "Inputs: name (string, unique name like 'researcher' or 'coder'), "
            "task (string, optional initial task to send it), "
            "system_prompt (string, optional custom system prompt)."
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


def create_tools(bus=None, sub_agent_manager=None, signal_consumer=None):
    """Create and return all agent tools."""
    tools = [file_read, file_write, file_edit, shell_exec, python_exec, web_fetch, codex_exec, claude_exec]
    # Main agent ("kai") has no workspace, so the sandbox is fully isolated.
    tools.append(create_docker_sandbox_tool(workspace_host_path=None))
    tools.extend(_get_crypto_tools(signal_consumer=signal_consumer))
    if bus:
        tools.append(create_nats_publish_tool(bus))
        tools.append(create_nats_request_tool(bus))
    if sub_agent_manager:
        tools.append(create_spawn_agent_tool(sub_agent_manager))
        tools.append(create_list_agents_tool(sub_agent_manager))
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
    tools = [file_read, file_write, file_edit, shell_exec, python_exec, web_fetch, codex_exec, claude_exec]
    tools.append(create_docker_sandbox_tool(workspace_host_path=workspace_host_path))
    tools.extend(_get_crypto_tools(signal_consumer=signal_consumer))
    if bus:
        tools.append(create_nats_publish_tool(bus))
    return tools

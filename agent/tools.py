"""Agent tools for the local AI agent."""

import asyncio
import io
import os
import subprocess
import sys
from contextlib import redirect_stdout
from html.parser import HTMLParser

import requests
from langchain_core.tools import StructuredTool

from config import (
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
                          for name, cfg in AGENTS.items() if name != "nano"]
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

def _get_crypto_tools():
    """Import and return crypto tools (lazy to avoid import errors if data_api not running)."""
    try:
        from agent.crypto_tools import ALL_CRYPTO_TOOLS
        return list(ALL_CRYPTO_TOOLS)
    except Exception:
        return []


def create_tools(bus=None, sub_agent_manager=None):
    """Create and return all agent tools."""
    tools = [file_read, file_write, file_edit, shell_exec, python_exec, web_fetch, codex_exec, claude_exec]
    tools.extend(_get_crypto_tools())
    if bus:
        tools.append(create_nats_publish_tool(bus))
        tools.append(create_nats_request_tool(bus))
    if sub_agent_manager:
        tools.append(create_spawn_agent_tool(sub_agent_manager))
        tools.append(create_list_agents_tool(sub_agent_manager))
    return tools


def create_sub_agent_tools(bus):
    """Create the limited toolset for sub-agents (no spawning)."""
    tools = [file_read, file_write, file_edit, shell_exec, python_exec, web_fetch, codex_exec, claude_exec]
    tools.extend(_get_crypto_tools())
    if bus:
        tools.append(create_nats_publish_tool(bus))
    return tools

"""Structured logging for the agent system.

Log levels:
  - ERROR: failures only
  - WARNING: failures + unexpected behavior
  - INFO: agent lifecycle, tool calls, NATS messages (summary)
  - DEBUG: full prompts, full responses, full tool I/O — everything needed for tuning
"""

import json
import logging
import os
from datetime import datetime, timezone

from config import PROJECT_ROOT

_config_path = os.path.join(PROJECT_ROOT, "agent-config.json")
with open(_config_path) as _f:
    _raw = json.load(_f)

LOG_LEVEL = getattr(logging, _raw.get("log_level", "INFO").upper(), logging.INFO)
LOG_DIR = os.path.join(PROJECT_ROOT, _raw.get("log_dir", "logs"))
os.makedirs(LOG_DIR, exist_ok=True)

# ── Formatters ───────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _make_handler(filename, level=None):
    """Create a rotating-ish file handler (append mode, one file per day)."""
    today = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(LOG_DIR, f"{filename}_{today}.log")
    handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    if level is not None:
        handler.setLevel(level)
    return handler


def get_logger(name: str) -> logging.Logger:
    """Get a logger that writes to both the main log and a per-component log."""
    logger = logging.getLogger(f"agent.{name}")
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(LOG_LEVEL)
    logger.propagate = False

    # Main combined log
    logger.addHandler(_make_handler("agent"))
    # Per-component log
    logger.addHandler(_make_handler(name))

    return logger


# ── Convenience loggers for common patterns ──────────────────

def _coerce_session_name(session) -> str:
    if hasattr(session, "name"):
        return str(getattr(session, "name"))
    return str(session)


def _format_log_parts(data) -> str:
    if data is None:
        return ""
    if isinstance(data, dict):
        return " ".join(f"{key}={value}" for key, value in data.items())
    if isinstance(data, (list, tuple)):
        return " ".join(str(item) for item in data if str(item))
    return str(data)


def log_llm_request(agent_name: str, messages: list, **kwargs):
    """Log the full prompt being sent to the LLM (DEBUG level)."""
    logger = get_logger(agent_name)
    if not logger.isEnabledFor(logging.DEBUG):
        return
    # Serialize messages for readability
    msg_dump = []
    for m in messages:
        if hasattr(m, "content"):
            msg_dump.append({"role": type(m).__name__, "content": m.content[:2000]})
        elif isinstance(m, dict):
            msg_dump.append(m)
        else:
            msg_dump.append(str(m)[:500])
    logger.debug("LLM_REQUEST %s", json.dumps({
        "agent": agent_name,
        "messages": msg_dump,
        "kwargs": {k: str(v)[:200] for k, v in kwargs.items()},
    }, indent=2, default=str))


def log_llm_response(agent_name: str, content: str, tool_calls=None, **kwargs):
    """Log the full LLM response (DEBUG level)."""
    logger = get_logger(agent_name)
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug("LLM_RESPONSE %s", json.dumps({
        "agent": agent_name,
        "content": content[:5000] if content else "",
        "tool_calls": str(tool_calls)[:2000] if tool_calls else None,
        **{k: str(v)[:500] for k, v in kwargs.items()},
    }, indent=2, default=str))


def log_tool_call(agent_name: str, tool_name: str, tool_input, tool_output=None):
    """Log a tool invocation (INFO summary, DEBUG full I/O)."""
    logger = get_logger(agent_name)
    input_str = json.dumps(tool_input, default=str) if isinstance(tool_input, dict) else str(tool_input)
    output_str = str(tool_output) if tool_output is not None else None

    logger.info("TOOL_CALL agent=%s tool=%s input=%s", agent_name, tool_name, input_str[:200])
    if output_str is not None:
        logger.info("TOOL_RESULT agent=%s tool=%s output=%s", agent_name, tool_name, output_str[:200])

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("TOOL_CALL_FULL %s", json.dumps({
            "agent": agent_name,
            "tool": tool_name,
            "input": input_str[:5000],
            "output": output_str[:5000] if output_str else None,
        }, indent=2, default=str))


def log_nats(direction: str, subject: str, payload: dict):
    """Log NATS message (INFO summary, DEBUG full payload)."""
    logger = get_logger("nats")
    logger.info("NATS %s subject=%s from=%s", direction.upper(), subject, payload.get("from", "?"))
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("NATS_FULL %s", json.dumps({
            "direction": direction,
            "subject": subject,
            "payload": payload,
        }, indent=2, default=str))


def log_agent_event(agent_name: str, event_type: str, data=None):
    """Log an agent lifecycle event (spawn, stop, error, fallback, etc.)."""
    logger = get_logger(agent_name)
    logger.info("AGENT_EVENT agent=%s event=%s data=%s", agent_name, event_type, str(data)[:200] if data else "")


def log_auto_event(session, event_type: str, data=None):
    """Log one autonomous-mode event with the session name attached."""
    logger = get_logger("auto")
    session_name = _coerce_session_name(session)
    suffix = _format_log_parts(data)
    if suffix:
        logger.info("%s session=%s %s", event_type, session_name, suffix)
        return
    logger.info("%s session=%s", event_type, session_name)


def log_slash_command(session, command: str, args: str = "", handler: str = ""):
    """Log slash-command routing decisions."""
    logger = get_logger("daemon.server")
    logger.info(
        "SLASH_COMMAND session=%s command=%s args=%s handler=%s",
        _coerce_session_name(session),
        command,
        args,
        handler,
    )

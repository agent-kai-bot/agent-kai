"""Tests for agents.yaml health and capacity schema validation."""

from __future__ import annotations

from pathlib import Path
import textwrap

import pytest
from pydantic import ValidationError

from config import (
    AGENTS,
    DEFAULT_CAPACITY_FEEDBACK_CODES,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_EXECUTOR,
    DEFAULT_HEALTH_PROBE_COMMAND,
    DEFAULT_HEALTH_PROBE_INTERVAL_SECONDS,
    DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS,
    DEFAULT_OVERFLOW_EXECUTOR,
    get_agent_config,
    load_agents_yaml,
)


def _write_agents_yaml(tmp_path: Path, content: str) -> Path:
    """Write a temporary agents.yaml file for schema tests."""
    path = tmp_path / "agents.yaml"
    path.write_text(textwrap.dedent(content).strip() + "\n")
    return path


def test_existing_agents_yaml_loads_cleanly_with_all_fields_defaulted():
    """The repository agents.yaml should load and merge defaults for all agents."""
    config = load_agents_yaml(base_agent_names=AGENTS)

    assert set(AGENTS).issubset(config.agents)
    agent = config.agents["kai"]
    assert agent.health_probe.command == DEFAULT_HEALTH_PROBE_COMMAND
    assert agent.health_probe.interval_seconds == DEFAULT_HEALTH_PROBE_INTERVAL_SECONDS
    assert agent.health_probe.timeout_seconds == DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS
    assert agent.capacity_feedback_codes == DEFAULT_CAPACITY_FEEDBACK_CODES
    assert agent.cooldown_seconds == DEFAULT_COOLDOWN_SECONDS
    assert agent.default_executor == DEFAULT_EXECUTOR
    assert agent.overflow_executor == DEFAULT_OVERFLOW_EXECUTOR

    resolved = get_agent_config("kai")
    assert resolved["health_probe"]["command"] == DEFAULT_HEALTH_PROBE_COMMAND
    assert resolved["capacity_feedback_codes"] == DEFAULT_CAPACITY_FEEDBACK_CODES


def test_explicit_per_agent_fields_parse_correctly(tmp_path):
    """Explicit health and executor settings should override defaults."""
    path = _write_agents_yaml(
        tmp_path,
        """
        version: 1
        agents:
          developer:
            health_probe:
              command: "test -f /tmp/healthy"
              interval_seconds: 120
              timeout_seconds: 20
            capacity_feedback_codes: [429, 500, 503]
            cooldown_seconds: 600
            default_executor: claude
            overflow_executor: local-llm
        """,
    )

    config = load_agents_yaml(str(path), base_agent_names=["developer"])
    agent = config.agents["developer"]

    assert agent.health_probe.command == "test -f /tmp/healthy"
    assert agent.health_probe.interval_seconds == 120
    assert agent.health_probe.timeout_seconds == 20
    assert agent.capacity_feedback_codes == [429, 500, 503]
    assert agent.cooldown_seconds == 600
    assert agent.default_executor == "claude"
    assert agent.overflow_executor == "local-llm"


@pytest.mark.parametrize("interval_seconds", [0, 9999])
def test_invalid_health_probe_interval_raises_clear_error(
    tmp_path, interval_seconds
):
    """Out-of-range probe intervals should be rejected with field context."""
    path = _write_agents_yaml(
        tmp_path,
        f"""
        version: 1
        agents:
          developer:
            health_probe:
              command: "echo healthy"
              interval_seconds: {interval_seconds}
              timeout_seconds: 1
        """,
    )

    with pytest.raises(ValidationError, match="interval_seconds"):
        load_agents_yaml(str(path), base_agent_names=["developer"])


def test_invalid_default_executor_value_is_rejected(tmp_path):
    """Unknown default_executor values should fail validation."""
    path = _write_agents_yaml(
        tmp_path,
        """
        version: 1
        agents:
          developer:
            default_executor: not-a-backend
        """,
    )

    with pytest.raises(ValidationError, match="default_executor"):
        load_agents_yaml(str(path), base_agent_names=["developer"])


def test_missing_health_probe_block_gets_defaults(tmp_path):
    """A missing health_probe block should not be an error."""
    path = _write_agents_yaml(
        tmp_path,
        """
        version: 1
        agents:
          developer:
            default_executor: codex
        """,
    )

    config = load_agents_yaml(str(path), base_agent_names=["developer"])
    probe = config.agents["developer"].health_probe
    assert probe.command == DEFAULT_HEALTH_PROBE_COMMAND
    assert probe.interval_seconds == DEFAULT_HEALTH_PROBE_INTERVAL_SECONDS
    assert probe.timeout_seconds == DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS


def test_overflow_executor_null_is_allowed(tmp_path):
    """overflow_executor is optional and may be explicitly disabled."""
    path = _write_agents_yaml(
        tmp_path,
        """
        version: 1
        agents:
          developer:
            default_executor: codex
            overflow_executor: null
        """,
    )

    config = load_agents_yaml(str(path), base_agent_names=["developer"])
    assert config.agents["developer"].overflow_executor is None

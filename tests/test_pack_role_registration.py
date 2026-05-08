from __future__ import annotations

from pathlib import Path

from daemon.signal_router.agent_pack import (
    AgentPack,
    load_pack,
    register_pack_role,
)
from daemon.signal_router.router import SignalRouter


class CapturingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, *args) -> None:
        self.warnings.append(message % args if args else message)


def _pack() -> AgentPack:
    return AgentPack(
        name="kai-alert-response",
        root_path=Path("/tmp/kai-alert-response"),
        system_prompt="assembled prompt",
        decision_logic="decision",
        tools_reference="tools",
        schema_path=None,
        manifest={},
    )


def test_pack_name_matches_existing_role_uses_role_as_is() -> None:
    agents = {"kai-alert-response": {"system_prompt": "assembled prompt", "endpoint": "codex-cli"}}

    outcome = register_pack_role(_pack(), agents=agents)

    assert outcome.status == "matched_existing"
    assert agents["kai-alert-response"]["system_prompt"] == "assembled prompt"


def test_pack_existing_role_prompt_divergence_warns_and_prefers_pack_content() -> None:
    agents = {"kai-alert-response": {"system_prompt": "old prompt", "endpoint": "codex-cli"}}
    logger = CapturingLogger()

    outcome = register_pack_role(_pack(), agents=agents, logger=logger)

    assert outcome.status == "matched_existing_updated"
    assert "divergent system_prompt" in logger.warnings[0]
    assert agents["kai-alert-response"]["system_prompt"] == "assembled prompt"


def test_missing_pack_role_auto_registered() -> None:
    agents: dict[str, dict] = {}

    outcome = register_pack_role(_pack(), agents=agents)

    role = agents["kai-alert-response"]
    assert outcome.status == "auto_registered"
    assert role["description"] == "Auto-registered from agent-pack kai-alert-response"
    assert role["endpoint"] == "codex-cli"
    assert role["model_id"] == "gpt-5.5"
    assert role["model"] == "gpt-5.5"
    assert role["system_prompt"] == "assembled prompt"
    assert role["max_iterations"] == 30


def test_reload_after_auto_registration_does_not_double_register() -> None:
    agents: dict[str, dict] = {}
    first = register_pack_role(_pack(), agents=agents)
    second = register_pack_role(_pack(), agents=agents)

    assert first.status == "auto_registered"
    assert second.status == "matched_existing"
    assert list(agents) == ["kai-alert-response"]


def test_route_load_registers_spawn_agent_pack(tmp_path, monkeypatch) -> None:
    packs_dir = tmp_path / "agent-packs"
    root = packs_dir / "kai-alert-response"
    root.mkdir(parents=True)
    (root / "system_prompt.md").write_text("router pack prompt\n", encoding="utf-8")
    agents: dict[str, dict] = {}
    monkeypatch.setattr("config.AGENTS", agents)

    SignalRouter(
        {
            "mode": "legacy",
            "agent_packs_dir": str(packs_dir),
            "dedup_table_path": str(tmp_path / "dedup.sqlite3"),
            "routes": [
                {
                    "name": "polymarket",
                    "channel": "polymarket_alarms",
                    "actions": [{"kind": "spawn_agent", "pack": "kai-alert-response"}],
                }
            ],
        }
    )

    assert agents["kai-alert-response"]["system_prompt"] == load_pack(
        "kai-alert-response",
        packs_dir=packs_dir,
    ).system_prompt

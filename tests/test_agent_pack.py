from __future__ import annotations

from pathlib import Path

import pytest

from daemon.signal_router.agent_pack import AgentPackError, load_pack


def _write_pack(base: Path, name: str, *, optional: bool = True) -> Path:
    root = base / name
    root.mkdir(parents=True)
    (root / "system_prompt.md").write_text("System prompt\n", encoding="utf-8")
    if optional:
        (root / "decision_logic.md").write_text("Decision tree\n", encoding="utf-8")
        (root / "tools_reference.md").write_text("Tools doc\n", encoding="utf-8")
        (root / "nats_payload_schema.json").write_text('{"type":"object"}\n', encoding="utf-8")
        (root / "README.md").write_text("Human docs\n", encoding="utf-8")
        examples = root / "example_alarms"
        examples.mkdir()
        (examples / "alarm.json").write_text('{"rule_id":"r"}\n', encoding="utf-8")
    return root


def test_load_valid_pack_with_all_file_types(tmp_path) -> None:
    packs_dir = tmp_path / "agent-packs"
    root = _write_pack(packs_dir, "kai-alert-response")

    pack = load_pack("kai-alert-response", packs_dir=packs_dir)

    assert pack.name == "kai-alert-response"
    assert pack.root_path == root
    assert "System prompt" in pack.system_prompt
    assert "## Decision logic\n\nDecision tree" in pack.system_prompt
    assert "## Tools reference\n\nTools doc" in pack.system_prompt
    assert pack.decision_logic == "Decision tree"
    assert pack.tools_reference == "Tools doc"
    assert pack.schema_path == root / "nats_payload_schema.json"
    assert pack.manifest["readme_path"] == str(root / "README.md")
    assert pack.manifest["example_alarms_path"] == str(root / "example_alarms")
    assert "Human docs" not in pack.system_prompt


def test_load_minimal_pack(tmp_path) -> None:
    packs_dir = tmp_path / "agent-packs"
    _write_pack(packs_dir, "minimal", optional=False)

    pack = load_pack("minimal", packs_dir=packs_dir)

    assert pack.system_prompt == "System prompt"
    assert pack.decision_logic == ""
    assert pack.tools_reference == ""
    assert pack.schema_path is None


def test_missing_system_prompt_raises(tmp_path) -> None:
    packs_dir = tmp_path / "agent-packs"
    (packs_dir / "broken").mkdir(parents=True)

    with pytest.raises(AgentPackError, match="missing required system_prompt.md"):
        load_pack("broken", packs_dir=packs_dir)


def test_bad_symlink_target_has_clear_staging_error(tmp_path) -> None:
    packs_dir = tmp_path / "agent-packs"
    packs_dir.mkdir()
    (packs_dir / "kai-alert-response").symlink_to(tmp_path / "missing-pack")

    with pytest.raises(AgentPackError, match="ln -s /home/atc/git/OPS/kai-alert-response"):
        load_pack("kai-alert-response", packs_dir=packs_dir)

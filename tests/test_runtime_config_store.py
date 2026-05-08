from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from daemon.runtime_config_store import RuntimeConfigStore


def _write_base_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "daemon": {
                    "auto_loop_brain": {
                        "enabled": False,
                        "client": "codex-cli",
                        "model_id": "gpt-5.5",
                        "timeout_seconds": 20.0,
                    },
                    "heartbeat": {"enabled": True},
                },
                "endpoints": {"codex-cli": {"provider": "codex-cli"}},
            }
        ),
        encoding="utf-8",
    )


def test_runtime_config_store_loads_and_merges_overlay(tmp_path: Path) -> None:
    base_path = tmp_path / "agent-config.json"
    override_path = tmp_path / "workspaces" / "runtime_overrides.json"
    _write_base_config(base_path)
    override_path.parent.mkdir()
    override_path.write_text(
        json.dumps(
            {
                "daemon": {
                    "auto_loop_brain": {
                        "enabled": True,
                        "model_id": "gpt-5.4",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    store = RuntimeConfigStore(
        base_config_path=base_path,
        overrides_path=override_path,
    )

    effective = store.effective_config()
    assert effective["daemon"]["auto_loop_brain"]["enabled"] is True
    assert effective["daemon"]["auto_loop_brain"]["client"] == "codex-cli"
    assert effective["daemon"]["auto_loop_brain"]["model_id"] == "gpt-5.4"
    assert effective["daemon"]["heartbeat"]["enabled"] is True


def test_runtime_config_store_missing_override_file_uses_base(tmp_path: Path) -> None:
    base_path = tmp_path / "agent-config.json"
    override_path = tmp_path / "workspaces" / "runtime_overrides.json"
    _write_base_config(base_path)

    store = RuntimeConfigStore(
        base_config_path=base_path,
        overrides_path=override_path,
    )

    assert store.load_overrides() == {}
    assert store.get_auto_loop_brain()["enabled"] is False
    assert not override_path.exists()


def test_runtime_config_store_atomic_write_leaves_base_untouched(tmp_path: Path) -> None:
    base_path = tmp_path / "agent-config.json"
    override_path = tmp_path / "workspaces" / "runtime_overrides.json"
    _write_base_config(base_path)
    before_base = base_path.read_text(encoding="utf-8")

    store = RuntimeConfigStore(
        base_config_path=base_path,
        overrides_path=override_path,
    )
    effective = store.update_auto_loop_brain_enabled(True)

    assert effective["enabled"] is True
    assert base_path.read_text(encoding="utf-8") == before_base
    payload = json.loads(override_path.read_text(encoding="utf-8"))
    assert payload["daemon"]["auto_loop_brain"]["enabled"] is True
    assert list(override_path.parent.glob("*.tmp")) == []


def test_runtime_config_store_concurrent_writes_do_not_corrupt_json(
    tmp_path: Path,
) -> None:
    base_path = tmp_path / "agent-config.json"
    override_path = tmp_path / "workspaces" / "runtime_overrides.json"
    _write_base_config(base_path)
    store = RuntimeConfigStore(
        base_config_path=base_path,
        overrides_path=override_path,
    )

    def write_value(index: int) -> None:
        store.update_section(("daemon", "concurrent"), {f"k{index}": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_value, range(40)))

    payload = json.loads(override_path.read_text(encoding="utf-8"))
    concurrent = payload["daemon"]["concurrent"]
    assert concurrent == {f"k{index}": index for index in range(40)}

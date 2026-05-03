"""Tests for the local workspace end-to-end smoke harness."""

from __future__ import annotations

from pathlib import Path

from tests.qa.workspace_e2e_smoke import run_smoke


def test_workspace_e2e_smoke_verifies_isolation_and_cleanup(tmp_path: Path) -> None:
    result = run_smoke(tmp_path)

    assert result.operator_head_before == result.operator_head_after
    assert result.developer_path != result.reviewer_path
    assert result.developer_commit == result.reviewer_commit
    assert result.task_dir_removed is True
    assert not result.developer_path.exists()
    assert not result.reviewer_path.exists()
    assert result.pruned_repo_keys == ("local.test__smoke__operator-clone",)

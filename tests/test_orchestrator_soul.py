"""Tests for orchestrator smallest-action guidance."""

from __future__ import annotations

from pathlib import Path


def test_orchestrator_soul_contains_smallest_action_checkpoint() -> None:
    text = Path("workspaces/orchestrator/SOUL.md").read_text()

    assert "Smallest-Action Principle" in text
    assert "is there a one-step version that satisfies the user's stated goal?" in text
    assert "2026-05-01 phase 0 cutover" in text
    assert "docker compose up -d" in text
    assert "30 minutes in devlab" in text or "30-minute devlab investigation" in text


def test_smallest_action_principle_doc_exists_with_audit_pattern() -> None:
    text = Path("agents/PRINCIPLES/SMALLEST-ACTION.md").read_text()

    assert "# Smallest-Action Principle" in text
    assert "Is there a one-step version that satisfies the user's stated goal?" in text
    assert "Audit Format" in text
    assert "Observed result" in text

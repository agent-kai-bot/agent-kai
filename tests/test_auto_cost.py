"""Tests for auto-loop-brain cost telemetry alerts."""

from __future__ import annotations

from daemon import auto_cost


def test_auto_evaluator_cost_alert_uses_cumulative_session_spend(tmp_path, monkeypatch):
    db_path = tmp_path / "daemon-state.sqlite3"
    original_connect = auto_cost.db.connect

    def connect_tmp():
        return original_connect(db_path)

    monkeypatch.setattr(auto_cost.db, "connect", connect_tmp)

    auto_cost.record_main_agent_llm_usage(
        session_name="s1",
        agent_name="developer",
        payload={"model_id": "main", "estimated_cost_usd": 1.0},
    )

    first = auto_cost.record_auto_evaluator_call(
        session_name="s1",
        agent_name="developer",
        payload={
            "model_id": "claude-sonnet-4-6",
            "evaluator_kind": "llm",
            "llm_usage": {"estimated_cost_usd": 0.03},
        },
    )
    assert first is None

    second = auto_cost.record_auto_evaluator_call(
        session_name="s1",
        agent_name="developer",
        payload={
            "model_id": "claude-sonnet-4-6",
            "evaluator_kind": "llm",
            "llm_usage": {"estimated_cost_usd": 0.03},
        },
    )

    assert second is not None
    assert second["estimated_cost_usd"] == 0.03
    assert second["session_auto_evaluator_cost_usd"] == 0.06
    assert second["session_main_agent_cost_usd"] == 1.0
    assert second["spend_ratio"] == 0.06

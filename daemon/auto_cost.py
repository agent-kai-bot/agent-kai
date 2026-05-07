"""Minimal cost telemetry store for auto-loop-brain critic calls."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from daemon import db

LOGGER = logging.getLogger(__name__)
AUTO_EVALUATOR_COST_ALERT_RATIO = 0.05


def record_auto_evaluator_call(
    *,
    session_name: str,
    agent_name: str,
    payload: dict[str, Any],
) -> dict[str, object] | None:
    """Persist one critic-call usage event and return alert metadata if any.

    The store is intentionally best-effort: cost dashboard failures must not
    affect auto-loop control flow.  ``payload`` is the non-secret telemetry
    emitted by :class:`agent.auto_loop_brain.LLMCriticEvaluator`.
    """

    usage = payload.get("llm_usage")
    if not isinstance(usage, dict):
        return None

    estimated_cost = _optional_float(usage.get("estimated_cost_usd"))
    input_tokens = _optional_int(usage.get("input_tokens"))
    output_tokens = _optional_int(usage.get("output_tokens"))
    model_id = str(payload.get("model_id") or "")

    if estimated_cost is None and input_tokens is None and output_tokens is None:
        return None

    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO auto_evaluator_cost_events(
                    created_at, session_name, agent_name, evaluator_kind, model_id,
                    input_tokens, output_tokens, estimated_cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    session_name,
                    agent_name,
                    str(payload.get("evaluator_kind") or "llm"),
                    model_id,
                    input_tokens,
                    output_tokens,
                    estimated_cost,
                ),
            )
            if estimated_cost is None:
                return None
            total = _sum_cost(conn, session_name=session_name)
            main_agent_spend = _optional_float(payload.get("main_agent_estimated_cost_usd"))
            if main_agent_spend is None:
                main_agent_spend = _optional_float(payload.get("session_main_agent_cost_usd"))
            # No general main-agent spend ledger exists in this repo yet. When
            # callers include main-agent spend, alert on the spec's >5% ratio;
            # otherwise still persist the event for dashboard aggregation.
            if (
                main_agent_spend is not None
                and main_agent_spend > 0
                and estimated_cost / main_agent_spend > AUTO_EVALUATOR_COST_ALERT_RATIO
            ):
                return {
                    "session_name": session_name,
                    "agent_name": agent_name,
                    "model_id": model_id,
                    "estimated_cost_usd": round(estimated_cost, 6),
                    "session_auto_evaluator_cost_usd": round(total, 6),
                    "main_agent_estimated_cost_usd": round(main_agent_spend, 6),
                    "spend_ratio": round(estimated_cost / main_agent_spend, 6),
                    "threshold_ratio": AUTO_EVALUATOR_COST_ALERT_RATIO,
                }
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive best-effort path
        LOGGER.warning("failed to record auto evaluator cost telemetry: %s", exc.__class__.__name__)
    return None


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_evaluator_cost_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_name TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            evaluator_kind TEXT NOT NULL,
            model_id TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost_usd REAL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_auto_evaluator_cost_session_created
        ON auto_evaluator_cost_events(session_name, created_at)
        """
    )


def _sum_cost(conn: sqlite3.Connection, *, session_name: str) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS total
        FROM auto_evaluator_cost_events
        WHERE session_name = ?
        """,
        (session_name,),
    ).fetchone()
    return float(row["total"] if row is not None else 0.0)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

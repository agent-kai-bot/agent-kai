"""Minimal cost telemetry store for auto-loop-brain critic calls."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from daemon import db

LOGGER = logging.getLogger(__name__)
AUTO_EVALUATOR_COST_ALERT_RATIO = 0.05
DEFAULT_INPUT_COST_PER_1M_TOKENS_USD = 1.0
DEFAULT_OUTPUT_COST_PER_1M_TOKENS_USD = 5.0
COST_TABLES = frozenset({"auto_evaluator_cost_events", "main_agent_cost_events"})


def record_main_agent_llm_usage(
    *,
    session_name: str,
    agent_name: str,
    payload: dict[str, Any],
) -> None:
    """Persist one main-agent LLM usage event for evaluator spend alerts.

    This is intentionally best-effort and non-blocking.  The auto-loop-brain
    cost dashboard compares critic spend against the same session's main-agent
    spend, so production alerts must come from real main-agent telemetry rather
    than synthetic fields on critic metrics.
    """

    input_tokens = _optional_int(payload.get("input_tokens"))
    output_tokens = _optional_int(payload.get("output_tokens"))
    estimated_cost = _usage_cost(payload, input_tokens=input_tokens, output_tokens=output_tokens)
    if estimated_cost is None and input_tokens is None and output_tokens is None:
        return

    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO main_agent_cost_events(
                    created_at, session_name, agent_name, model_id,
                    input_tokens, output_tokens, estimated_cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    session_name,
                    agent_name,
                    str(payload.get("model_id") or ""),
                    input_tokens,
                    output_tokens,
                    estimated_cost,
                ),
            )
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive best-effort path
        LOGGER.warning("failed to record main agent cost telemetry: %s", exc.__class__.__name__)


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

    input_tokens = _optional_int(usage.get("input_tokens"))
    output_tokens = _optional_int(usage.get("output_tokens"))
    estimated_cost = _usage_cost(usage, input_tokens=input_tokens, output_tokens=output_tokens)
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
            total = _sum_cost(conn, table="auto_evaluator_cost_events", session_name=session_name)
            main_agent_spend = _sum_cost(
                conn,
                table="main_agent_cost_events",
                session_name=session_name,
            )
            if (
                main_agent_spend > 0
                and estimated_cost / main_agent_spend > AUTO_EVALUATOR_COST_ALERT_RATIO
            ):
                return {
                    "session_name": session_name,
                    "agent_name": agent_name,
                    "model_id": model_id,
                    "estimated_cost_usd": round(estimated_cost, 6),
                    "session_auto_evaluator_cost_usd": round(total, 6),
                    "session_main_agent_cost_usd": round(main_agent_spend, 6),
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS main_agent_cost_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_name TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            model_id TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost_usd REAL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_main_agent_cost_session_created
        ON main_agent_cost_events(session_name, created_at)
        """
    )


def _sum_cost(conn: sqlite3.Connection, *, table: str, session_name: str) -> float:
    if table not in COST_TABLES:
        raise ValueError("unsupported cost table")
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS total
        FROM {table}
        WHERE session_name = ?
        """,
        (session_name,),
    ).fetchone()
    return float(row["total"] if row is not None else 0.0)


def _usage_cost(
    payload: dict[str, Any],
    *,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    explicit = _optional_float(payload.get("estimated_cost_usd"))
    if explicit is not None:
        return explicit
    if input_tokens is None and output_tokens is None:
        return None
    input_rate = _optional_float(payload.get("input_cost_per_1m_tokens_usd"))
    if input_rate is None:
        input_rate = DEFAULT_INPUT_COST_PER_1M_TOKENS_USD
    output_rate = _optional_float(payload.get("output_cost_per_1m_tokens_usd"))
    if output_rate is None:
        output_rate = DEFAULT_OUTPUT_COST_PER_1M_TOKENS_USD
    return ((input_tokens or 0) * input_rate + (output_tokens or 0) * output_rate) / 1_000_000


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

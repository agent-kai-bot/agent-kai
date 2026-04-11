"""SQLite-backed provenance store for ASO strategy versions and evaluations."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.strategy_ir import StrategyIR
from agent.strategy_metrics import (
    BenchmarkMetrics,
    CostAnalysisMetrics,
    DrawdownMetrics,
    MetricsReport,
    ReturnMetrics,
    RiskAdjustedMetrics,
    StabilityMetrics,
    TailRiskMetrics,
    TradeMetrics,
)
from agent.strategy_provenance import EXECUTOR_VERSION

DEFAULT_DB_PATH = Path("workspaces/strategies/aso.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_id TEXT,
    ir_json TEXT NOT NULL,
    yaml_source TEXT,
    pool TEXT NOT NULL DEFAULT 'candidates',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'human',
    UNIQUE(name, version),
    FOREIGN KEY(parent_id) REFERENCES strategies(id)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    stage TEXT NOT NULL,
    fold_index INTEGER,
    dataset_hash TEXT NOT NULL,
    dataset_source TEXT,
    dataset_range TEXT,
    executor_version TEXT NOT NULL,
    fee_model_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    sample_size_pass INTEGER NOT NULL,
    sample_size_detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mutations (
    id TEXT PRIMARY KEY,
    parent_strategy_id TEXT NOT NULL REFERENCES strategies(id),
    child_strategy_id TEXT NOT NULL REFERENCES strategies(id),
    mutations_json TEXT NOT NULL,
    llm_model TEXT,
    llm_prompt_hash TEXT,
    accepted INTEGER NOT NULL,
    rejection_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategies(id),
    action TEXT NOT NULL,
    from_pool TEXT NOT NULL,
    to_pool TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    evaluation_run_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(strategy_id) REFERENCES strategies(id),
    FOREIGN KEY(evaluation_run_id) REFERENCES runs(id)
);
"""

_DB_LOCK = threading.RLock()


@dataclass(frozen=True)
class StrategyRecord:
    id: str
    name: str
    version: int
    parent_id: str | None
    ir: StrategyIR
    yaml_source: str | None
    pool: str
    created_at: str
    created_by: str


@dataclass(frozen=True)
class RunRecord:
    id: str
    strategy_id: str
    stage: str
    fold_index: int | None
    dataset_hash: str
    dataset_source: str | None
    dataset_range: str | None
    executor_version: str
    fee_model_json: str
    metrics: MetricsReport
    sample_size_pass: bool
    sample_size_detail: str | None
    created_at: str


@dataclass(frozen=True)
class MutationRecord:
    id: str
    parent_strategy_id: str
    child_strategy_id: str
    mutations: list[dict[str, Any]]
    llm_model: str | None
    llm_prompt_hash: str | None
    accepted: bool
    rejection_reason: str | None
    created_at: str


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    strategy_id: str
    action: str
    from_pool: str
    to_pool: str
    approved_by: str
    evaluation_run_id: str | None
    created_at: str


@dataclass(frozen=True)
class LineageEntry:
    strategy: StrategyRecord
    mutation: MutationRecord | None


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> Path:
    """Create the provenance database and schema if missing."""
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK, _connect(target) as conn:
        conn.executescript(_SCHEMA)
    return target


def save_strategy(
    ir: StrategyIR,
    name: str,
    version: int,
    parent_id: str | None = None,
    pool: str = "candidates",
    created_by: str = "human",
    yaml_source: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    """Persist an immutable strategy version and return its id."""
    if ir.name != name:
        raise ValueError(f"strategy name mismatch: ir.name={ir.name!r} != name={name!r}")

    init_db(db_path)
    strategy_id = _strategy_id(name, version)
    created_at = _utc_now()
    with _DB_LOCK, _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategies (id, name, version, parent_id, ir_json, yaml_source, pool, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                name,
                version,
                parent_id,
                ir.model_dump_json(),
                yaml_source,
                pool,
                created_at,
                created_by,
            ),
        )
    return strategy_id


def save_run(
    strategy_id: str,
    stage: str,
    fold_index: int | None,
    dataset_hash: str,
    metrics: MetricsReport,
    sample_pass: bool,
    *,
    dataset_source: str | None = None,
    dataset_range: str | None = None,
    fee_model_json: str = "{}",
    executor_version: str = EXECUTOR_VERSION,
    sample_size_detail: str | None = None,
    run_id: str | None = None,
    created_at: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    """Persist one immutable evaluation run and return its id."""
    init_db(db_path)
    run_id = run_id or str(uuid4())
    created_at = created_at or _utc_now()
    with _DB_LOCK, _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
                id, strategy_id, stage, fold_index, dataset_hash, dataset_source, dataset_range,
                executor_version, fee_model_json, metrics_json, sample_size_pass, sample_size_detail, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                strategy_id,
                stage,
                fold_index,
                dataset_hash,
                dataset_source,
                dataset_range,
                executor_version,
                fee_model_json,
                json.dumps(asdict(metrics), sort_keys=True, separators=(",", ":")),
                int(sample_pass),
                sample_size_detail,
                created_at,
            ),
        )
    return run_id


def save_mutation(
    parent_id: str,
    child_id: str,
    mutations: list[dict[str, Any]],
    accepted: bool,
    rejection_reason: str | None = None,
    *,
    llm_model: str | None = None,
    llm_prompt_hash: str | None = None,
    mutation_id: str | None = None,
    created_at: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    """Persist a parent->child mutation record and return its id."""
    init_db(db_path)
    mutation_id = mutation_id or str(uuid4())
    created_at = created_at or _utc_now()
    with _DB_LOCK, _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO mutations (
                id, parent_strategy_id, child_strategy_id, mutations_json,
                llm_model, llm_prompt_hash, accepted, rejection_reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mutation_id,
                parent_id,
                child_id,
                json.dumps(mutations, sort_keys=True, separators=(",", ":")),
                llm_model,
                llm_prompt_hash,
                int(accepted),
                rejection_reason,
                created_at,
            ),
        )
    return mutation_id


def get_strategy(strategy_id: str, db_path: str | Path = DEFAULT_DB_PATH) -> StrategyIR:
    """Load a strategy IR by id."""
    init_db(db_path)
    with _DB_LOCK, _connect(db_path) as conn:
        row = conn.execute("SELECT ir_json FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown strategy: {strategy_id}")
    return StrategyIR.model_validate_json(row["ir_json"])


def get_lineage(name: str, db_path: str | Path = DEFAULT_DB_PATH) -> list[LineageEntry]:
    """Return the version lineage for a strategy name in ascending version order."""
    init_db(db_path)
    with _DB_LOCK, _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.name,
                s.version,
                s.parent_id,
                s.ir_json,
                s.yaml_source,
                s.pool,
                s.created_at,
                s.created_by,
                m.id AS mutation_id,
                m.parent_strategy_id,
                m.child_strategy_id,
                m.mutations_json,
                m.llm_model,
                m.llm_prompt_hash,
                m.accepted,
                m.rejection_reason,
                m.created_at AS mutation_created_at
            FROM strategies AS s
            LEFT JOIN mutations AS m ON m.child_strategy_id = s.id
            WHERE s.name = ?
            ORDER BY s.version ASC
            """,
            (name,),
        ).fetchall()
    return [_lineage_entry_from_row(row) for row in rows]


def get_latest_run(strategy_id: str, stage: str, db_path: str | Path = DEFAULT_DB_PATH) -> RunRecord | None:
    """Return the most recent run for a strategy and stage, if any."""
    init_db(db_path)
    with _DB_LOCK, _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM runs
            WHERE strategy_id = ? AND stage = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (strategy_id, stage),
        ).fetchone()
    return None if row is None else _run_from_row(row)


def list_strategies(pool: str, db_path: str | Path = DEFAULT_DB_PATH) -> list[StrategyRecord]:
    """List strategies currently assigned to a pool."""
    init_db(db_path)
    with _DB_LOCK, _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM strategies WHERE pool = ? ORDER BY name ASC, version ASC",
            (pool,),
        ).fetchall()
    return [_strategy_from_row(row) for row in rows]


def promote_strategy(
    strategy_id: str,
    from_pool: str,
    to_pool: str,
    approved_by: str,
    *,
    evaluation_run_id: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    """Move a strategy between pools and persist the approval action."""
    init_db(db_path)
    approval_id = str(uuid4())
    created_at = _utc_now()
    with _DB_LOCK, _connect(db_path) as conn:
        row = conn.execute("SELECT pool FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown strategy: {strategy_id}")
        current_pool = row["pool"]
        if current_pool != from_pool:
            raise ValueError(f"strategy {strategy_id} is in pool {current_pool!r}, expected {from_pool!r}")

        conn.execute("UPDATE strategies SET pool = ? WHERE id = ?", (to_pool, strategy_id))
        conn.execute(
            """
            INSERT INTO approvals (
                id, strategy_id, action, from_pool, to_pool, approved_by, evaluation_run_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                strategy_id,
                _pool_action(from_pool, to_pool),
                from_pool,
                to_pool,
                approved_by,
                evaluation_run_id,
                created_at,
            ),
        )
    return approval_id


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _strategy_id(name: str, version: int) -> str:
    return f"{name}_v{version}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _pool_action(from_pool: str, to_pool: str) -> str:
    if to_pool == "graveyard":
        return "retire"
    if from_pool == "active" and to_pool != "active":
        return "demote"
    return "promote"


def _strategy_from_row(row: sqlite3.Row) -> StrategyRecord:
    return StrategyRecord(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        parent_id=row["parent_id"],
        ir=StrategyIR.model_validate_json(row["ir_json"]),
        yaml_source=row["yaml_source"],
        pool=row["pool"],
        created_at=row["created_at"],
        created_by=row["created_by"],
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        strategy_id=row["strategy_id"],
        stage=row["stage"],
        fold_index=row["fold_index"],
        dataset_hash=row["dataset_hash"],
        dataset_source=row["dataset_source"],
        dataset_range=row["dataset_range"],
        executor_version=row["executor_version"],
        fee_model_json=row["fee_model_json"],
        metrics=_metrics_from_json(row["metrics_json"]),
        sample_size_pass=bool(row["sample_size_pass"]),
        sample_size_detail=row["sample_size_detail"],
        created_at=row["created_at"],
    )


def _mutation_from_row(row: sqlite3.Row) -> MutationRecord:
    return MutationRecord(
        id=row["mutation_id"],
        parent_strategy_id=row["parent_strategy_id"],
        child_strategy_id=row["child_strategy_id"],
        mutations=json.loads(row["mutations_json"]),
        llm_model=row["llm_model"],
        llm_prompt_hash=row["llm_prompt_hash"],
        accepted=bool(row["accepted"]),
        rejection_reason=row["rejection_reason"],
        created_at=row["mutation_created_at"],
    )


def _lineage_entry_from_row(row: sqlite3.Row) -> LineageEntry:
    mutation = None if row["mutation_id"] is None else _mutation_from_row(row)
    return LineageEntry(strategy=_strategy_from_row(row), mutation=mutation)


def _metrics_from_json(metrics_json: str) -> MetricsReport:
    payload = json.loads(metrics_json)
    return MetricsReport(
        returns=ReturnMetrics(**payload["returns"]),
        risk_adjusted=RiskAdjustedMetrics(**payload["risk_adjusted"]),
        drawdown=DrawdownMetrics(**payload["drawdown"]),
        trades=TradeMetrics(**payload["trades"]),
        benchmark=BenchmarkMetrics(**payload["benchmark"]),
        tail_risk=TailRiskMetrics(**payload["tail_risk"]),
        stability=StabilityMetrics(**payload["stability"]),
        cost_analysis=CostAnalysisMetrics(**payload["cost_analysis"]),
    )

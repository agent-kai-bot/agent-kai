"""Session-scoped ASO strategy tools and slash-command renderers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

import pandas as pd
import yaml
from langchain_core.tools import StructuredTool

from agent.strategy_compiler import compile_strategy
from agent.strategy_metrics import MetricsReport
from agent.strategy_mutator import create_optimizer_llm_client
from agent.strategy_optimizer import OHLCVFetcher, OptimizerConfig, StrategyOptimizer
from agent.strategy_store import DEFAULT_DB_PATH, LineageEntry, StrategyRecord, StrategyStore
from config import get_agent_config

_KNOWN_POOLS = ("candidates", "active", "graveyard")


class OptimizerRuntime(Protocol):
    """Minimal optimizer-control surface used by strategy tools."""

    def optimizer_state(self) -> dict[str, Any]: ...

    def start_optimizer(self, store: StrategyStore, max_cycles: int) -> dict[str, Any]: ...

    def pause_optimizer(self) -> dict[str, Any]: ...

    def recent_cycle_results(self, limit: int = 5) -> list[dict[str, Any]]: ...


@dataclass
class InProcessStrategyRuntime:
    """Local session runtime for background optimizer execution."""

    session_name: str
    agent_name: str = "kai"
    optimizer_config: OptimizerConfig | None = None
    ohlcv_fetcher: OHLCVFetcher | None = None
    event_callback: Callable[[str, dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        self._task: asyncio.Task[list[dict[str, Any]]] | None = None
        self._started_at: str | None = None
        self._last_completed_at: str | None = None
        self._last_stop_reason: str | None = None
        self._last_error: str | None = None
        self._last_requested_cycles: int | None = None
        self._history: list[dict[str, Any]] = []

    def optimizer_state(self) -> dict[str, Any]:
        running = self._task is not None and not self._task.done()
        return {
            "running": running,
            "paused": not running and self._last_stop_reason == "paused",
            "started_at": self._started_at,
            "last_completed_at": self._last_completed_at,
            "last_stop_reason": self._last_stop_reason,
            "last_error": self._last_error,
            "requested_cycles": self._last_requested_cycles,
            "last_cycle_result": self._history[-1] if self._history else None,
        }

    def recent_cycle_results(self, limit: int = 5) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return list(self._history[-limit:])

    def start_optimizer(self, store: StrategyStore, max_cycles: int) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return _error("optimizer_start", "optimizer is already running")
        if max_cycles <= 0:
            return _error("optimizer_start", "max_cycles must be greater than zero")

        self._started_at = _utc_now_iso()
        self._last_requested_cycles = max_cycles
        self._last_stop_reason = None
        self._last_error = None
        self._task = asyncio.create_task(self._run_optimizer(store, max_cycles))
        self._task.add_done_callback(self._finalize_optimizer_task)
        return {
            "ok": True,
            "kind": "optimizer_start",
            "running": True,
            "started_at": self._started_at,
            "max_cycles": max_cycles,
        }

    def pause_optimizer(self) -> dict[str, Any]:
        if self._task is None or self._task.done():
            return _error("optimizer_pause", "optimizer is not running")
        self._last_stop_reason = "paused"
        self._task.cancel()
        return {
            "ok": True,
            "kind": "optimizer_pause",
            "running": False,
            "paused": True,
        }

    async def _run_optimizer(self, store: StrategyStore, max_cycles: int) -> list[dict[str, Any]]:
        optimizer = StrategyOptimizer(
            store=store,
            llm_client=create_optimizer_llm_client(_resolve_agent_endpoint(self.agent_name)),
            ohlcv_fetcher=self.ohlcv_fetcher or KaiApiOHLCVFetcher(),
            config=self.optimizer_config or replace(OptimizerConfig(), cycle_interval_seconds=0),
        )
        results = await optimizer.run_loop(max_cycles=max_cycles)
        return [_serialize_cycle_result(result) for result in results]

    def _finalize_optimizer_task(self, task: asyncio.Task[list[dict[str, Any]]]) -> None:
        cancelled = task.cancelled()
        payload: dict[str, Any]
        if cancelled:
            results: list[dict[str, Any]] = []
            payload = {
                "session": self.session_name,
                "cancelled": True,
                "cycle_count": 0,
                "last_cycle_result": self._history[-1] if self._history else None,
            }
        else:
            try:
                results = task.result()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                self._last_stop_reason = "error"
                self._last_completed_at = _utc_now_iso()
                payload = {
                    "session": self.session_name,
                    "cancelled": False,
                    "cycle_count": 0,
                    "error": str(exc),
                    "last_cycle_result": self._history[-1] if self._history else None,
                }
            else:
                self._history.extend(results)
                self._last_completed_at = _utc_now_iso()
                self._last_stop_reason = "completed"
                payload = {
                    "session": self.session_name,
                    "cancelled": False,
                    "cycle_count": len(results),
                    "last_cycle_result": results[-1] if results else (self._history[-1] if self._history else None),
                }
        self._task = None
        if self.event_callback is not None:
            try:
                self.event_callback("optimizer.completed", payload)
            except Exception:
                pass


class KaiApiOHLCVFetcher:
    """Simple OHLCV fetcher backed by the existing cloud market-data client."""

    async def fetch(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        from agent.data_sources.kai_api import fetch_candles

        raw_bars = await asyncio.to_thread(fetch_candles, symbol, timeframe, bars)
        return _bars_to_frame(raw_bars)


def create_strategy_tools(session_context: Any) -> list[StructuredTool]:
    """Bind the session-scoped ASO tools into the main agent runtime."""

    def _json_result(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    return [
        StructuredTool.from_function(
            func=lambda pool="candidates": _json_result(list_strategies(session_context, pool=pool)),
            name="list_strategies",
            description=(
                "List ASO strategies from one pool. Input: optional pool "
                "('candidates', 'active', 'graveyard', or 'all')."
            ),
        ),
        StructuredTool.from_function(
            func=lambda name, version=None: _json_result(show_strategy(session_context, name=name, version=version)),
            name="show_strategy",
            description="Show one ASO strategy with YAML, latest metrics, and lineage summary. Inputs: name, optional version.",
        ),
        StructuredTool.from_function(
            func=lambda yaml_str: _json_result(propose_strategy(session_context, yaml_str=yaml_str)),
            name="propose_strategy",
            description="Compile and save a user-provided strategy YAML document into the candidates pool. Input: yaml_str.",
        ),
        StructuredTool.from_function(
            func=lambda: _json_result(optimizer_status(session_context)),
            name="optimizer_status",
            description="Return ASO optimizer status including running state, recent cycle info, and pool counts.",
        ),
        StructuredTool.from_function(
            func=lambda max_cycles=10: _json_result(optimizer_start(session_context, max_cycles=max_cycles)),
            name="optimizer_start",
            description="Start the ASO optimizer loop in the background. Input: optional max_cycles integer.",
        ),
        StructuredTool.from_function(
            func=lambda: _json_result(optimizer_pause(session_context)),
            name="optimizer_pause",
            description="Pause the currently running ASO optimizer loop.",
        ),
        StructuredTool.from_function(
            func=lambda name: _json_result(get_strategy_report(session_context, name=name)),
            name="get_strategy_report",
            description="Return the full mutation, lineage, acceptance, and best-metrics report for one strategy name.",
        ),
    ]


def list_strategies(session_context: Any, pool: str = "candidates") -> dict[str, Any]:
    """List strategy records and the latest Sharpe per strategy version."""
    try:
        normalized_pool = pool.strip().lower() if isinstance(pool, str) else "candidates"
        if normalized_pool == "all":
            records = _list_all_strategies(_get_store(session_context))
        elif normalized_pool in _KNOWN_POOLS:
            records = _get_store(session_context).list_strategies(normalized_pool)
        else:
            return _error("list_strategies", f"unknown pool '{pool}'")

        payload = []
        store = _get_store(session_context)
        for record in records:
            latest_run = _get_latest_run_summary(store, record.id)
            payload.append(
                {
                    "id": record.id,
                    "name": record.name,
                    "version": record.version,
                    "pool": record.pool,
                    "created_at": record.created_at,
                    "created_by": record.created_by,
                    "latest_run": latest_run,
                }
            )

        return {
            "ok": True,
            "kind": "list_strategies",
            "pool": normalized_pool,
            "count": len(payload),
            "strategies": payload,
        }
    except Exception as exc:  # noqa: BLE001
        return _error("list_strategies", str(exc))


def show_strategy(session_context: Any, name: str, version: int | None = None) -> dict[str, Any]:
    """Show one strategy version with YAML, metrics, and lineage summary."""
    try:
        store = _get_store(session_context)
        record = _find_strategy_record(store, name=name, version=version)
        lineage = store.get_lineage(record.name)
        latest_run = _get_latest_run_summary(store, record.id)
        return {
            "ok": True,
            "kind": "show_strategy",
            "strategy": {
                "id": record.id,
                "name": record.name,
                "version": record.version,
                "pool": record.pool,
                "created_at": record.created_at,
                "created_by": record.created_by,
                "parent_id": record.parent_id,
                "yaml": _strategy_yaml(record),
                "latest_run": latest_run,
                "lineage_summary": _lineage_summary(lineage, store),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return _error("show_strategy", str(exc))


def propose_strategy(session_context: Any, yaml_str: str) -> dict[str, Any]:
    """Compile and save a user-provided YAML strategy into the candidates pool."""
    try:
        if not isinstance(yaml_str, str) or not yaml_str.strip():
            return _error("propose_strategy", "strategy YAML cannot be empty")
        store = _get_store(session_context)
        ir = compile_strategy(yaml_str)
        existing = store.get_lineage(ir.name)
        next_version = (max(entry.strategy.version for entry in existing) + 1) if existing else 1
        parent_id = existing[-1].strategy.id if existing else None
        strategy_id = store.save_strategy(
            ir,
            ir.name,
            next_version,
            parent_id=parent_id,
            pool="candidates",
            created_by="human",
            yaml_source=yaml_str,
        )
        return {
            "ok": True,
            "kind": "propose_strategy",
            "strategy": {
                "id": strategy_id,
                "name": ir.name,
                "version": next_version,
                "pool": "candidates",
                "parent_id": parent_id,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return _error("propose_strategy", str(exc))


def optimizer_status(session_context: Any) -> dict[str, Any]:
    """Return optimizer state, pool counts, and last-cycle context."""
    try:
        runtime = _get_runtime(session_context)
        store = _get_store(session_context)
        strategies = _list_all_strategies(store)
        pool_counts = {pool: 0 for pool in _KNOWN_POOLS}
        for record in strategies:
            pool_counts[record.pool] = pool_counts.get(record.pool, 0) + 1
        state = runtime.optimizer_state()
        today = _utc_today()
        cycles_today = sum(1 for result in runtime.recent_cycle_results(limit=10_000) if result.get("completed_at", "").startswith(today))
        return {
            "ok": True,
            "kind": "optimizer_status",
            "running": bool(state.get("running")),
            "paused": bool(state.get("paused")),
            "started_at": state.get("started_at"),
            "last_completed_at": state.get("last_completed_at"),
            "last_stop_reason": state.get("last_stop_reason"),
            "last_error": state.get("last_error"),
            "requested_cycles": state.get("requested_cycles"),
            "cycles_completed_today": cycles_today,
            "pool_counts": pool_counts,
            "last_cycle_result": state.get("last_cycle_result"),
        }
    except Exception as exc:  # noqa: BLE001
        return _error("optimizer_status", str(exc))


def optimizer_start(session_context: Any, max_cycles: int = 10) -> dict[str, Any]:
    """Start the background optimizer loop for the current session."""
    try:
        return _get_runtime(session_context).start_optimizer(_get_store(session_context), int(max_cycles))
    except Exception as exc:  # noqa: BLE001
        return _error("optimizer_start", str(exc))


def optimizer_pause(session_context: Any) -> dict[str, Any]:
    """Pause the running optimizer loop for the current session."""
    try:
        return _get_runtime(session_context).pause_optimizer()
    except Exception as exc:  # noqa: BLE001
        return _error("optimizer_pause", str(exc))


def optimizer_report(session_context: Any, limit: int = 5) -> dict[str, Any]:
    """Return the most recent optimizer cycle results."""
    try:
        return {
            "ok": True,
            "kind": "optimizer_report",
            "cycles": _get_runtime(session_context).recent_cycle_results(limit=limit),
        }
    except Exception as exc:  # noqa: BLE001
        return _error("optimizer_report", str(exc))


def move_strategy(session_context: Any, name: str, to_pool: str, approved_by: str = "human") -> dict[str, Any]:
    """Move the latest matching strategy version into a new pool."""
    try:
        normalized_target = to_pool.strip().lower()
        if normalized_target not in _KNOWN_POOLS:
            return _error("move_strategy", f"unknown pool '{to_pool}'")

        store = _get_store(session_context)
        source_pool = _source_pool_for_target(normalized_target)
        try:
            record = _find_strategy_record(store, name=name, pool=source_pool)
        except ValueError:
            if normalized_target != "graveyard":
                raise
            source_pool = "candidates"
            record = _find_strategy_record(store, name=name, pool=source_pool)
        approval_id = store.promote_strategy(
            record.id,
            source_pool,
            normalized_target,
            approved_by,
        )
        return {
            "ok": True,
            "kind": "move_strategy",
            "action": _action_name(source_pool, normalized_target),
            "approval_id": approval_id,
            "strategy": {
                "id": record.id,
                "name": record.name,
                "version": record.version,
                "from_pool": source_pool,
                "to_pool": normalized_target,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return _error("move_strategy", str(exc))


def get_strategy_lineage(session_context: Any, name: str) -> dict[str, Any]:
    """Return the version lineage for one strategy name."""
    try:
        store = _get_store(session_context)
        lineage = store.get_lineage(name)
        if not lineage:
            return _error("strategy_lineage", f"strategy '{name}' was not found")
        return {
            "ok": True,
            "kind": "strategy_lineage",
            "name": name,
            "summary": _lineage_summary(lineage, store),
            "versions": [_lineage_entry_payload(entry, store) for entry in lineage],
        }
    except Exception as exc:  # noqa: BLE001
        return _error("strategy_lineage", str(exc))


def get_strategy_report(session_context: Any, name: str) -> dict[str, Any]:
    """Return the full mutation, acceptance, and lineage report for one strategy."""
    try:
        store = _get_store(session_context)
        lineage = store.get_lineage(name)
        if not lineage:
            return _error("strategy_report", f"strategy '{name}' was not found")

        best_entry = max(
            lineage,
            key=lambda entry: (_sharpe_or_neg_inf(_get_latest_run_summary(store, entry.strategy.id)), entry.strategy.version),
        )
        approvals = []
        mutations = []
        for entry in lineage:
            approvals.extend(_approval_history(store, entry.strategy.id))
            if entry.mutation is not None:
                mutations.append(
                    {
                        "version": entry.strategy.version,
                        "accepted": entry.mutation.accepted,
                        "rejection_reason": entry.mutation.rejection_reason,
                        "created_at": entry.mutation.created_at,
                        "mutations": entry.mutation.mutations,
                    }
                )

        return {
            "ok": True,
            "kind": "strategy_report",
            "name": name,
            "summary": _lineage_summary(lineage, store),
            "best_version": {
                "version": best_entry.strategy.version,
                "pool": best_entry.strategy.pool,
                "latest_run": _get_latest_run_summary(store, best_entry.strategy.id),
            },
            "lineage": [_lineage_entry_payload(entry, store) for entry in lineage],
            "mutations": mutations,
            "approvals": approvals,
        }
    except Exception as exc:  # noqa: BLE001
        return _error("strategy_report", str(exc))


def render_strategy_command_result(result: dict[str, Any]) -> str:
    """Render one strategy/optimizer payload for slash-command output."""
    if not result.get("ok"):
        return f"Error: {result.get('error', 'unknown error')}"

    kind = result.get("kind")
    if kind == "list_strategies":
        strategies = result.get("strategies", [])
        if not strategies:
            return f"No strategies in pool {result.get('pool')}."
        lines = [f"Strategies ({result.get('pool')}):"]
        for item in strategies:
            latest = item.get("latest_run") or {}
            sharpe = _format_float(latest.get("sharpe_ratio"))
            stage = latest.get("stage") or "n/a"
            lines.append(
                f"- {item['name']} v{item['version']} [{item['pool']}] "
                f"Sharpe={sharpe} stage={stage}"
            )
        return "\n".join(lines)

    if kind == "show_strategy":
        strategy = result["strategy"]
        latest = strategy.get("latest_run") or {}
        summary = strategy.get("lineage_summary") or {}
        lines = [
            f"{strategy['name']} v{strategy['version']} [{strategy['pool']}]",
            f"Latest run: stage={latest.get('stage', 'n/a')} Sharpe={_format_float(latest.get('sharpe_ratio'))} "
            f"trades={latest.get('trade_count', 'n/a')} drawdown={_format_float(latest.get('max_drawdown_pct'))}",
            f"Lineage: versions={summary.get('versions', 0)} accepted={summary.get('accepted_versions', 0)} "
            f"rejected={summary.get('rejected_versions', 0)}",
            "",
            strategy.get("yaml", ""),
        ]
        return "\n".join(lines).rstrip()

    if kind == "propose_strategy":
        strategy = result["strategy"]
        return (
            f"Saved {strategy['name']} v{strategy['version']} to candidates "
            f"as {strategy['id']}."
        )

    if kind == "optimizer_status":
        last = result.get("last_cycle_result") or {}
        lines = [
            f"Optimizer: {'running' if result.get('running') else 'idle'}"
            + (" (paused)" if result.get("paused") else ""),
            f"Cycles today: {result.get('cycles_completed_today', 0)}",
            "Pools: "
            + ", ".join(
                f"{pool}={count}" for pool, count in sorted((result.get("pool_counts") or {}).items())
            ),
        ]
        if last:
            lines.append(
                f"Last cycle: status={last.get('status')} reason={last.get('reason')}"
            )
        if result.get("last_error"):
            lines.append(f"Last error: {result['last_error']}")
        return "\n".join(lines)

    if kind == "optimizer_start":
        return f"Optimizer started for {result.get('max_cycles')} cycle(s)."

    if kind == "optimizer_pause":
        return "Optimizer paused."

    if kind == "optimizer_report":
        cycles = result.get("cycles", [])
        if not cycles:
            return "No optimizer cycle results yet."
        lines = ["Recent optimizer cycles:"]
        for item in cycles:
            lines.append(
                f"- {item.get('completed_at', 'n/a')} status={item.get('status')} reason={item.get('reason')}"
            )
        return "\n".join(lines)

    if kind == "move_strategy":
        strategy = result["strategy"]
        return (
            f"{result.get('action', 'moved').capitalize()} {strategy['name']} v{strategy['version']} "
            f"from {strategy['from_pool']} to {strategy['to_pool']}."
        )

    if kind == "strategy_lineage":
        versions = result.get("versions", [])
        lines = [f"Lineage for {result.get('name')}:"] if versions else ["No lineage found."]
        for item in versions:
            mutation = item.get("mutation") or {}
            suffix = ""
            if mutation:
                outcome = "accepted" if mutation.get("accepted") else "rejected"
                suffix = f" mutation={outcome}"
            lines.append(f"- v{item['version']} [{item['pool']}] id={item['id']}{suffix}")
        return "\n".join(lines)

    if kind == "strategy_report":
        best = result.get("best_version") or {}
        lines = [
            f"Strategy report for {result.get('name')}:",
            f"Best version: v{best.get('version')} [{best.get('pool')}] Sharpe={_format_float((best.get('latest_run') or {}).get('sharpe_ratio'))}",
            f"Mutations: {len(result.get('mutations', []))}",
            f"Approvals: {len(result.get('approvals', []))}",
        ]
        return "\n".join(lines)

    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _get_store(session_context: Any) -> StrategyStore:
    store = getattr(session_context, "_strategy_store", None)
    if store is None:
        store = StrategyStore(_resolve_store_path(session_context))
        store.init_db()
        setattr(session_context, "_strategy_store", store)
    return store


def _get_runtime(session_context: Any) -> OptimizerRuntime:
    runtime = getattr(session_context, "strategy_runtime", None)
    if runtime is None:
        runtime = InProcessStrategyRuntime(
            session_name=str(getattr(session_context, "name", "default")),
            agent_name=str(getattr(session_context, "agent_name", None) or "kai"),
            event_callback=lambda topic, payload: _publish_session_event(session_context, topic, payload),
        )
        setattr(session_context, "strategy_runtime", runtime)
    return runtime


def _resolve_store_path(session_context: Any) -> Path:
    """Resolve the global strategy store path.

    Strategies are shared across all sessions (not per-session)
    because they represent trading knowledge, not conversation
    state. The store lives at workspaces/strategies/aso.db.
    """
    explicit = getattr(session_context, "strategy_store_path", None)
    if explicit:
        return Path(explicit)
    # Global store — shared across all sessions
    return Path(DEFAULT_DB_PATH)


def _publish_session_event(session_context: Any, topic: str, payload: dict[str, Any]) -> None:
    publisher = getattr(session_context, "publish_event", None)
    if callable(publisher):
        publisher(topic, payload)


def _resolve_agent_endpoint(agent_name: str) -> dict[str, Any] | None:
    cfg = get_agent_config(agent_name)
    return cfg.get("endpoint") if isinstance(cfg, dict) else None


def _list_all_strategies(store: StrategyStore) -> list[StrategyRecord]:
    records: list[StrategyRecord] = []
    for pool in _KNOWN_POOLS:
        records.extend(store.list_strategies(pool))
    return sorted(records, key=lambda record: (record.name, record.version))


def _find_strategy_record(
    store: StrategyStore,
    *,
    name: str,
    version: int | None = None,
    pool: str | None = None,
) -> StrategyRecord:
    lineage = store.get_lineage(name)
    if not lineage:
        raise ValueError(f"strategy '{name}' was not found")
    candidates = [entry.strategy for entry in lineage if pool is None or entry.strategy.pool == pool]
    if not candidates:
        raise ValueError(f"strategy '{name}' has no versions in pool '{pool}'")
    if version is None:
        return max(candidates, key=lambda record: record.version)
    for record in candidates:
        if record.version == version:
            return record
    raise ValueError(f"strategy '{name}' version {version} was not found")


def _source_pool_for_target(to_pool: str) -> str:
    if to_pool == "active":
        return "candidates"
    if to_pool == "candidates":
        return "active"
    return "active"


def _action_name(from_pool: str, to_pool: str) -> str:
    if to_pool == "graveyard":
        return "retired"
    if from_pool == "active":
        return "demoted"
    return "promoted"


def _strategy_yaml(record: StrategyRecord) -> str:
    if record.yaml_source:
        return record.yaml_source.strip()
    return yaml.safe_dump(record.ir.model_dump(mode="json"), sort_keys=False).strip()


def _lineage_summary(lineage: list[LineageEntry], store: StrategyStore) -> dict[str, Any]:
    accepted = 0
    rejected = 0
    best_version: int | None = None
    best_sharpe = float("-inf")
    for entry in lineage:
        if entry.mutation is not None:
            if entry.mutation.accepted:
                accepted += 1
            else:
                rejected += 1
        latest = _get_latest_run_summary(store, entry.strategy.id)
        sharpe = _sharpe_or_neg_inf(latest)
        if sharpe > best_sharpe:
            best_sharpe = sharpe
            best_version = entry.strategy.version
    return {
        "versions": len(lineage),
        "accepted_versions": accepted,
        "rejected_versions": rejected,
        "best_version": best_version,
    }


def _lineage_entry_payload(entry: LineageEntry, store: StrategyStore) -> dict[str, Any]:
    mutation = None
    if entry.mutation is not None:
        mutation = {
            "accepted": entry.mutation.accepted,
            "rejection_reason": entry.mutation.rejection_reason,
            "created_at": entry.mutation.created_at,
            "mutations": entry.mutation.mutations,
        }
    return {
        "id": entry.strategy.id,
        "version": entry.strategy.version,
        "pool": entry.strategy.pool,
        "created_at": entry.strategy.created_at,
        "created_by": entry.strategy.created_by,
        "latest_run": _get_latest_run_summary(store, entry.strategy.id),
        "mutation": mutation,
    }


def _approval_history(store: StrategyStore, strategy_id: str) -> list[dict[str, Any]]:
    rows = _query_rows(
        store,
        """
        SELECT action, from_pool, to_pool, approved_by, evaluation_run_id, created_at
        FROM approvals
        WHERE strategy_id = ?
        ORDER BY created_at ASC, rowid ASC
        """,
        (strategy_id,),
    )
    return [dict(row) for row in rows]


def _get_latest_run_summary(store: StrategyStore, strategy_id: str) -> dict[str, Any] | None:
    rows = _query_rows(
        store,
        """
        SELECT stage, fold_index, metrics_json, sample_size_pass, sample_size_detail, created_at
        FROM runs
        WHERE strategy_id = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """,
        (strategy_id,),
    )
    if not rows:
        return None
    row = rows[0]
    metrics = _metrics_payload(json.loads(row["metrics_json"]))
    return {
        "stage": row["stage"],
        "fold_index": row["fold_index"],
        "sample_size_pass": bool(row["sample_size_pass"]),
        "sample_size_detail": row["sample_size_detail"],
        "created_at": row["created_at"],
        **metrics,
    }


def _metrics_payload(metrics_json: dict[str, Any]) -> dict[str, Any]:
    risk = metrics_json.get("risk_adjusted") or {}
    drawdown = metrics_json.get("drawdown") or {}
    trades = metrics_json.get("trades") or {}
    returns = metrics_json.get("returns") or {}
    return {
        "sharpe_ratio": risk.get("sharpe_ratio"),
        "sortino_ratio": risk.get("sortino_ratio"),
        "calmar_ratio": risk.get("calmar_ratio"),
        "max_drawdown_pct": drawdown.get("max_drawdown_pct"),
        "trade_count": trades.get("total"),
        "win_rate_pct": trades.get("win_rate_pct"),
        "profit_factor": trades.get("profit_factor"),
        "total_return_pct": returns.get("total_pct"),
        "annualized_return_pct": returns.get("annualized_pct"),
    }


def _query_rows(store: StrategyStore, query: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    db_path = Path(store.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path), check_same_thread=False) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def _serialize_cycle_result(result: Any) -> dict[str, Any]:
    payload = asdict(result)
    payload["completed_at"] = _utc_now_iso()
    return payload


def _bars_to_frame(raw_bars: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(raw_bars)
    if frame.empty:
        raise ValueError("no OHLCV bars returned")
    if "ts" not in frame.columns:
        raise ValueError("OHLCV bars are missing timestamps")
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame = frame.set_index("ts").sort_index()
    columns = ["open", "high", "low", "close", "volume"]
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"OHLCV bars are missing columns: {', '.join(missing)}")
    return frame[columns]


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "kind": kind, "error": message}


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


def _sharpe_or_neg_inf(latest_run: dict[str, Any] | None) -> float:
    if latest_run is None:
        return float("-inf")
    sharpe = latest_run.get("sharpe_ratio")
    return float(sharpe) if sharpe is not None else float("-inf")


def _format_float(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)

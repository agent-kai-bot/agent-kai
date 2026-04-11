"""Autonomous strategy optimization loop for ASO Priority 3."""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd

from agent.strategy_diagnostics import DiagnosticResult, diagnose_strategy
from agent.strategy_executor import execute_strategy
from agent.strategy_metrics import MetricsReport, compute_metrics
from agent.strategy_mutator import (
    AsyncMutationLLM,
    Mutation,
    apply_mutations,
    create_optimizer_llm_client,
    mutation_to_dict,
    parameter_tune,
    structural_mutate,
)
from agent.strategy_prompts import analyst_prompt_hash
from agent.strategy_sample_size import check_sample_size
from agent.strategy_selector import select_next_strategy
from agent.strategy_store import LineageEntry, StrategyRecord, StrategyStore
from agent.strategy_walkforward import WalkForwardResult, walk_forward_evaluate


class OHLCVFetcher(Protocol):
    async def fetch(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame: ...


@dataclass(frozen=True)
class OptimizerConfig:
    max_cycles_per_day: int = 100
    cycle_interval_seconds: int = 300
    exploration_rate: float = 0.2
    max_lineage_iterations: int = 50
    stagnation_threshold: int = 10
    param_tune_fraction: float = 0.6
    history_bars: int = 600


@dataclass(frozen=True)
class CycleResult:
    status: str
    reason: str
    selected_strategy_id: str | None = None
    parent_strategy_id: str | None = None
    child_strategy_id: str | None = None
    accepted: bool | None = None
    mutation_mode: str | None = None
    mutations: list[Mutation] = field(default_factory=list)
    diagnostic: DiagnosticResult | None = None


@dataclass(frozen=True)
class CandidateValidation:
    in_sample_metrics: MetricsReport
    walk_forward: WalkForwardResult
    lockbox_metrics: MetricsReport
    lockbox_pass: bool
    lockbox_detail: str


def accept_child(
    parent_metrics: WalkForwardResult,
    child_metrics: MetricsReport,
    child_wf: CandidateValidation,
) -> tuple[bool, str]:
    """Simplified v1 acceptance: WF Sharpe improvement + WF hard pass + lockbox pass."""
    del child_metrics

    if not child_wf.walk_forward.all_folds_pass:
        return False, child_wf.walk_forward.rejection_reason or "walk-forward validation failed"
    if not child_wf.lockbox_pass:
        return False, child_wf.lockbox_detail

    parent_sharpe = parent_metrics.median_sharpe
    child_sharpe = child_wf.walk_forward.median_sharpe
    if child_sharpe is None:
        return False, "child walk-forward Sharpe is unavailable"
    if parent_sharpe is None:
        return True, "accepted: child established positive walk-forward Sharpe"
    if child_sharpe <= parent_sharpe:
        return False, f"child walk-forward Sharpe {child_sharpe:.3f} did not exceed parent {parent_sharpe:.3f}"
    return True, "accepted: child improved walk-forward Sharpe and passed lockbox"


class StrategyOptimizer:
    """Run one or many autonomous ASO optimization cycles."""

    def __init__(
        self,
        store: StrategyStore,
        llm_client: AsyncMutationLLM | None,
        ohlcv_fetcher: OHLCVFetcher,
        config: OptimizerConfig,
        *,
        rng: random.Random | None = None,
    ):
        self.store = store
        self.llm_client = llm_client or create_optimizer_llm_client()
        self.ohlcv_fetcher = ohlcv_fetcher
        self.config = config
        self.rng = rng or random.Random()
        self.cycles_run = 0

    async def run_one_cycle(self) -> CycleResult:
        selected_id = select_next_strategy(
            self.store,
            exploration_rate=self.config.exploration_rate,
            max_lineage_iterations=self.config.max_lineage_iterations,
            rng=self.rng,
        )
        if selected_id is None:
            return CycleResult(status="pool_exhausted", reason="No candidate lineage available")

        selected_record = self.store.get_strategy_record(selected_id)
        lineage = self.store.get_lineage(selected_record.name)
        parent_record = self._resolve_parent_record(selected_record, lineage)
        mutation_mode = self._choose_mutation_mode(lineage, parent_record.id != selected_record.id)

        ohlcv = await self.ohlcv_fetcher.fetch(parent_record.ir.symbol, parent_record.ir.timeframe, self.config.history_bars)
        tuning_frame, lockbox_frame = self._split_ohlcv(ohlcv, parent_record.ir.warmup_bars)

        parent_in_sample = self._evaluate_in_sample(parent_record.id, parent_record.ir, tuning_frame, "parent_in_sample")
        parent_history = self._build_iteration_history(lineage)
        diagnostic = diagnose_strategy(parent_in_sample, parent_history)
        if not diagnostic.proceed:
            return CycleResult(
                status="skipped",
                reason=diagnostic.reason,
                selected_strategy_id=selected_id,
                parent_strategy_id=parent_record.id,
                diagnostic=diagnostic,
            )

        lessons = self._build_lessons_learned(lineage)
        proposed_mutations = await self._propose_mutations(
            parent_record.ir,
            parent_in_sample,
            diagnostic,
            parent_history,
            lessons,
            mutation_mode,
        )
        filtered_mutations = [mutation for mutation in proposed_mutations if self._is_novel_mutation(lineage, mutation)]
        if not filtered_mutations:
            return CycleResult(
                status="rejected",
                reason="No novel supported mutation candidates",
                selected_strategy_id=selected_id,
                parent_strategy_id=parent_record.id,
                accepted=False,
                mutation_mode=mutation_mode,
                diagnostic=diagnostic,
            )

        mutation_batch = filtered_mutations if diagnostic.bundled_hypothesis and mutation_mode == "llm_structural" else filtered_mutations[:1]
        child_ir = apply_mutations(parent_record.ir, mutation_batch)
        if child_ir == parent_record.ir:
            return CycleResult(
                status="rejected",
                reason="Mutation batch failed IR validation",
                selected_strategy_id=selected_id,
                parent_strategy_id=parent_record.id,
                accepted=False,
                mutation_mode=mutation_mode,
                mutations=mutation_batch,
                diagnostic=diagnostic,
            )

        child_version = max(entry.strategy.version for entry in lineage) + 1
        child_validation = self._validate_candidate(child_ir, tuning_frame, lockbox_frame)
        parent_walk_forward = walk_forward_evaluate(parent_record.ir, tuning_frame)
        accepted, decision_reason = accept_child(parent_walk_forward, child_validation.in_sample_metrics, child_validation)
        child_pool = "candidates" if accepted else "graveyard"
        child_id = self.store.save_strategy(
            child_ir,
            parent_record.name,
            child_version,
            parent_id=parent_record.id,
            pool=child_pool,
            created_by="optimizer",
        )
        self._persist_validation_runs(child_id, tuning_frame, lockbox_frame, child_validation)
        self.store.save_mutation(
            parent_record.id,
            child_id,
            [mutation_to_dict(mutation) for mutation in mutation_batch],
            accepted=accepted,
            rejection_reason=None if accepted else decision_reason,
            llm_model=getattr(self.llm_client, "model_name", None) if mutation_mode == "llm_structural" else None,
            llm_prompt_hash=analyst_prompt_hash() if mutation_mode == "llm_structural" else None,
        )

        return CycleResult(
            status="accepted" if accepted else "rejected",
            reason=decision_reason,
            selected_strategy_id=selected_id,
            parent_strategy_id=parent_record.id,
            child_strategy_id=child_id,
            accepted=accepted,
            mutation_mode=mutation_mode,
            mutations=mutation_batch,
            diagnostic=diagnostic,
        )

    async def run_loop(self, max_cycles: int = 100) -> list[CycleResult]:
        """Run repeated optimization cycles until exhaustion, budget, or error."""
        limit = min(max_cycles, self.config.max_cycles_per_day)
        results: list[CycleResult] = []
        for cycle_index in range(limit):
            result = await self.run_one_cycle()
            results.append(result)
            self.cycles_run += 1
            if result.status in {"pool_exhausted", "error"}:
                break
            if cycle_index < limit - 1 and self.config.cycle_interval_seconds > 0:
                await asyncio.sleep(self.config.cycle_interval_seconds)
        return results

    def _resolve_parent_record(self, selected: StrategyRecord, lineage: list[LineageEntry]) -> StrategyRecord:
        if self._consecutive_rejections(lineage) < self.config.stagnation_threshold:
            return selected
        return self._best_lineage_record(lineage)

    def _choose_mutation_mode(self, lineage: list[LineageEntry], forced_structural: bool) -> str:
        if forced_structural:
            return "llm_structural"
        if self.config.param_tune_fraction <= 0.0:
            return "llm_structural"
        if self.config.param_tune_fraction >= 1.0:
            return "parameter_tune"
        mutation_count = sum(1 for entry in lineage if entry.mutation is not None)
        schedule_size = 10
        param_slots = max(1, min(schedule_size - 1, round(self.config.param_tune_fraction * schedule_size)))
        return "parameter_tune" if mutation_count % schedule_size < param_slots else "llm_structural"

    async def _propose_mutations(
        self,
        ir,
        metrics,
        diagnostic,
        iteration_history,
        lessons,
        mutation_mode,
    ) -> list[Mutation]:
        if mutation_mode == "parameter_tune":
            return parameter_tune(ir, metrics, diagnostic)
        return await structural_mutate(ir, metrics, diagnostic, iteration_history, lessons, self.llm_client)

    def _evaluate_in_sample(self, strategy_id: str, ir, frame: pd.DataFrame, stage: str) -> MetricsReport:
        backtest = execute_strategy(ir, frame)
        metrics = compute_metrics(backtest.equity_curve, backtest.trades, backtest.benchmark_prices)
        self.store.save_run(
            strategy_id,
            stage,
            None,
            self._hash_frame(frame),
            metrics,
            True,
            dataset_range=self._dataset_range(frame),
            sample_size_detail="not gated for in_sample",
        )
        return metrics

    def _validate_candidate(self, ir, tuning_frame: pd.DataFrame, lockbox_frame: pd.DataFrame) -> CandidateValidation:
        backtest = execute_strategy(ir, tuning_frame)
        in_sample_metrics = compute_metrics(backtest.equity_curve, backtest.trades, backtest.benchmark_prices)
        walk_forward = walk_forward_evaluate(ir, tuning_frame)
        lockbox_metrics, lockbox_pass, lockbox_detail = self._evaluate_lockbox(ir, tuning_frame, lockbox_frame)
        return CandidateValidation(
            in_sample_metrics=in_sample_metrics,
            walk_forward=walk_forward,
            lockbox_metrics=lockbox_metrics,
            lockbox_pass=lockbox_pass,
            lockbox_detail=lockbox_detail,
        )

    def _evaluate_lockbox(
        self,
        ir,
        tuning_frame: pd.DataFrame,
        lockbox_frame: pd.DataFrame,
    ) -> tuple[MetricsReport, bool, str]:
        warmup_prefix = tuning_frame.iloc[-ir.warmup_bars:] if ir.warmup_bars > 0 else tuning_frame.iloc[0:0]
        exec_frame = pd.concat([warmup_prefix, lockbox_frame])
        backtest = execute_strategy(ir, exec_frame)
        metrics = compute_metrics(backtest.equity_curve, backtest.trades, backtest.benchmark_prices)
        avg_bars_held = metrics.trades.avg_duration_bars or 0.0
        sample_pass, sample_detail = check_sample_size(metrics.trades.total, avg_bars_held, len(lockbox_frame), "lockbox")
        if not sample_pass:
            return metrics, False, sample_detail
        sharpe = metrics.risk_adjusted.sharpe_ratio
        if sharpe is None or sharpe <= 0:
            return metrics, False, "lockbox Sharpe must be positive"
        return metrics, True, "ok"

    def _persist_validation_runs(
        self,
        child_id: str,
        tuning_frame: pd.DataFrame,
        lockbox_frame: pd.DataFrame,
        validation: CandidateValidation,
    ) -> None:
        tuning_hash = self._hash_frame(tuning_frame)
        self.store.save_run(
            child_id,
            "in_sample",
            None,
            tuning_hash,
            validation.in_sample_metrics,
            True,
            dataset_range=self._dataset_range(tuning_frame),
            sample_size_detail="not gated for in_sample",
        )
        for fold in validation.walk_forward.folds:
            self.store.save_run(
                child_id,
                "walk_forward",
                fold.fold_index,
                tuning_hash,
                fold.metrics,
                fold.sample_size_pass,
                dataset_range=self._dataset_range(tuning_frame),
                sample_size_detail=fold.hard_constraint_detail,
            )
        self.store.save_run(
            child_id,
            "lockbox",
            None,
            self._hash_frame(lockbox_frame),
            validation.lockbox_metrics,
            validation.lockbox_pass,
            dataset_range=self._dataset_range(lockbox_frame),
            sample_size_detail=validation.lockbox_detail,
        )

    def _split_ohlcv(self, frame: pd.DataFrame, warmup_bars: int) -> tuple[pd.DataFrame, pd.DataFrame]:
        sorted_frame = frame.sort_index()
        if len(sorted_frame) <= warmup_bars * 2:
            raise ValueError("not enough OHLCV bars for optimization split")
        tuning_end = max(warmup_bars + 1, int(len(sorted_frame) * 0.6))
        lockbox_end = max(tuning_end + 1, int(len(sorted_frame) * 0.8))
        lockbox_end = min(lockbox_end, len(sorted_frame))
        tuning_frame = sorted_frame.iloc[:tuning_end]
        lockbox_frame = sorted_frame.iloc[tuning_end:lockbox_end]
        if len(lockbox_frame) == 0:
            raise ValueError("lockbox split is empty")
        return tuning_frame, lockbox_frame

    def _build_iteration_history(self, lineage: list[LineageEntry]) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for entry in lineage:
            if entry.mutation is None:
                continue
            history.append(
                {
                    "version": entry.strategy.version,
                    "accepted": entry.mutation.accepted,
                    "rejection_reason": entry.mutation.rejection_reason,
                    "mutations": entry.mutation.mutations,
                }
            )
        return history[-5:]

    def _build_lessons_learned(self, lineage: list[LineageEntry]) -> list[dict[str, Any]]:
        lessons: list[dict[str, Any]] = []
        for entry in lineage:
            if entry.mutation is None or entry.mutation.accepted:
                continue
            lessons.append(
                {
                    "strategy_id": entry.strategy.id,
                    "rejection_reason": entry.mutation.rejection_reason,
                    "mutations": entry.mutation.mutations,
                }
            )
        return lessons

    def _is_novel_mutation(self, lineage: list[LineageEntry], mutation: Mutation) -> bool:
        signature = (mutation.yaml_path, self._normalize_value(mutation.new_value))
        for entry in lineage:
            if entry.mutation is None or entry.mutation.accepted:
                continue
            for prior in entry.mutation.mutations:
                prior_signature = (prior.get("path"), self._normalize_value(prior.get("new")))
                if prior_signature == signature:
                    return False
        return True

    def _consecutive_rejections(self, lineage: list[LineageEntry]) -> int:
        count = 0
        for entry in reversed(lineage):
            if entry.mutation is None:
                break
            if entry.mutation.accepted:
                break
            count += 1
        return count

    def _best_lineage_record(self, lineage: list[LineageEntry]) -> StrategyRecord:
        best_record = lineage[-1].strategy
        best_sharpe = float("-inf")
        for entry in lineage:
            run = self.store.get_latest_run(entry.strategy.id, "in_sample")
            sharpe = run.metrics.risk_adjusted.sharpe_ratio if run is not None else None
            if sharpe is not None and sharpe > best_sharpe:
                best_record = entry.strategy
                best_sharpe = sharpe
        return best_record

    @staticmethod
    def _hash_frame(frame: pd.DataFrame) -> str:
        payload = frame.sort_index().to_csv().encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _dataset_range(frame: pd.DataFrame) -> str:
        return f"{frame.index[0].isoformat()}/{frame.index[-1].isoformat()}"

    @staticmethod
    def _normalize_value(value: Any) -> Any:
        if isinstance(value, dict):
            return tuple(sorted((key, StrategyOptimizer._normalize_value(inner)) for key, inner in value.items()))
        if isinstance(value, list):
            return tuple(StrategyOptimizer._normalize_value(item) for item in value)
        return value

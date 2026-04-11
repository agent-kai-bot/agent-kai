"""Strategy mutation utilities for ASO autonomous optimization."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import yaml
from pydantic import ValidationError

from agent.core import create_llm
from agent.strategy_diagnostics import DiagnosticResult
from agent.strategy_ir import AtrExitSpec, StrategyIR
from agent.strategy_metrics import MetricsReport
from agent.strategy_prompts import render_analyst_prompt

_PATH_PATTERNS = (
    re.compile(r"^symbol$"),
    re.compile(r"^timeframe$"),
    re.compile(r"^indicators$"),
    re.compile(r"^indicators\[\d+\]$"),
    re.compile(r"^indicators\[\d+\]\.(type|period|alias|source|band|stddev)$"),
    re.compile(r"^entry\.conditions$"),
    re.compile(r"^entry\.conditions\[\d+\]$"),
    re.compile(r"^entry\.conditions\[\d+\]\.(indicator|operator|target)$"),
    re.compile(r"^exit\.(stop_loss|take_profit|trailing_stop|time_exit)$"),
    re.compile(r"^exit\.(stop_loss|take_profit)\.(type|percent|atr_indicator|multiple)$"),
    re.compile(r"^exit\.trailing_stop\.(type|distance|activation|atr_indicator)$"),
    re.compile(r"^exit\.time_exit\.max_bars$"),
    re.compile(r"^risk\.(max_position_pct|max_drawdown_pct)$"),
    re.compile(r"^costs\.(fee_pct|slippage_pct|spread_pct)$"),
)


@dataclass(frozen=True)
class Mutation:
    description: str
    yaml_path: str
    old_value: Any
    new_value: Any
    rationale: str
    source: str


class AsyncMutationLLM(Protocol):
    """Async interface used by the structural mutator."""

    async def complete(self, prompt: str) -> str: ...


class LangChainMutationLLM:
    """Adapter from the existing agent LLM factory to a tiny async interface."""

    def __init__(self, endpoint_cfg: dict[str, Any] | None = None):
        self._llm = create_llm(endpoint_cfg)
        self.model_name = getattr(self._llm, "model_name", None) or getattr(self._llm, "model", None)

    async def complete(self, prompt: str) -> str:
        response = await self._llm.ainvoke(prompt)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            return "".join(str(part) for part in content)
        return str(content)


def create_optimizer_llm_client(endpoint_cfg: dict[str, Any] | None = None) -> LangChainMutationLLM:
    """Build the optimizer's dedicated async LLM client."""
    return LangChainMutationLLM(endpoint_cfg)


def parameter_tune(ir: StrategyIR, metrics: MetricsReport, diagnostic: DiagnosticResult) -> list[Mutation]:
    """Generate simple one-parameter hill-climb candidates."""
    del metrics

    candidates: list[Mutation] = []
    seen: set[tuple[str, Any]] = set()

    def add(path: str, old: Any, new: Any, description: str, rationale: str) -> None:
        if new == old:
            return
        key = (path, _freeze_value(new))
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            Mutation(
                description=description,
                yaml_path=path,
                old_value=old,
                new_value=new,
                rationale=rationale,
                source="parameter_tune",
            )
        )

    for index, indicator in enumerate(ir.indicators):
        if indicator.type.value == "rsi":
            for delta in (-3, 3):
                new_period = max(1, indicator.period + delta)
                add(
                    f"indicators[{index}].period",
                    indicator.period,
                    new_period,
                    f"Adjust {indicator.alias} RSI period by {delta:+d}",
                    _period_rationale(diagnostic.failure_mode, indicator.alias),
                )
        elif indicator.type.value in {"ema", "sma", "atr", "bbands"}:
            for delta in (-5, 5):
                new_period = max(1, indicator.period + delta)
                add(
                    f"indicators[{index}].period",
                    indicator.period,
                    new_period,
                    f"Adjust {indicator.alias} period by {delta:+d}",
                    _period_rationale(diagnostic.failure_mode, indicator.alias),
                )

    if isinstance(ir.exit.stop_loss, AtrExitSpec):
        for delta in (-0.25, 0.25):
            add(
                "exit.stop_loss.multiple",
                ir.exit.stop_loss.multiple,
                round(max(0.1, ir.exit.stop_loss.multiple + delta), 4),
                f"Adjust ATR stop-loss multiple by {delta:+.2f}",
                _stop_loss_rationale(diagnostic.failure_mode),
            )

    if isinstance(ir.exit.take_profit, AtrExitSpec):
        for delta in (-0.5, 0.5):
            add(
                "exit.take_profit.multiple",
                ir.exit.take_profit.multiple,
                round(max(0.1, ir.exit.take_profit.multiple + delta), 4),
                f"Adjust ATR take-profit multiple by {delta:+.2f}",
                _take_profit_rationale(diagnostic.failure_mode),
            )

    for delta in (-0.01, 0.01):
        add(
            "risk.max_position_pct",
            ir.risk.max_position_pct,
            round(min(1.0, max(0.001, ir.risk.max_position_pct + delta)), 4),
            f"Adjust max position size by {delta:+.2f}",
            _position_rationale(diagnostic.failure_mode),
        )

    ranked = _rank_candidates(candidates, diagnostic.failure_mode)
    return ranked[:5]


async def structural_mutate(
    ir: StrategyIR,
    metrics: MetricsReport,
    diagnostic: DiagnosticResult,
    iteration_history: list[dict],
    lessons_learned: list[dict],
    llm_client: AsyncMutationLLM,
) -> list[Mutation]:
    """Ask the LLM for structural mutations and filter them to supported IR edits."""
    prompt = render_analyst_prompt(
        strategy_yaml=_render_strategy_yaml(ir),
        metrics_json=json.dumps(asdict(metrics), indent=2, sort_keys=True),
        iteration_history=json.dumps(iteration_history[-5:], indent=2, sort_keys=True),
        lessons_learned=json.dumps(lessons_learned, indent=2, sort_keys=True),
        failure_mode=diagnostic.failure_mode,
        bundled_hypothesis=diagnostic.bundled_hypothesis,
    )
    raw_response = await _complete_text(llm_client, prompt)
    payload = _parse_json_payload(raw_response)
    raw_mutations = payload.get("mutations", [])
    if not isinstance(raw_mutations, list):
        return []

    valid: list[Mutation] = []
    for raw_mutation in raw_mutations[:3]:
        if not isinstance(raw_mutation, dict):
            continue
        mutation = Mutation(
            description=str(raw_mutation.get("description", "")).strip(),
            yaml_path=str(raw_mutation.get("yaml_path", "")).strip(),
            old_value=raw_mutation.get("old_value"),
            new_value=raw_mutation.get("new_value"),
            rationale=str(raw_mutation.get("rationale", "")).strip(),
            source="llm_structural",
        )
        if not is_supported_mutation_path(mutation.yaml_path):
            continue
        _, applied = _apply_single_mutation(ir, mutation)
        if applied:
            valid.append(mutation)
    return valid


def apply_mutations(ir: StrategyIR, mutations: list[Mutation]) -> StrategyIR:
    """Apply mutations one at a time, skipping invalid edits."""
    current = ir
    for mutation in mutations:
        current, _ = _apply_single_mutation(current, mutation)
    return current


def is_supported_mutation_path(path: str) -> bool:
    """Return True when the mutation path fits the v1 IR capability matrix."""
    return any(pattern.fullmatch(path) for pattern in _PATH_PATTERNS)


def mutation_to_dict(mutation: Mutation) -> dict[str, Any]:
    """Serialize a mutation for persistence."""
    payload = asdict(mutation)
    payload["path"] = payload.pop("yaml_path")
    payload["old"] = payload.pop("old_value")
    payload["new"] = payload.pop("new_value")
    return payload


def _rank_candidates(candidates: list[Mutation], failure_mode: str | None) -> list[Mutation]:
    priorities = {
        "drawdown": ("risk.max_position_pct", "exit.stop_loss.multiple", "exit.take_profit.multiple"),
        "poor_risk_adjusted": ("indicators", "exit.take_profit.multiple", "risk.max_position_pct"),
        "noisy_entries": ("indicators", "exit.stop_loss.multiple"),
        "poor_win_loss": ("exit.take_profit.multiple", "exit.stop_loss.multiple"),
        "downside_volatility": ("exit.stop_loss.multiple", "risk.max_position_pct"),
        "low_signal": ("indicators",),
        "over_trading": ("indicators", "risk.max_position_pct"),
    }
    preferred = priorities.get(failure_mode or "", ())
    return sorted(
        candidates,
        key=lambda mutation: next(
            (index for index, prefix in enumerate(preferred) if mutation.yaml_path.startswith(prefix)),
            len(preferred),
        ),
    )


def _period_rationale(failure_mode: str | None, alias: str) -> str:
    if failure_mode in {"low_signal", "poor_risk_adjusted"}:
        return f"Adjusting {alias} changes signal sensitivity without altering the overall structure."
    if failure_mode == "over_trading":
        return f"Changing {alias} period can reduce trade frequency by smoothing the trigger."
    return f"Adjusting {alias} changes entry sensitivity around the diagnosed weakness."


def _stop_loss_rationale(failure_mode: str | None) -> str:
    if failure_mode in {"drawdown", "downside_volatility"}:
        return "Tightening the stop should reduce loss severity and compress drawdowns."
    return "Adjusting the stop-loss multiple changes how much adverse movement each trade can absorb."


def _take_profit_rationale(failure_mode: str | None) -> str:
    if failure_mode == "poor_win_loss":
        return "Changing the take-profit multiple targets the payoff ratio directly."
    return "Adjusting the take-profit multiple changes realized reward without changing entry logic."


def _position_rationale(failure_mode: str | None) -> str:
    if failure_mode == "drawdown":
        return "Sizing down is the fastest deterministic way to reduce drawdown pressure."
    return "Position sizing affects risk-adjusted return without altering signal logic."


def _render_strategy_yaml(ir: StrategyIR) -> str:
    payload = ir.model_dump(mode="json")
    return yaml.safe_dump(payload, sort_keys=False)


async def _complete_text(llm_client: AsyncMutationLLM, prompt: str) -> str:
    if hasattr(llm_client, "complete"):
        return await llm_client.complete(prompt)
    if hasattr(llm_client, "ainvoke"):
        response = await llm_client.ainvoke(prompt)
        content = getattr(response, "content", response)
        return content if isinstance(content, str) else str(content)
    raise TypeError("llm_client must expose an async complete(prompt) method")


def _parse_json_payload(raw_response: str) -> dict[str, Any]:
    stripped = raw_response.strip()
    if stripped == "CONVERGED":
        return {"mutations": []}
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    return json.loads(stripped)


def _apply_single_mutation(ir: StrategyIR, mutation: Mutation) -> tuple[StrategyIR, bool]:
    if not is_supported_mutation_path(mutation.yaml_path):
        return ir, False

    payload = ir.model_dump(mode="json")
    try:
        _set_path_value(payload, mutation.yaml_path, mutation.new_value)
        _refresh_derived_fields(payload)
        mutated = StrategyIR.model_validate(payload)
    except (KeyError, IndexError, TypeError, ValueError, ValidationError):
        return ir, False
    return mutated, True


def _refresh_derived_fields(payload: dict[str, Any]) -> None:
    indicators = payload.get("indicators", [])
    if not indicators:
        return
    max_warmup = max(int(indicator["period"]) for indicator in indicators)
    payload["max_warmup"] = max_warmup
    payload["warmup_bars"] = StrategyIR.compute_warmup_bars(max_warmup)


def _set_path_value(payload: dict[str, Any], path: str, new_value: Any) -> None:
    tokens = _parse_path_tokens(path)
    cursor: Any = payload
    for token in tokens[:-1]:
        if isinstance(token, int):
            cursor = cursor[token]
        else:
            cursor = cursor[token]
    last = tokens[-1]
    if isinstance(last, int):
        cursor[last] = new_value
    else:
        cursor[last] = new_value


def _parse_path_tokens(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for chunk in path.split("."):
        remaining = chunk
        while "[" in remaining:
            prefix, rest = remaining.split("[", 1)
            if prefix:
                tokens.append(prefix)
            index_text, remaining = rest.split("]", 1)
            tokens.append(int(index_text))
        if remaining:
            tokens.append(remaining)
    if not tokens:
        raise KeyError(f"invalid mutation path: {path}")
    return tokens


def _freeze_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze_value(inner)) for key, inner in value.items()))
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value

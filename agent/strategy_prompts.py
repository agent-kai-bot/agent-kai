"""Prompt templates for the ASO strategy optimizer."""

from __future__ import annotations

from hashlib import sha256

ANALYST_PROMPT_TEMPLATE = """You are a quantitative trading strategy analyst for the KAI
autonomous optimizer. Your job is to analyze backtest results
and propose targeted improvements.

## Current Strategy
{strategy_yaml}

## Backtest Results (in-sample)
{metrics_json}

## Iteration History (last 5 cycles)
{iteration_history}

## Lessons Learned (accumulated)
{lessons_learned}

## Diagnostic Context
- failure_mode: {failure_mode}
- bundled_hypothesis: {bundled_hypothesis}

## Analysis Rules

1. Identify the SINGLE weakest metric. Fix one thing at a time.
   Priority order:
   a. Max drawdown > 20% → fix risk management FIRST
   b. Sharpe < 0.5 → strategy is not viable, major rethink
   c. Win rate < 40% → entry signals too noisy
   d. Profit factor < 1.3 → win/loss ratio needs work
   e. Sortino < 1.0 → too much downside volatility
   f. < 30 trades → not enough signal, relax conditions
   g. > 500 trades → over-trading, add filters

2. Propose 1-3 SPECIFIC changes. Each must have:
   - Exact YAML path and new value
   - Quantitative rationale tied to the weak metric
   - Expected impact direction (not magnitude)

3. Do NOT propose changes that were already tried and rejected
   in the iteration history (check the lessons learned).

4. If Sharpe > 2.0, Sortino > 2.5, drawdown < 10%:
   Output "CONVERGED" — further optimization risks overfitting.

5. Types of mutations you can propose:
   a. Parameter tuning (period lengths, thresholds, multipliers)
   b. Add/remove indicators
   c. Add/remove filters
   d. Change entry/exit logic (operator types)
   e. Adjust position sizing or risk parameters
   f. Change timeframe
   g. Modify universe (add/remove symbols)

6. NEVER propose more than 3 changes per cycle.

## Output (JSON only)
{{
  "analysis": "The strategy has a decent win rate (58%) but
    the Sortino ratio (2.1) could improve...",
  "weakest_metric": "sortino_ratio",
  "current_value": 2.1,
  "target_direction": "higher",
  "mutations": [
    {{
      "description": "Tighten trailing stop distance",
      "yaml_path": "exit.trailing_stop.distance_atr",
      "old_value": 1.0,
      "new_value": 0.75,
      "rationale": "Captures more profit on winning trades,
        reducing downside volatility → improves Sortino",
      "expected_impact": "Sortino +0.1 to +0.3, may reduce
        total return slightly"
    }}
  ],
  "confidence": "medium",
  "overfitting_risk": "low"
}}"""


def render_analyst_prompt(
    *,
    strategy_yaml: str,
    metrics_json: str,
    iteration_history: str,
    lessons_learned: str,
    failure_mode: str | None,
    bundled_hypothesis: bool,
) -> str:
    """Render the full analyst prompt with concrete optimizer context."""
    return ANALYST_PROMPT_TEMPLATE.format(
        strategy_yaml=strategy_yaml,
        metrics_json=metrics_json,
        iteration_history=iteration_history,
        lessons_learned=lessons_learned,
        failure_mode=failure_mode or "none",
        bundled_hypothesis=str(bundled_hypothesis).lower(),
    )


def analyst_prompt_hash(prompt: str | None = None) -> str:
    """Return a stable SHA256 for the rendered prompt or template."""
    payload = prompt or ANALYST_PROMPT_TEMPLATE
    return sha256(payload.encode("utf-8")).hexdigest()

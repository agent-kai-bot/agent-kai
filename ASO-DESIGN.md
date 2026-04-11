# Autonomous Strategy Optimizer (ASO) — Design v1

## Core concept

A closed-loop system where an LLM continuously iterates on trading
strategies by analyzing backtest metrics, proposing targeted
mutations, validating improvements, and rejecting overfitting.
Think evolutionary optimization with LLM-guided mutations instead
of random ones — the LLM understands WHY a strategy underperforms
and proposes targeted fixes.

```
Strategy Pool → Backtest → Metrics → LLM Analysis → Mutation →
Backtest Child → Compare → OOS Validate → Accept/Reject → Loop
```

## 1. Strategy representation

Strategies are declarative YAML — readable by both LLMs and a
strategy executor. No code generation, no eval.

```yaml
strategy:
  name: momentum_rsi_crossover
  version: 3
  parent: momentum_rsi_crossover_v2
  universe: [BTC-USD, ETH-USD, SOL-USD]
  timeframe: 4h

  indicators:
    - name: rsi
      period: 14
      source: close
    - name: ema
      period: 20
      source: close
      alias: ema_fast
    - name: ema
      period: 50
      source: close
      alias: ema_slow
    - name: atr
      period: 14
      source: close
    - name: volume_sma
      period: 20
      source: volume

  filters:
    - indicator: volume
      operator: above
      value: volume_sma
      note: "only trade when volume confirms"

  entry:
    long:
      conditions:
        - indicator: rsi
          operator: crosses_above
          value: 30
        - indicator: ema_fast
          operator: above
          value: ema_slow
      position_size: 0.02

    short:
      conditions:
        - indicator: rsi
          operator: crosses_below
          value: 70
        - indicator: ema_fast
          operator: below
          value: ema_slow
      position_size: 0.02

  exit:
    stop_loss:
      type: atr_multiple
      multiplier: 2.0
    take_profit:
      type: atr_multiple
      multiplier: 3.0
    trailing_stop:
      enabled: true
      activation_atr: 1.5
      distance_atr: 1.0
    time_exit:
      max_bars: 48

  risk:
    max_position_pct: 0.05
    max_drawdown_pct: 0.15
    max_concurrent_positions: 3
    cooldown_bars: 4
```

Operators: `above`, `below`, `crosses_above`, `crosses_below`,
`between`, `equals`. Values can be numbers or indicator aliases.

## 2. The iteration loop

```
┌──────────────────────────────────────────────────────────┐
│              STRATEGY ITERATION CYCLE                     │
│                                                           │
│  1. SELECT next strategy from candidate pool              │
│       │                                                   │
│  2. BACKTEST on in-sample period (e.g. 12 months)         │
│       │                                                   │
│  3. COMPUTE full metrics suite                            │
│       │                                                   │
│  4. LLM ANALYSIS — structured prompt with metrics +       │
│     strategy YAML + iteration history                     │
│       │                                                   │
│  5. LLM PROPOSES 1-3 targeted mutations                   │
│       │                                                   │
│  6. VALIDATE mutations (schema check, bounds check)       │
│       │                                                   │
│  7. APPLY mutations → create child strategy (version+1)   │
│       │                                                   │
│  8. BACKTEST child on SAME in-sample period                │
│       │                                                   │
│  9. COMPARE parent vs child on key metrics                 │
│       │                                                   │
│  10. If child LOSES → REJECT, log lesson, try next        │
│       │                                                   │
│  11. If child WINS → run OOS validation (holdout period)  │
│       │                                                   │
│  12. If OOS VALIDATES → ACCEPT, add to pool, notify user  │
│       │                                                   │
│  13. If OOS FAILS → REJECT as overfit, log pattern        │
│       │                                                   │
│  14. UPDATE lineage tree, iteration log, session report    │
│       │                                                   │
│  15. LOOP → back to step 1                                │
└──────────────────────────────────────────────────────────┘
```

### Cycle timing

- One full cycle takes ~30-60 seconds (backtest is fast on
  historical OHLCV, LLM call is the bottleneck)
- Default: 1 cycle every 5 minutes (configurable)
- Max iterations per day: 100 (cost control)
- Runs as a daemon scheduler job

## 3. Metrics pipeline

Every backtest produces a standardized metrics object:

```json
{
  "strategy": "momentum_rsi_crossover_v3",
  "period": {"start": "2025-01-01", "end": "2026-01-01"},
  "type": "in_sample",
  "metrics": {
    "return": {
      "total_pct": 42.3,
      "annualized_pct": 42.3,
      "monthly_returns": [3.2, -1.1, 5.4, ...]
    },
    "risk_adjusted": {
      "sharpe_ratio": 1.45,
      "sortino_ratio": 2.10,
      "calmar_ratio": 3.20,
      "omega_ratio": 1.85,
      "information_ratio": 0.92
    },
    "drawdown": {
      "max_drawdown_pct": -13.2,
      "max_drawdown_duration_days": 18,
      "avg_drawdown_pct": -4.1,
      "recovery_factor": 3.2,
      "underwater_curve": [...]
    },
    "trades": {
      "total": 127,
      "winners": 74,
      "losers": 53,
      "win_rate_pct": 58.4,
      "profit_factor": 1.82,
      "avg_win_pct": 3.4,
      "avg_loss_pct": -1.8,
      "largest_win_pct": 12.1,
      "largest_loss_pct": -5.8,
      "avg_duration_hours": 32,
      "avg_bars_held": 8
    },
    "distribution": {
      "by_symbol": {"BTC-USD": {...}, "ETH-USD": {...}},
      "by_hour_utc": {...},
      "by_day_of_week": {...},
      "by_month": {...}
    },
    "benchmark": {
      "btc_buy_hold_return_pct": 35.0,
      "alpha_pct": 7.3,
      "beta": 0.72,
      "correlation": 0.68
    },
    "stability": {
      "monthly_return_stddev": 4.2,
      "positive_months_pct": 75.0,
      "longest_losing_streak_trades": 5,
      "longest_losing_streak_days": 12
    }
  }
}
```

### Metric interpretation thresholds

| Metric | Poor | Acceptable | Good | Excellent |
|--------|------|-----------|------|-----------|
| Sharpe | <0.5 | 0.5-1.0 | 1.0-2.0 | >2.0 |
| Sortino | <0.8 | 0.8-1.5 | 1.5-2.5 | >2.5 |
| Max DD | >25% | 15-25% | 10-15% | <10% |
| Win rate | <40% | 40-50% | 50-60% | >60% |
| Profit factor | <1.2 | 1.2-1.5 | 1.5-2.0 | >2.0 |
| Calmar | <1.0 | 1.0-2.0 | 2.0-3.0 | >3.0 |

## 4. LLM analyst prompt

```
You are a quantitative trading strategy analyst for the KAI
autonomous optimizer. Your job is to analyze backtest results
and propose targeted improvements.

## Current Strategy
{strategy_yaml}

## Backtest Results (in-sample)
{metrics_json}

## Iteration History (last 5 cycles)
{iteration_log — what was tried, whether it helped, why}

## Lessons Learned (accumulated)
{rejected_mutations_and_why}

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
{
  "analysis": "The strategy has a decent win rate (58%) but
    the Sortino ratio (2.1) could improve...",
  "weakest_metric": "sortino_ratio",
  "current_value": 2.1,
  "target_direction": "higher",
  "mutations": [
    {
      "description": "Tighten trailing stop distance",
      "yaml_path": "exit.trailing_stop.distance_atr",
      "old_value": 1.0,
      "new_value": 0.75,
      "rationale": "Captures more profit on winning trades,
        reducing downside volatility → improves Sortino",
      "expected_impact": "Sortino +0.1 to +0.3, may reduce
        total return slightly"
    }
  ],
  "confidence": "medium",
  "overfitting_risk": "low"
}
```

## 5. Overfitting prevention (critical)

LLM-guided optimization is smarter than random search but still
overfits. The defense is layered:

### Layer 1: Data splits
- **In-sample**: 70% of historical data (training)
- **Out-of-sample**: 20% (validation — child must pass here)
- **True holdout**: 10% (never seen by the optimizer, used for
  final human review before paper trading)
- Walk-forward: retrain every month, test on next month

### Layer 2: Statistical guards
- Minimum 30 trades to accept a strategy
- Improvement must be statistically significant (not just noise)
- Sharpe improvement > 0.1 to accept (not 0.01)
- Child must beat parent on AT LEAST 3 of 5 key metrics (Sharpe,
  Sortino, drawdown, win rate, profit factor), not just one

### Layer 3: Complexity penalty
- Each indicator adds +0.05 to the required Sharpe threshold
- Each filter adds +0.03
- This prevents the optimizer from adding 20 indicators that
  perfectly fit historical data but don't generalize

### Layer 4: Regime testing
- Backtest separately on: bull market periods, bear market
  periods, sideways/choppy periods
- Strategy must be profitable (or at least not catastrophic)
  across all three regimes
- A strategy that makes 100% in bull markets but -50% in bear
  markets is not robust

### Layer 5: Monte Carlo robustness
- Randomly perturb entry timing by ±2 bars
- Randomly perturb prices by ±0.1%
- Run 100 simulations
- Median result must still be positive
- 5th percentile result must not be catastrophic (> -20%)

### Layer 6: Iteration history
- Track every accepted and rejected mutation
- Feed the last 5 iterations + all rejection reasons to the LLM
- The LLM learns not to repeat failed mutations
- If the same direction of change has been rejected 3x, the LLM
  should explore a fundamentally different approach

## 6. Strategy pool management

```
workspaces/strategies/
├── pool/
│   ├── active/              # approved for paper/live
│   │   └── momentum_rsi_v7.yaml
│   ├── candidates/          # under optimization
│   │   ├── momentum_rsi_v8.yaml
│   │   └── mean_reversion_v3.yaml
│   └── graveyard/           # rejected (kept for learning)
│       ├── momentum_rsi_v4.yaml
│       └── breakout_v2.yaml
├── lineage.json             # parent→child tree + mutations
├── iteration_log.jsonl      # append-only log of every cycle
├── lessons_learned.json     # accumulated rejection patterns
└── config.yaml              # optimizer settings (cycle time,
                             #   max iterations, thresholds)
```

### Lineage tracking

```json
{
  "momentum_rsi_crossover": {
    "v1": {
      "created": "2026-04-01",
      "source": "human",
      "sharpe": 0.82
    },
    "v2": {
      "created": "2026-04-02",
      "source": "optimizer",
      "parent": "v1",
      "mutations": [
        {"path": "indicators[0].period", "old": 14, "new": 21,
         "rationale": "reduce RSI noise"}
      ],
      "sharpe": 1.12,
      "accepted": true
    },
    "v3": {
      "created": "2026-04-02",
      "source": "optimizer",
      "parent": "v2",
      "mutations": [
        {"path": "exit.stop_loss.multiplier", "old": 2.0, "new": 1.5,
         "rationale": "tighter stops to reduce max drawdown"}
      ],
      "sharpe": 1.05,
      "accepted": false,
      "rejection_reason": "OOS validation failed — sharpe dropped
        from 1.12 to 0.78 out of sample. Likely overfit to
        in-sample volatility regime."
    }
  }
}
```

## 7. Integration with existing architecture

| Component | Role |
|---|---|
| **Daemon scheduler** | Runs the iteration loop as a recurring cron job (every 5 min) |
| **Sub-agent: `strategy-optimizer`** | Dedicated agent with the analyst prompt, separate from the user-facing agent |
| **`run_backtest` tool** | Already exists in the agent's tool list — reused for fast backtesting |
| **OHLCV data** | Existing Coinbase/kai-api OHLCV fetch tools |
| **NATS bus** | Optimizer publishes `strategy.improved` events |
| **Session integration** | Results delivered to user session as strategy reports |
| **Web UI** | "Strategy Lab" panel (future) |
| **Custodial balance** | LLM calls cost tokens — debited per cycle |

### New agent tools

- `list_strategies(pool)` — list active/candidate/graveyard
- `get_strategy(name, version)` — fetch a strategy YAML
- `get_strategy_metrics(name, version)` — fetch last backtest
- `get_strategy_lineage(name)` — full mutation history
- `propose_strategy(yaml)` — submit a human-designed strategy
- `optimizer_status()` — current cycle, queue, budget remaining
- `optimizer_pause()` / `optimizer_resume()` — control the loop

### New slash commands

```
/optimizer status        — current state, cycle count, budget
/optimizer start         — begin iteration loop
/optimizer pause         — stop the loop
/optimizer report        — last N iteration results
/strategies list         — show all strategies with Sharpe
/strategy show NAME      — full YAML + metrics + lineage
/strategy propose        — submit a new human strategy for optimization
/strategy promote NAME   — move from candidate → active (paper trading)
/strategy demote NAME    — move from active → candidate
```

## 8. Human oversight and safety

### Approval gates
- **Candidate → Active**: requires explicit `/strategy promote`
- **Active → Live trading**: requires separate approval (not in v1)
- No autonomous live trading in v1. The optimizer proposes; the
  human decides what to actually trade.

### Notifications
- `strategy.improved` event when a child beats its parent on OOS
- `strategy.converged` event when the LLM says "stop optimizing"
- `strategy.failed` event when a promising candidate fails OOS
- Weekly digest of iteration results to the user's session

### Kill switches
- `/optimizer pause` immediately stops the loop
- Per-day iteration budget (default 100, configurable)
- Per-day LLM token budget (separate from user chat budget)
- Max strategies in candidate pool (default 10)

### Transparency
- Every mutation + rationale is logged
- Every rejection + reason is logged
- The user can ask "why did you change the RSI period?" and
  the agent can look up the lineage + rationale
- No black-box optimization — every decision is explainable

## 9. Phased rollout

### Phase 1: Strategy YAML + backtest pipeline
- Define the strategy YAML schema + validator
- Strategy executor that reads YAML and runs backtests
- Metrics computation module
- `/strategy` slash commands for CRUD

### Phase 2: LLM analyst + mutation engine
- LLM analyst prompt + structured output parsing
- Mutation applicator (apply JSON patches to YAML)
- Iteration log + lineage tracking
- Single manual iteration: user triggers one cycle and reviews

### Phase 3: Autonomous loop
- Daemon scheduler integration (recurring job)
- Budget controls (iterations/day, tokens/day)
- OOS validation + overfitting guards
- Automatic pool management (accept/reject/graveyard)
- Session notifications

### Phase 4: Advanced features
- Walk-forward validation
- Monte Carlo robustness testing
- Regime-aware backtesting (bull/bear/sideways splits)
- Multi-strategy portfolio optimization (correlation-aware)
- Paper trading integration with live P&L tracking
- Strategy Lab panel in the web UI

## 10. Open questions

1. **Backtest engine**: Use the existing `run_backtest` tool (Python,
   in-process) or a separate fast engine (Rust/vectorbt)?
2. **OHLCV depth**: How much history per symbol? 2 years? 5 years?
3. **Strategy complexity cap**: Max number of indicators/filters?
4. **Seed strategies**: Start with a set of classic strategies
   (momentum, mean reversion, breakout) or let the user provide
   the initial seed?
5. **Multi-timeframe**: Should a strategy be allowed to reference
   multiple timeframes (e.g. 4h trend + 15m entry)?
6. **Cross-strategy correlation**: When the pool has multiple active
   strategies, should the optimizer consider portfolio-level
   metrics (total portfolio Sharpe, cross-strategy correlation)?

# Truly Autonomous Trading Agent System — Design v1

## The gap

The current system is **reactive** — the agent waits for user input,
does work when asked, and stops. Even with `/auto`, it only suppresses
permission-asking during a human-initiated task. The optimizer iterates
on strategies but doesn't trade them.

A truly autonomous trading agent is **proactive** — it continuously
monitors markets, independently decides what needs attention, executes
trades based on validated strategies, learns from results, and only
involves the human for high-stakes decisions or periodic reviews.

## Architecture: The Cognitive Loop

The core is an always-running loop that mirrors how a professional
trader thinks:

```
PERCEIVE → DECIDE → ACT → REFLECT → PERCEIVE → ...
```

This runs continuously in the daemon, independent of any connected
client. The human can watch, configure, and override — but the
agent doesn't need them to function.

```
┌─────────────────────────────────────────────────────────────┐
│                   AUTONOMOUS TRADING DAEMON                  │
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │ PERCEIVE │──▶│  DECIDE  │──▶│   ACT    │──▶│ REFLECT  │  │
│  │          │   │          │   │          │   │          │  │
│  │ Market   │   │ Priority │   │ Research │   │ Track    │  │
│  │ data     │   │ queue    │   │ Trade    │   │ outcomes │  │
│  │ Signals  │   │ What     │   │ Adjust   │   │ Learn    │  │
│  │ Portfolio │   │ needs    │   │ Report   │   │ Adapt    │  │
│  │ News     │   │ attention│   │          │   │          │  │
│  │ On-chain │   │ next?    │   │          │   │          │  │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘  │
│       ▲                                            │         │
│       └────────────────────────────────────────────┘         │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐    │
│  │                   SAFETY LAYER                        │    │
│  │  Position limits · Drawdown limits · Daily loss cap  │    │
│  │  Kill switch · Human approval gates · Audit log      │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐    │
│  │                COMMUNICATION LAYER                    │    │
│  │  Session events · Discord alerts · Daily reports     │    │
│  │  Human override channel · Emergency notifications    │    │
│  └──────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

---

## 1. PERCEIVE — Continuous Market Awareness

The perception layer feeds the agent a continuous stream of
structured observations. It runs independently of the cognitive
loop — always collecting, always updating.

### Data streams

| Stream | Source | Frequency | What it provides |
|--------|--------|-----------|-----------------|
| Price feed | Coinbase WS / kai-api | Real-time | Live prices for watchlist tokens |
| OHLCV candles | Coinbase / kai-api | Per bar close | Technical analysis input |
| Signal scanner | NATS signal bus | As published | External scanner signals (momentum, breakout, etc.) |
| Portfolio state | Internal | On every trade | Positions, P&L, exposure, cash |
| On-chain data | Alchemy / Helius | 5-min poll | Whale movements, DEX flows, holder changes (future) |
| News / sentiment | RSS / API | 15-min poll | Market-moving events (future) |

### Perception state object

The perception layer maintains a structured "world state" that
the decision engine reads:

```python
@dataclass
class MarketPerception:
    timestamp: datetime
    prices: dict[str, float]          # symbol → price
    price_changes_24h: dict[str, float]  # symbol → % change
    technicals: dict[str, TechnicalSnapshot]  # per-symbol RSI, MACD, EMAs
    portfolio: PortfolioSnapshot      # positions, cash, P&L
    signals: list[Signal]             # recent unprocessed signals
    active_strategies: list[StrategyStatus]  # running strategies + their state
    risk_state: RiskState             # current drawdown, exposure, daily P&L
    market_regime: str                # "bull" | "bear" | "sideways" | "volatile"
```

### Market regime classifier

A simple rules-based classifier that labels the current regime:
- **Bull**: BTC above 20-day EMA, RSI > 50, positive 7d momentum
- **Bear**: BTC below 20-day EMA, RSI < 50, negative 7d momentum
- **Sideways**: BTC within 3% of 20-day EMA, RSI 40-60
- **Volatile**: ATR(14) > 2x its 20-day average

The regime affects which strategies are active and how
aggressively the agent trades.

---

## 2. DECIDE — What Needs Attention Next

The decision engine is the brain. Every cycle (default: 60s), it
examines the world state and produces a prioritized task queue.

### Priority framework

Tasks are scored on **urgency × importance**:

| Priority | Category | Example | Action window |
|----------|----------|---------|---------------|
| P0: Emergency | Risk breach | Max drawdown hit, position liquidation risk | Immediate |
| P1: Time-sensitive | Signal expiring | Scanner published a strong signal, entry window closing | < 5 min |
| P2: Scheduled | Strategy cycle | Optimizer iteration, daily report, rebalance check | Next cycle |
| P3: Opportunistic | Improvement | New strategy idea, parameter tune opportunity | When idle |
| P4: Background | Housekeeping | Log cleanup, state persistence, health check | Low priority |

### Decision rules (configurable)

```yaml
decision_rules:
  # Risk management (P0 — always active)
  - trigger: portfolio.drawdown_pct > max_drawdown_pct
    action: emergency_close_all
    priority: 0

  - trigger: position.unrealized_loss > position_stop_loss
    action: close_position
    priority: 0

  # Signal reaction (P1)
  - trigger: signal.score > 0.8 AND signal.symbol in watchlist
    action: evaluate_signal_for_trade
    priority: 1
    requires: strategy_validates(signal)

  # Strategy execution (P2)
  - trigger: strategy.entry_conditions_met(symbol, timeframe)
    action: execute_strategy_entry
    priority: 2
    requires: risk_check_passes AND position_limit_ok

  # Periodic tasks (P2)
  - trigger: cron("0 7 * * *")  # daily 7am
    action: daily_portfolio_review
    priority: 2

  - trigger: cron("0 */4 * * *")  # every 4h
    action: scan_opportunities
    priority: 3

  # Optimization (P3)
  - trigger: idle AND optimizer.has_candidates
    action: run_optimizer_cycle
    priority: 3

  # Reflection (P3)
  - trigger: trade_closed
    action: analyze_closed_trade
    priority: 3
```

### The decision loop

```python
class DecisionEngine:
    async def run_cycle(self, perception: MarketPerception) -> list[Task]:
        tasks = []

        # P0: Risk checks (always first)
        tasks.extend(self._check_risk_breaches(perception))

        # P1: Signal evaluation
        for signal in perception.signals:
            if self._should_evaluate_signal(signal, perception):
                tasks.append(Task(P1, "evaluate_signal", signal))

        # P2: Strategy entry/exit checks
        for strategy in perception.active_strategies:
            if self._check_entry_conditions(strategy, perception):
                tasks.append(Task(P2, "execute_entry", strategy))
            if self._check_exit_conditions(strategy, perception):
                tasks.append(Task(P2, "execute_exit", strategy))

        # P2: Scheduled tasks
        tasks.extend(self._check_scheduled_tasks(perception))

        # P3: Opportunistic
        if not tasks:  # only when nothing urgent
            tasks.extend(self._find_opportunities(perception))

        return sorted(tasks, key=lambda t: t.priority)
```

---

## 3. ACT — Execute Decisions

The action layer takes the prioritized task queue and executes
each task using the agent's existing tools.

### Action types

| Action | Tools used | Side effects |
|--------|-----------|--------------|
| `emergency_close_all` | `close_position` for each open | Closes all positions |
| `evaluate_signal_for_trade` | `get_ohlcv`, `run_backtest`, LLM analysis | May produce a trade entry task |
| `execute_strategy_entry` | `place_order` | Opens a position |
| `execute_strategy_exit` | `place_order` (close) | Closes a position |
| `daily_portfolio_review` | `get_portfolio`, `get_ohlcv`, LLM summary | Produces report, may adjust |
| `scan_opportunities` | `get_ohlcv` multi-symbol, `get_signals` | Identifies potential trades |
| `run_optimizer_cycle` | Strategy optimizer tools | Improves strategy parameters |
| `analyze_closed_trade` | Trade history, LLM analysis | Updates lessons learned |

### Trade execution flow

When a strategy signals an entry:

```
1. Strategy conditions met (from DECIDE)
      │
2. Pre-trade risk check:
   - Position limit not exceeded?
   - Daily loss limit not breached?
   - Sufficient capital?
   - Not correlated with existing positions?
      │
3. Size the position:
   - Kelly criterion or fixed-fraction
   - Adjusted for regime (reduce in volatile/bear)
   - Never exceed max_position_pct
      │
4. Determine execution:
   - Entry price (market or limit)
   - Stop loss level
   - Take profit level
   - Time exit (max bars)
      │
5. Execute:
   - If paper mode: record in portfolio tracker
   - If live mode: submit to exchange via API
   - Log everything
      │
6. Post-trade:
   - Update portfolio state
   - Set exit monitoring
   - Notify user (if connected)
```

### Autonomy levels

The user configures how autonomous the agent is:

| Level | Name | What the agent does autonomously | What needs approval |
|-------|------|--------------------------------|-------------------|
| 0 | Observer | Monitor, analyze, report | Everything |
| 1 | Advisor | Monitor, analyze, recommend trades | All trades |
| 2 | Paper trader | Execute paper trades autonomously | Live trades |
| 3 | Conservative | Execute live trades within strict limits | Large trades, new strategies |
| 4 | Full auto | Execute all validated strategy trades | Emergency stops, config changes |

Default: Level 2 (paper). Level 3+ requires explicit opt-in.

---

## 4. REFLECT — Learn From Results

After every action, the agent reflects on the outcome.

### Trade journal

Every trade is logged with:
- Entry/exit prices and times
- Strategy that generated it
- Signal that triggered it
- Market regime at entry
- Risk metrics at entry
- Outcome (P&L, duration, exit reason)
- LLM post-mortem: "what went right/wrong?"

### Strategy performance tracking

Per strategy, per regime:
- Rolling Sharpe (30-day, 90-day)
- Win rate trend (improving or degrading?)
- Drawdown trend
- Signal quality (what % of signals became profitable trades?)

### Adaptation rules

```python
class ReflectionEngine:
    async def reflect_on_trade(self, trade: ClosedTrade):
        # Update strategy stats
        self.update_strategy_metrics(trade)

        # Check if strategy is degrading
        if strategy.rolling_sharpe_30d < 0.5:
            self.flag_strategy_for_review(strategy)

        # Check if regime changed since entry
        if current_regime != trade.entry_regime:
            self.log_regime_mismatch(trade)

        # If strategy has 5 consecutive losses:
        if strategy.consecutive_losses >= 5:
            self.pause_strategy(strategy)
            self.notify_user("Strategy paused: 5 consecutive losses")

        # Feed closed trade to optimizer for learning
        self.optimizer.ingest_trade_outcome(trade)
```

### Weekly strategy review (automated)

Every Sunday at 8am:
1. Rank all active strategies by rolling Sharpe
2. Flag underperformers (Sharpe < 0.5)
3. Check if the optimizer has produced better variants
4. Generate a "Strategy Health Report"
5. Deliver to user session + Discord

---

## 5. SAFETY LAYER — The Non-Negotiable Guardrails

These are hard-coded limits that cannot be overridden by the
agent, the optimizer, or the LLM. Only the human can change them
via explicit configuration.

### Hard limits (defaults)

```yaml
safety:
  # Position limits
  max_position_pct: 0.05          # 5% of portfolio per position
  max_positions: 5                # max concurrent positions
  max_correlated_exposure: 0.15   # max total exposure to correlated assets

  # Loss limits
  max_daily_loss_pct: 0.03        # 3% daily loss → halt all trading
  max_weekly_loss_pct: 0.07       # 7% weekly loss → halt all trading
  max_drawdown_pct: 0.15          # 15% drawdown → close all, halt

  # Trade limits
  max_trades_per_day: 20          # prevent overtrading
  min_time_between_trades: 300    # 5 min cooldown
  max_single_trade_usd: 1000     # absolute cap per trade

  # Strategy limits
  min_backtest_sharpe: 0.8        # strategy must have Sharpe > 0.8
  min_backtest_trades: 50         # strategy must have 50+ backtest trades
  require_oos_validation: true    # must pass out-of-sample

  # Kill switch
  kill_switch_active: false       # when true, close all and halt
  kill_switch_reason: ""
```

### Circuit breakers

| Trigger | Action | Recovery |
|---------|--------|----------|
| Daily loss > 3% | Halt all trading for 24h | Auto-resume next day |
| Weekly loss > 7% | Halt all trading for 7 days | Auto-resume next week |
| Max drawdown > 15% | Close all positions, halt indefinitely | Manual `/resume` required |
| 3 consecutive stop-outs in 1h | Pause the specific strategy | Auto-resume after 4h |
| Exchange API errors > 5 in 10min | Switch to paper mode | Auto-resume after 30min |

### Audit trail

Every autonomous decision is logged to SQLite with:
- Timestamp
- Decision type
- Reasoning (what the agent "thought")
- Action taken
- Outcome
- Risk state at time of decision

The human can always ask "why did you do X?" and get a
complete audit trail.

---

## 6. COMMUNICATION LAYER — Keeping the Human in the Loop

The agent proactively communicates important information.

### Notification tiers

| Tier | Channel | Example |
|------|---------|---------|
| Emergency | Session + Discord + (future: SMS) | "DRAWDOWN BREACH: closing all positions" |
| Alert | Session + Discord | "BTC dropped 5% in 1h, adjusting exposure" |
| Update | Session | "Opened BTC long at $72,500, SL $71,200" |
| Report | Session + Discord | Daily summary, weekly strategy review |
| Info | Session only | "Optimizer improved momentum_v3 Sharpe from 1.2 to 1.4" |

### Daily autonomous report

Generated every day at a configured time:
- Portfolio value + daily P&L
- All trades executed today with P&L
- Active positions with unrealized P&L
- Strategy performance summary
- Market regime assessment
- Next day's watchlist + planned actions

---

## 7. The Cognitive Loop Implementation

### Main loop

```python
class AutonomousTradingAgent:
    def __init__(self, config, perception, decision, action, reflection, safety):
        self.config = config
        self.perception = perception
        self.decision = decision
        self.action = action
        self.reflection = reflection
        self.safety = safety
        self.running = False

    async def run(self):
        self.running = True
        while self.running:
            try:
                # 1. PERCEIVE
                world_state = await self.perception.get_state()

                # 2. SAFETY CHECK (before anything else)
                if self.safety.is_kill_switch_active():
                    await self.action.execute_kill_switch()
                    await asyncio.sleep(60)
                    continue

                if self.safety.is_circuit_breaker_active():
                    await asyncio.sleep(60)
                    continue

                # 3. DECIDE
                tasks = await self.decision.run_cycle(world_state)

                # 4. ACT
                for task in tasks:
                    if not self.running:
                        break

                    # Safety gate per action
                    if not self.safety.approve_action(task, world_state):
                        self.notify("Action blocked by safety: " + task.describe())
                        continue

                    # Autonomy level gate
                    if task.requires_approval(self.config.autonomy_level):
                        self.notify("Awaiting approval: " + task.describe())
                        continue

                    result = await self.action.execute(task, world_state)

                    # 5. REFLECT
                    await self.reflection.process_result(task, result)

                # Sleep until next cycle
                await asyncio.sleep(self.config.cycle_interval_seconds)

            except Exception as e:
                self.log.error("Cognitive loop error: %s", e)
                self.notify_emergency(f"Agent error: {e}")
                await asyncio.sleep(30)
```

### Integration with existing system

| Existing component | Role in autonomous system |
|---|---|
| Daemon scheduler | Triggers periodic tasks (daily report, weekly review) |
| Signal consumer (NATS) | Feeds perception layer with scanner signals |
| Strategy optimizer | Runs during idle cycles to improve strategies |
| Strategy executor | Validates and backtests before any live trade |
| Validation ladder | Every strategy must pass walk-forward + lockbox before trading |
| Tool policy registry | Enforces approval gates at the right autonomy level |
| Session events + Discord | Communication layer for notifications |
| SQLite provenance store | Audit trail for every decision |

---

## 8. What's NEW vs what we reuse

### Reuse (already built)
- Signal consumer + NATS bus
- Strategy IR + compiler + executor + metrics
- Validation ladder (walk-forward + lockbox)
- Strategy optimizer (ASO P1-P4)
- Tool policy registry
- Session + daemon architecture
- Scheduler (for periodic tasks)
- SQLite provenance store
- Web UI + TUI

### New to build
- **Perception layer** (MarketPerception, regime classifier)
- **Decision engine** (priority framework, configurable rules)
- **Autonomous cognitive loop** (the main run() loop)
- **Trade execution manager** (pre-trade checks, sizing, execution)
- **Reflection engine** (trade journal, strategy health tracking)
- **Safety layer** (circuit breakers, hard limits, kill switch)
- **Notification dispatcher** (tiered alerts across channels)
- **Autonomy level configuration** (0-4 levels)
- **Daily/weekly automated reports**

---

## 9. Phased rollout

### Phase 1: Perception + Decision (observer mode)
- Build the perception layer with MarketPerception
- Build the decision engine with configurable rules
- Run the cognitive loop in observer-only mode (Level 0)
- Agent monitors and reports but takes no action
- Validate that it correctly identifies opportunities

### Phase 2: Paper trading (Level 2)
- Build the trade execution manager (paper mode)
- Wire strategies to generate entry/exit signals
- Agent executes paper trades autonomously
- Track paper P&L with full audit trail
- Build daily report generator

### Phase 3: Safety + Reflection
- Build all circuit breakers and hard limits
- Build the reflection engine
- Strategy auto-pausing on consecutive losses
- Weekly strategy health review
- Full audit trail

### Phase 4: Live trading (Level 3-4)
- Wire exchange API for real order submission
- Build the autonomy level gate system
- Human approval flow for Level 3
- Full auto for Level 4 (explicit opt-in)
- Emergency notification to Discord/SMS

---

## 10. Open questions

1. **Exchange integration**: which exchange API for live trades?
   Coinbase Advanced Trade? Binance? Both?
2. **Paper vs live parity**: should paper trading simulate
   slippage/fills realistically, or just use mid prices?
3. **Multi-strategy portfolio**: when multiple strategies signal
   on different assets, how to allocate capital?
4. **Correlation management**: how to measure and limit correlation
   between open positions?
5. **News/sentiment integration**: is this v1 or future? Which
   sources?
6. **Regime classifier**: rules-based (simple) or ML-based
   (accurate but complex)?
7. **Report delivery**: session-only or also Discord/Telegram
   from day one?
8. **State persistence across restarts**: how much of the
   cognitive loop state needs to survive a daemon restart?

---

## 11. Why this design vs alternatives

### vs. Pure rule-based bot
Rule-based bots are rigid. This system uses LLM reasoning for
the hard parts (signal evaluation, trade analysis, strategy
research) while using deterministic rules for the safety-critical
parts (risk management, position sizing, circuit breakers).

### vs. Full LLM autonomy (AutoGPT-style)
Giving the LLM full control is dangerous for trading. This design
constrains the LLM to specific decision points where its reasoning
adds value, while keeping execution on deterministic rails. The
LLM decides "should I trade this signal?" but the code decides
"is this trade within risk limits?"

### vs. Copying existing platforms (3Commas, Pionex, etc.)
Those platforms offer pre-built strategies with parameter tuning.
This system is different because:
- The LLM can reason about WHY a strategy is working or failing
- The optimizer can propose structural changes, not just param tweaks
- The agent can research new strategies from scratch
- The communication is natural language, not config forms

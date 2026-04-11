# Critic Review: Truly Autonomous Trading Agent v1

## Verdict

This is not a safe design for real money.

It reads like a clean control-loop diagram pasted on top of a chat-agent runtime, a paper-only order tool, and an offline strategy optimizer. If you build it as written and point it at live capital, the most likely outcome is not one spectacular blow-up. It is death by a thousand cuts: stale data, duplicate actions, fake backtest robustness, missing state after crashes, paper/live mismatch, and LLM-driven bad judgement that no deterministic risk engine catches in time.

The most important thing to say upfront is this: the design materially overstates how much of this already exists.

## Ground Truth From The Repo

- `place_order` is paper-only and returns a human string, not an order object or lifecycle state. `get_positions` also returns formatted text, not structured risk data. See `agent/crypto_tools.py:326-386`.
- The underlying order backend is a local paper simulator backed by a JSON file, not an OMS or broker adapter. It instant-fills orders, stores stop-loss/take-profit fields without enforcing them, and has no partial fills, idempotency, or reconciliation. See `data_api/paper_trading.py:50-239`.
- Auto mode is not an always-running daemon loop. It is a bounded prompt-chaining mechanism capped at 100 iterations and 180 seconds by default. See `daemon/core.py:34-36`, `daemon/core.py:607-642`, `daemon/core.py:816-918`, and `agent/auto_prompt.py:14-52`.
- Tool policy does not implement autonomy levels. It only knows booleans like `read_only` and `requires_approval_in_auto`. It cannot express “small validated live trades allowed, large trades blocked, new strategies blocked.” See `agent/tool_policy.py:14-23` and `agent/tool_policy.py:36-173`.
- The validation ladder is far thinner than the design claims. The hard gate is basically sample size plus max drawdown. There is no absolute Sharpe floor like `0.8`, no portfolio-level validation, no shadow stage, and no live execution validation. See `agent/strategy_validator.py:96-126` and `agent/strategy_walkforward.py:15-16`, `agent/strategy_walkforward.py:114-124`.
- The optimizer can mutate risk and cost fields. That directly contradicts the design’s claim that hard risk limits cannot be overridden by the optimizer or LLM. See `agent/strategy_mutator.py:19-34`, `agent/strategy_mutator.py:140-147`, and the allowed `costs.*` paths in `agent/strategy_mutator.py:33`.
- The existing SQLite provenance store is for strategy versions, mutations, runs, and approvals, not autonomous trading decisions, order lifecycle, or risk overrides. See `agent/strategy_store.py:1` and `agent/strategy_store.py:61-84`.
- Signal and event plumbing are in-memory and non-durable. Signals are just a bounded ring buffer with no ack, cursor, replay, or “unprocessed” state. See `agent/signal_consumer.py:66-74` and `agent/signal_consumer.py:164-188`. The daemon event bus is a simple callback list. See `daemon/scheduler.py:262-280`.

## 1. Safety Analysis

- The design’s “non-negotiable guardrails” are mostly prose. In the current code, there is no implementation of:
  `max_positions`, `max_correlated_exposure`, `max_daily_loss_pct`, `max_weekly_loss_pct`, `max_trades_per_day`, `min_time_between_trades`, or `max_single_trade_usd`.
- The strategy IR has `risk.max_position_pct` and `risk.max_drawdown_pct`, but `risk.max_drawdown_pct` does not appear to drive execution or validation behavior. The validator uses a hardcoded walk-forward drawdown threshold of 20%, not the strategy field. See `agent/strategy_ir.py:213-220`, `agent/strategy_walkforward.py:15`, and `agent/strategy_validator.py:118-126`.
- Worse, the optimizer and LLM are allowed to mutate `risk.max_position_pct` and `risk.max_drawdown_pct`. So your “hard guardrails” are currently strategy parameters, not guardrails. See `agent/strategy_mutator.py:32` and `agent/strategy_mutator.py:140-147`.
- The optimizer can also mutate `costs.fee_pct`, `costs.slippage_pct`, and `costs.spread_pct`. That is catastrophic. It means the LLM can improve validation results by lying about trading costs. That is not optimization. That is backtest fraud with extra steps. See `agent/strategy_mutator.py:33`.
- `accept_child()` accepts relative improvement, not absolute tradability. A child can be promoted for merely beating a bad parent, and lockbox only requires positive Sharpe. The design says `min_backtest_sharpe: 0.8`; the code does not enforce that. See `agent/strategy_optimizer.py:69-90`.
- The backtest executor is not a live risk engine. It sizes one position as `cash * max_position_pct`, opens it, and tracks one active position at a time. There is no multi-position portfolio safety, no correlation check, no account-level drawdown breaker, and no daily loss accounting. See `agent/strategy_executor.py:82-127` and `agent/strategy_executor.py:228-267`.
- The autonomy table is internally unsafe. “Emergency stops need approval” under full-auto is exactly backwards. Emergency flattening should never wait for human approval. That is how you turn a contained failure into a large loss.
- The circuit breakers are missing the failures that actually kill automated traders:
  stale or missing data, disconnected broker, order ack timeout, fill/position mismatch, quote/execution price deviation, exchange maintenance mode, spread blowout, repeated rejects, duplicate-order detection, unexpected balance movement, and clock drift.
- Auto-resume after a daily or weekly loss breach is a bad idea. A system that just lost enough to hit a hard limit should require human review before it trades again.
- Paper mode currently gives false comfort. The paper engine stores stop-loss and take-profit values on positions, but there is no monitoring loop that automatically fires them. See `data_api/paper_trading.py:121-142` and `data_api/paper_trading.py:181-210`.

## 2. Architecture Gaps

- The design claims an always-running autonomous daemon independent of any client. The current daemon explicitly describes itself as “Phase 1” in-process runtime scaffolding. See `daemon/core.py:1-5`.
- The current `/auto` mode is not a daemon control loop. It is a multi-turn conversation shim that repeatedly prompts the LLM with “Continue with the next step” and depends on the model emitting a correctly formatted `[AUTO_STATE: ...]` footer. See `daemon/core.py:816-918` and `agent/auto_prompt.py:14-52`.
- Auto-mode state is not durably persisted. Session save/load stores chat history, UI state, and queued inputs, but not open autonomous control state, risk state, order intents, or perception snapshots. After a crash, the daemon does not know what trade workflow was in progress. See `daemon/core.py:709-768`.
- The scheduler is a prompt scheduler, not a trading control plane. It schedules prompts into a session. That is not the same thing as a deterministic task engine.
- Event-driven scheduler jobs do not actually deliver event payload into the session execution path. `fire_event_job()` passes payload to `_dispatch()`, but the payload is only logged and the dispatch callback signature ignores it. That means the proposed event-driven decisioning model is not supported by the current scheduler shape. See `daemon/scheduler.py:549-565` and `daemon/scheduler.py:635-640`.
- `SignalConsumer` has no concept of “recent unprocessed signals,” dedupe, ack, cursor, or durable offsets. It only has “whatever is still in the last N ring-buffer entries.” The design assumes a much richer state model than the code provides. See `agent/signal_consumer.py:69-74` and `agent/signal_consumer.py:164-188`.
- `place_order` and `get_positions` as tools return strings. If the new safety layer is supposed to use “existing tools,” it will either be parsing human text or bypassing the tool layer entirely. Either way, the reuse story is false. See `agent/crypto_tools.py:326-386`.
- There is no portfolio engine capable of expressing the design’s portfolio-level constraints across multiple active strategies. The executor is a single-strategy, single-position backtester. That does not validate or simulate multi-strategy autonomous portfolio behavior.
- The design says the tool policy registry will enforce autonomy levels. It cannot. The current policy model has no room for trade size, strategy novelty, symbol allowlists, venue state, or per-level permissions. See `agent/tool_policy.py:14-23`.
- The design says the audit trail will be SQLite and answer “why did you do X?” The only SQLite reuse I found is ASO provenance for strategies and evaluations. That is not a decision log, not an order log, and not an explainability substrate for live trading.

## 3. The LLM Problem

The LLM is being asked to do too much in the money path.

Where it adds value:

- Offline strategy ideation.
- Mutation proposal generation for research, if tightly sandboxed.
- Human-readable summaries and daily reports.
- Post-mortem narrative generation for humans.
- Triage of anomalies for investigation.

Where it adds risk and should be removed:

- Deciding whether a live signal is good enough to trade.
- Deciding position size.
- Deciding live order parameters.
- Deciding whether a risk exception is acceptable.
- Deciding recovery behavior after partial failures.
- Managing schedules or task queues that can indirectly place money at risk.
- Writing “lessons learned” that directly feed an active optimizer.

Specific design problems:

- `evaluate_signal_for_trade` using `get_ohlcv`, `run_backtest`, and LLM analysis is a terrible live pattern. It invites on-the-fly data mining at the moment capital is at risk.
- `analyze_closed_trade` feeding back into learning closes the loop around narrative hindsight. The LLM will tell a coherent story regardless of whether the story is true.
- The current auto loop is parser-driven. If the model fails to emit the footer, the system stops. If it emits `continue` when it should stop, it continues. That is fine for a chat agent. It is not fine for a trading control loop.
- The existing optimizer already gives the LLM access to strategy structure, risk fields, and cost fields. That is acceptable only for offline research under human review. It is not acceptable as part of an autonomous live-trading pipeline.

My recommendation is simple:

- Keep the LLM completely out of live trading decisions.
- Let it propose hypotheses offline.
- Require deterministic validation, deterministic promotion rules, deterministic risk limits, and deterministic execution.

## 4. Market Reality

The design assumes a cleaner market than the one you will actually trade in.

- The executor is bar-based. It opens at bar close plus static slippage/spread and exits at exact stop or take-profit thresholds when `Low` or `High` cross them. See `agent/strategy_executor.py:237-243` and `agent/strategy_executor.py:291-330`.
- It cannot model:
  intrabar path ambiguity, partial fills, queue priority, depth, spread widening, gapping through stops, maker/taker differences, cancel/replace latency, or self-trade prevention.
- If both stop-loss and take-profit are touched in the same bar, the code checks stop first and take-profit second. That arbitrary ordering can materially change PnL. See `agent/strategy_executor.py:291-309`.
- There is no liquidity model. For small-cap crypto, even a “modest” notional can move price.
- There is no borrow/funding/margin/liquidation model.
- There is no stale-data detection, missing-bar detection, or cross-source sanity check.
- There is no rate-limit budget or exchange-specific degraded mode beyond a hand-wavy breaker concept.
- The regime classifier is BTC-centric. That is a weak assumption for altcoins that move on listings, unlocks, exploits, governance drama, or concentrated whale flows.
- Kelly sizing should not be in v1. In crypto, edge estimates are unstable and Kelly aggressively magnifies estimation error.
- The paper engine is not even a good live proxy:
  it instant-fills, uses a single price, ignores fees/slippage, does not enforce stops/TPs, and handles shorts unrealistically. See `data_api/paper_trading.py:68-179`.

## 5. Operational Risks

- Crash after order submit but before state write:
  there is no durable intent log plus broker reconciliation step, so restart can double-submit or believe the account is flat when it is not.
- Network timeout after submit:
  without client order ids, idempotency keys, and reconciliation, retries will eventually duplicate orders.
- Exchange API drift:
  there is no broker adapter layer with contract tests, schema validation, or versioned integration handling.
- Downtime:
  signals are lost because the consumer is in-memory only. There is no replay.
- Scheduler recovery is inadequate for trading:
  missed jobs older than the 5-minute catch-up window are effectively dropped or marked completed. See `daemon/scheduler.py:691-725`.
- Busy-session skip behavior can silently drop scheduled work without turning it into a recorded failure. See `daemon/server.py:533-539`.
- Portfolio state is unsafe:
  the paper portfolio is a process-local object with JSON save/load and no locking. Multiple sessions/processes can stomp the same `portfolio.json`. See `data_api/paper_trading.py:14-16` and `data_api/paper_trading.py:212-237`.
- There is no independent kill switch.
  If the same process that trades is the only process that can stop trading, you do not have a real kill switch.
- There is no key-management or custody story:
  where trade keys live, how they are rotated, whether withdrawals are disabled, whether read and trade permissions are split, whether IP allowlists are enforced, how secrets are audited.
- There is no startup reconciliation process:
  the system should query exchange balances, open orders, positions, fills since last checkpoint, and risk status before resuming. Nothing in the design or code suggests that exists.

## 6. What Is Over-Engineered For v1

Cut these if the goal is to ship something useful without blowing yourself up:

- The “cognitive loop” abstraction. It is presentation polish, not the hard part.
- On-chain data, news sentiment, Discord, weekly reviews, and tiered notification trees.
- Five autonomy levels. Start with two:
  paper and live-with-explicit-enable.
- Kelly sizing.
- LLM post-mortems and self-learning reflection loops.
- Autonomous optimizer cycles in the live control loop.
- Regime-aware aggressiveness as a live lever.

If you want a sane v1, ship this instead:

- One venue.
- One deterministic strategy family.
- Paper trading and shadow mode first.
- Deterministic risk engine.
- Human-reviewed promotion to live.
- Simple daily reports.

## 7. What Is Under-Engineered

These are the parts that actually need more design before any real money is touched:

- A real OMS/EMS:
  order ids, client order ids, status transitions, partial fills, cancels, replace, rejects, expirations, reconciliation.
- A deterministic portfolio/risk ledger:
  positions, cash, realized/unrealized PnL, fees, exposure, correlation buckets, drawdown accounting.
- A durable event/state backbone:
  not in-memory queues and ring buffers.
- Data quality controls:
  staleness, gap detection, cross-feed comparisons, timestamp sanity checks, clock-sync monitoring.
- Execution safeguards:
  price collars, max slippage, max spread, max order notional, duplicate-order prevention, venue health state.
- Independent risk process and kill switch:
  not inside the LLM or strategy runtime.
- Shadow trading:
  the codebase even has a shadow sample-size constant, but no actual shadow execution stage in the runtime. See `agent/strategy_sample_size.py:7` and `agent/strategy_sample_size.py:13-16`.
- Governance:
  approved strategies, promotion workflow, rollback path, change control, human ownership of live strategy set.
- Security:
  key storage, withdrawal-disabled keys, IP allowlists, environment separation, incident response.

## 8. Comparison To Production Systems

Real algorithmic trading systems usually do all of the following:

- Separate alpha generation, risk, and execution into distinct components.
- Use a durable OMS/EMS with idempotent order routing.
- Reconcile continuously against broker/exchange truth.
- Run hard risk limits outside the strategy process.
- Keep durable event logs with replay, not ephemeral pub/sub only.
- Track transaction-cost analysis and venue-specific execution quality.
- Shadow trade and canary deploy before increasing size.
- Maintain operational telemetry, alerts, dashboards, and incident runbooks.
- Treat model changes and live-strategy promotions as controlled releases, not emergent behavior.

This design misses or under-specifies most of that.

The closest current code analogue is not a production trading platform. It is a research environment with:

- a strategy compiler/executor,
- an offline validation ladder,
- an optimizer,
- a paper portfolio helper,
- and a chat-agent runtime.

That is useful. It is not remotely the same thing as a live autonomous trading system.

## 9. The Honest Question

Would I trust this with $10,000 of my own money?

No.

I would not trust it with $1,000 live in its current conceptual shape, and I would not treat the existing codebase as “most of the way there.” I would use the current system for offline research, backtesting, paper trading experiments, and maybe shadow-mode signal evaluation after cleanup. Nothing more.

What would need to change before I would even consider tiny live capital:

1. Remove the LLM from all live trading decisions and execution paths.
2. Build a real broker adapter plus OMS with client order ids, idempotency, and reconciliation.
3. Build deterministic portfolio/risk accounting outside the chat agent.
4. Make orders, positions, fills, signals, and breaker state durable.
5. Add hard circuit breakers for stale data, broker disconnect, duplicate orders, spread blowout, and reconciliation failure.
6. Add shadow mode and run it for months.
7. Prove paper/live parity on one venue and one strategy family.
8. Start with tiny-notional canary live, not “full auto.”

The honest framing for this project is not “truly autonomous trading agent v1.”

The honest framing is:

“research assistant + deterministic paper trader + offline optimizer, with a lot of boring trading infrastructure still missing.”

That boring infrastructure is exactly the part that protects money.

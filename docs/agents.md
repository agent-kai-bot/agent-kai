# Sub-agent system

The most distinctive feature in Agent KAI: a network of long-lived specialist sub-agents the main agent can spawn, delegate to, and synthesize from. Each sub-agent is a full LangChain executor with its own prompt, workspace, persistent memory, skill library, tool set, NATS subject, and LLM endpoint with fallback chain. This is not a "chatbot router" or a "function-calling wrapper" — every sub-agent is a complete agent in its own right.

This doc covers the runtime, the 14 built-in agents, the messaging contract, the workspace layout, the SOUL.md prompts, and patterns for multi-agent orchestration with sample prompts you can copy-paste.

## TL;DR

```
Main agent (kai)
  ├── spawn_agent(name)               ← create a sub-agent
  ├── nats_request(name, task)        ← send it work, get a reply
  ├── nats_publish(subject, msg)      ← fire-and-forget event
  └── list_agents()                   ← see what's running

Sub-agents listen on NATS subject  agent.{name}.request
Sub-agent replies arrive on        agent.{name}.response
Sub-agent status updates fire on   agent.{name}.status
Registry events on                 system.registry
```

Each sub-agent has its own `workspaces/{name}/` directory:

```
workspaces/analyst/
├── SOUL.md             # role prompt (the agent's "personality" and responsibilities)
├── memories/
│   └── MEMORY.md       # per-agent persistent memory
└── skills/
    ├── how-to-write-a-ta-skill.md       # meta-skill template
    ├── rsi-divergence-hunt.md           # mentor-drafted skill
    ├── macd-zero-cross.md
    ├── volume-profile-poc-reject.md
    └── …                                # one .md file per skill
```

## Quick agent map

This is the compact reference for the currently configured runtime agents.

| Agent | Role | Workspace | Prompt docs | High-value learning targets |
|---|---|---|---|---|
| `kai` | Primary trading assistant and orchestrator | `workspaces/kai/` | [docs/kai/prompts](/home/atc/git/claude-local-ai-agent/docs/kai/prompts/README.md) | orchestration workflows, alpha-finding procedures, delegation patterns |
| `analyst` | Technical analysis specialist | `workspaces/analyst/` | [docs/analyst/prompts](/home/atc/git/claude-local-ai-agent/docs/analyst/prompts/README.md) | multi-timeframe confluence, breakout validation, pullback quality, regime detection |
| `trader` | Execution specialist | `workspaces/trader/` | [docs/trader/prompts](/home/atc/git/claude-local-ai-agent/docs/trader/prompts/README.md) | entry timing, staging, partial exits, execution playbooks |
| `risk-manager` | Capital and exposure guardian | `workspaces/risk-manager/` | [docs/risk-manager/prompts](/home/atc/git/claude-local-ai-agent/docs/risk-manager/prompts/README.md) | position sizing, leverage safety, drawdown enforcement, rebalance logic |
| `scanner` | Market radar and discovery worker | `workspaces/scanner/` | [docs/scanner/prompts](/home/atc/git/claude-local-ai-agent/docs/scanner/prompts/README.md) | scanner triage, ranking workflows, reject or promote rules |
| `onchain` | Blockchain and wallet investigator | `workspaces/onchain/` | [docs/onchain/prompts](/home/atc/git/claude-local-ai-agent/docs/onchain/prompts/README.md) | onchain confirmation flows, holder analysis, liquidity checks |
| `mentor` | Reflection and learning coach | `workspaces/mentor/` | [docs/mentor/prompts](/home/atc/git/claude-local-ai-agent/docs/mentor/prompts/README.md) | identify create vs no_skill patterns, reflection quality, skill drafting |
| `ceo` | Strategy and prioritization | `workspaces/ceo/` | [docs/ceo/prompts](/home/atc/git/claude-local-ai-agent/docs/ceo/prompts/README.md) | decision frameworks, prioritization, escalation rules |
| `cto` | Technical leadership | `workspaces/cto/` | [docs/cto/prompts](/home/atc/git/claude-local-ai-agent/docs/cto/prompts/README.md) | architecture reviews, standards, technical tradeoff playbooks |
| `architect` | System design specialist | `workspaces/architect/` | [docs/architect/prompts](/home/atc/git/claude-local-ai-agent/docs/architect/prompts/README.md) | system topology, API contracts, failure analysis |
| `developer` | Implementation worker | `workspaces/developer/` | [docs/developer/prompts](/home/atc/git/claude-local-ai-agent/docs/developer/prompts/README.md) | debugging workflows, implementation patterns, verification routines |
| `qa` | Quality and verification | `workspaces/qa/` | [docs/qa/prompts](/home/atc/git/claude-local-ai-agent/docs/qa/prompts/README.md) | regression procedures, failure classification, test planning |
| `ux-manager` | UX and accessibility reviewer | `workspaces/ux-manager/` | [docs/ux-manager/prompts](/home/atc/git/claude-local-ai-agent/docs/ux-manager/prompts/README.md) | UX review checklists, friction analysis, accessibility workflows |
| `project-manager` | Coordination and sequencing | `workspaces/project-manager/` | [docs/project-manager/prompts](/home/atc/git/claude-local-ai-agent/docs/project-manager/prompts/README.md) | planning, dependency mapping, handoff workflows |
| `seo` | Search and content optimizer | `workspaces/seo/` | [docs/seo/prompts](/home/atc/git/claude-local-ai-agent/docs/seo/prompts/README.md) | content optimization routines, audit checklists |
| `sales-marketing` | GTM and messaging | `workspaces/sales-marketing/` | [docs/sales-marketing/prompts](/home/atc/git/claude-local-ai-agent/docs/sales-marketing/prompts/README.md) | audience targeting, messaging frameworks, campaign playbooks |

## Extra workspace directories

These exist on disk but are not part of the current configured runtime agent list:

- `workspaces/rsi_watcher/`
- `workspaces/terminal/`

## Learning notes

- The current self-learning loop is strongest for `analyst`, `trader`, `risk-manager`, `scanner`, and `mentor`.
- The most learnable outputs are reusable procedures, checklists, scoring rubrics, formulas, and validation routines.
- The weakest learning targets are one-off commentary and trivial factual answers.
- See [docs/prompts.md](/home/atc/git/claude-local-ai-agent/docs/prompts.md) for the prompt library index.

## How a sub-agent runs

### Lifecycle

1. **Spawn.** The main agent calls `spawn_agent(name)`. `SubAgentManager.spawn` constructs a new `SubAgent(name, bus)` instance. Construction loads the agent's config from `agent-config.json`, builds the LLM (primary + fallback chain), loads memory + skills into the system prompt, and creates the LangChain executor. Then `agent.start()` subscribes to `agent.{name}.request` on NATS and publishes a `system.registry` event announcing the agent is online.

2. **Receive task.** Some other agent (or the user via `/analyze`, `/buy`, etc.) calls `nats_request("analyst", "Run technical analysis on BTC 1h")`. The sub-agent's `_handle_request` fires.

3. **Run with recording.** The handler creates a fresh `ToolCallRecorder` (a LangChain `BaseCallbackHandler`) and a fresh `SessionRecord`. It calls `_invoke_with_fallback(task)` which runs the primary executor, then walks the fallback chain on any error. The recorder captures every tool call (name, input, output, error flag) for the entire run.

4. **Capture session.** When the run completes, the handler stashes `session.response`, `session.tool_calls`, and `session.finished_at` into `self.last_session`. This is what `/learn` reads to build the reflection bundle.

5. **Reply.** The handler publishes the response on `agent.{name}.response` and returns it as the reply to the original `nats_request`.

6. **Stay alive.** The sub-agent remains running, ready for the next task. Multiple tasks reuse the same instance — no per-task spawn cost.

7. **Stop.** `SubAgentManager.stop(name)` unsubscribes the NATS handler and publishes a `system.registry` offline event.

### Code path

| Step | File | Function |
|---|---|---|
| Spawn | `agent/sub_agents.py` | `SubAgentManager.spawn` |
| Construct | `agent/sub_agents.py:34` | `SubAgent.__init__` |
| Subscribe to NATS | `agent/sub_agents.py:144` | `SubAgent.start` |
| Receive task | `agent/sub_agents.py:166` | `SubAgent._handle_request` |
| Run executor + record | `agent/sub_agents.py:203` | `SubAgent._invoke` |
| Walk fallback chain | `agent/sub_agents.py:228` | `SubAgent._invoke_with_fallback` |
| Read last session | `agent/sub_agents.py:290` | `SubAgent.get_last_session` |
| Stop | `agent/sub_agents.py:158` | `SubAgent.stop` |

### Per-agent isolation

Each sub-agent gets:

- **Its own workspace directory** (`workspaces/{name}/`) — files written via `file_write` land here. The Docker sandbox tool bind-mounts this as `/work` so the agent can write a script and run it sandboxed without host paths leaking into the LLM's view.
- **Its own MemoryStore** — backed by `workspaces/{name}/memories/MEMORY.md`. Per-agent notes the agent curates over time. Plus the shared `workspaces/user.md` (USER profile) which every agent can read and write to.
- **Its own SkillStore** — backed by `workspaces/{name}/skills/*.md`. One markdown file per skill. The agent loads the catalog at startup but only loads individual skill bodies via `skill_view` when the catalog suggests one is relevant (progressive disclosure).
- **Its own chat_history** — sessions are independent.
- **Its own LangChain executor** — built from its own LLM endpoint, its own tools, its own prompt.
- **Its own NATS subscription** — `agent.{name}.request`.

The main agent has the same shape (workspace, memory, skills, executor) but lives in `agent/core.py:AgentRunner` and is constructed by `main.py` rather than by `SubAgentManager`.

---

## The 14 built-in agents

All defined in `agent-config.json`. Listed here in groups:

### Trading specialists

#### `analyst`

**Role:** Technical analysis expert. Reads charts, computes indicators, identifies patterns, generates trading signals.

**Default endpoint:** `kai-smart` (cloud, 200k context). Fallback chain: `kai-local/qwen35-gptq` → `codex-cli/gpt-5.4`.

**Tools used most:** `query_ohlcv`, `calculate_indicator` (RSI/MACD/EMA/SMA/BBANDS/ATR/VWAP), `get_latest_price`, `get_signals`, `run_backtest`, `nats_publish` (to publish signals on `signals.{strategy}.{symbol}`).

**Skill library** (sample): `rsi-divergence-hunt`, `macd-zero-cross`, `volume-profile-poc-reject`, `moving-average-ribbon-stack`, `inside-bar-breakout`, `bb-squeeze-breakout`, `support-retest-bounce`, `how-to-write-a-ta-skill`.

**Sample prompt:**

```
/analyze SOL 1h
```

or directly:

```
nats_request("analyst", "Run a multi-timeframe analysis on ETH. Use 1d for macro, 4h for swing, 1h for tactical. Include RSI/MACD/BBANDS at each timeframe and identify the cleanest setup.")
```

#### `trader`

**Role:** Order execution specialist. Places trades, manages positions, handles order lifecycle.

**Default endpoint:** `kai-smart` with the same fallback chain.

**Tools used most:** `place_order`, `get_positions`, `get_latest_price`, `query_ohlcv` (for entry timing), `nats_request` (to ask the analyst or risk-manager for input).

**Skill library:** `break-even-management`, `news-window-hedge`, `limit-order-retry`, `scaled-entry-ladder`, `how-to-write-an-execution-skill`.

**Sample prompt:**

```
/buy BTC 0.1
/sell ETH 2 limit 3500
```

#### `risk-manager`

**Role:** Capital guardian. Position sizing, stop-loss math, exposure limits, drawdown monitoring.

**Default endpoint:** `kai-smart` + fallbacks.

**Tools used most:** `get_positions`, `calculate_indicator` (ATR for volatility-based sizing), `get_latest_price`.

**Sample prompt:**

```
/risk
```

or:

```
"Size a long ETH position assuming I have $10k account, want max 2% risk per trade, and the current ATR(14) on the 1h is roughly $40."
```

#### `scanner`

**Role:** Market radar. Pump.fun monitoring, new-token alerts, breakout detection.

**Default endpoint:** `kai-fast` (cheaper, faster — high message volume).

**Tools used most:** `scan_tokens`, `get_signals`, `get_latest_price`.

**Sample prompt:**

```
/scan trending
/scan new
```

#### `onchain`

**Role:** Blockchain investigator. Wallet tracking, contract analysis, liquidity checks.

**Default endpoint:** `kai-smart`.

**Sample prompt:**

```
"Look up the top 10 holders for the contract 0x123..., check the last 50 transactions, and tell me if there's any whale accumulation or distribution."
```

### Org-chart agents

These agents exist for projects that span beyond pure trading — a full AI-driven product team where each role can be invoked for design, building, testing, marketing, and ops decisions. All default to `kai-smart`.

#### `ceo`
Strategic leader — sets vision, makes priority decisions, aligns all agents.

#### `cto`
Technical leader — architecture decisions, engineering standards, tech strategy.

#### `architect`
System designer — blueprints, API contracts, data models, design docs.

#### `developer`
Builder — writes, edits, debugs, and ships code. `max_iterations: 200` (lower than the others because builds should be focused, not exploratory).

#### `qa`
Quality gatekeeper — tests, bug reports, code review, coverage.

#### `ux-manager`
UX champion — user flows, usability, accessibility, design system.

#### `project-manager`
Coordinator — task tracking, priorities, schedules, cross-agent communication.

#### `seo`
Search optimizer — keywords, meta tags, technical SEO, content strategy.

#### `sales-marketing`
Growth driver — messaging, copy, go-to-market, campaigns, lead gen.

### The reflection agent

#### `mentor`

**Role:** Reads other agents' sessions and authors reusable skills for them. The lynchpin of the self-learning loop.

**Default endpoint:** `kai-smart`. Fallback chain: `codex-cli/gpt-5.4` → `kai-local/qwen35-gptq`.

**How it's invoked:** the user runs `/learn [agent]` in the TUI, which builds a reflection bundle (task, response, tool calls, chat turns, existing skills) and sends it to the mentor as a NATS request. The mentor reads its own `how-to-reflect-on-a-session` meta-skill and returns a structured reply with `DECISION:`, `TARGET_AGENT:`, `SKILL_NAME:`, `OP:`, and either `SKILL_CONTENT:` (for create) or `OLD_STRING:` / `NEW_STRING:` (for patch).

**Skill library:** `how-to-reflect-on-a-session` (the meta-skill that defines its own job).

See [learning-and-skills.md](learning-and-skills.md) for the full reflection loop with a working example.

---

## SOUL.md — the role prompt

Each sub-agent has a `workspaces/{name}/SOUL.md` file that defines its identity, responsibilities, tools-it-uses-most, decision frameworks, and learning rules. This file is loaded at construction time and prepended to the shared base prompt.

### Example: the analyst's SOUL

```markdown
# Analyst Agent

## Identity
You are the Analyst agent — the technical analysis expert of the KAI crypto trading system. You read charts, compute indicators, identify patterns, and generate trading signals.

## Responsibilities
- Run technical analysis on any symbol when requested
- Compute and interpret indicators: RSI, MACD, EMA, SMA, Bollinger Bands, ATR, VWAP
- Identify chart patterns: support/resistance, trend lines, breakouts, divergences
- Generate clear trading signals with confidence levels
- Provide multi-timeframe analysis (1m, 5m, 15m, 1h)

## Tools You Use Most
- `query_ohlcv` — Fetch historical candle data
- `calculate_indicator` — Compute TA indicators
- `get_latest_price` — Get current price context
- `nats_publish` — Publish signals to market.{symbol}.signal

## Analysis Framework
1. **Check your skill library FIRST.** At the start of any non-trivial analysis,
   call `skills_list` to see if you already have a playbook for this kind of setup.
   If a skill name looks relevant, call `skill_view` to load its body. This is the
   whole point of procedural memory — you've been here before.
2. Classify the regime before picking a direction. Use `moving-average-ribbon-stack`
   (if the skill exists) or the equivalent 5/10/20/50 EMA check.
3. Start with the higher timeframe (1h) for trend direction.
4. Drop to lower timeframes (15m, 5m) for entry timing.
5. Check multiple indicators — don't rely on just one.
6. Always note key support and resistance levels.
7. State your confidence level: high / medium / low.

## Learning from hard sessions
If you just solved a non-trivial problem — 3+ tool calls, a wrong initial read
you corrected, a subtle pitfall you avoided — **save it as a skill**. Use
`skill_manage` with action `create`. Follow the `how-to-write-a-ta-skill`
meta-skill in your library for the template.

## Working With Other Agents
- **Trader**: Provide signals and analysis for trade decisions
- **Scanner**: Validate scanner alerts with deeper technical analysis
- **Risk Manager**: Provide volatility data (ATR) for position sizing
```

### Editing a SOUL

Just edit the file. Changes take effect the next time the agent is constructed (i.e. after `SubAgentManager.stop(name)` + `spawn(name)`, or on TUI restart). The frozen system-prompt snapshot inside the running agent is not refreshed mid-session — see [learning-and-skills.md#frozen-snapshot](learning-and-skills.md#why-the-system-prompt-doesnt-update-mid-session) for why.

---

## Multi-agent orchestration patterns

The interesting part of the runtime isn't any one agent — it's combining them. Here are working patterns you can use directly.

### Pattern 1 — Specialist delegation

Just ask for the right specialist by role. The main kai agent will spawn it if needed.

```
/analyze BTC 1h         # delegates to analyst
/buy SOL 50             # delegates to trader
/risk                   # delegates to risk-manager
/scan trending          # delegates to scanner
```

### Pattern 2 — Multi-specialist synthesis

Ask kai to combine multiple specialists' outputs into one answer:

```
I'm thinking about a long position on ETH. Get the analyst to do a 1h + 4h technical breakdown, get the risk-manager to size the position assuming I have a $10k account and want max 2% risk per trade, then synthesize their two reports into one recommendation.
```

What kai will do internally:
1. `spawn_agent("analyst", task="ETH 1h+4h technical")`
2. `nats_request("analyst", task)` → blocks until analyst replies
3. `spawn_agent("risk-manager", task="size ETH long, $10k account, 2% risk")`
4. `nats_request("risk-manager", task)` → blocks until risk-manager replies
5. Synthesize both replies into a final recommendation, posted as kai's own response

### Pattern 3 — Validate before recommending

Have the analyst check the live signal feed AND backtest a candidate strategy before saying anything is tradeable:

```
Run a multi-timeframe BTC analysis. Pull live signals from the get_signals tool first, then do TA on 1h + 6h, then backtest any candidate trade idea on 500 bars before recommending it. Only mark a strategy as "promising" if the backtest shows win_rate > 55% and sharpe > 0.5.
```

The analyst's `how-to-write-a-ta-skill` template + the `kai/skills/btc-mtf-analysis-with-validation` skill (in the kai workspace, see [learning-and-skills.md](learning-and-skills.md)) both encode this pattern.

### Pattern 4 — Cross-venue validation

Cross-check the local data source against Coinbase before publishing a signal:

```
Pull BTC 6h from kai-api AND from Coinbase. If both venues agree on the regime, publish a signal on signals.cross_venue.BTC. If they disagree, hold off.
```

The analyst will use `query_ohlcv` for kai-api and `get_coinbase_candles` for the Coinbase side. See [data-sources.md](data-sources.md).

### Pattern 5 — Long-running scanner

Spawn the scanner and have it monitor pump.fun without blocking the main chat:

```
Spawn the scanner agent and have it check pump.fun trending tokens every 10 minutes for the next hour. If any token shows >50% gain in under 15 minutes, publish a [pump] alert on signals.scanner.{symbol}.
```

The scanner runs in the background, the main chat stays responsive, and alerts land in the alerts panel as they fire.

### Pattern 6 — Fork to a stronger model for one hard task

Switch the analyst to GPT-5.4 with maximum thinking for one hard analysis, then switch back:

```
/model analyst codex-cli/gpt-5.4
/think analyst xhigh
/analyze BTC 1h          # runs on GPT-5.4 xhigh
/model analyst kai-smart
/think analyst medium
/analyze BTC 1h          # back to the cloud default
```

### Pattern 7 — Org-chart project work

Build an actual feature using the dev-team agents:

```
I want to add a new chart timeframe — 2h. Ask the architect to design the change (file paths, what needs to update). Then ask the developer to implement it. Then ask the qa agent to write a test for it. Synthesize the three reports for me.
```

Each agent runs in its own workspace, has its own SOUL, and stays alive between turns so follow-up questions on the same topic reuse the same instance with its accumulated context.

---

## Sub-agent tools vs main-agent tools

Sub-agents have a deliberately smaller toolset than the main kai agent. They get:

- File operations: `file_read`, `file_write`, `file_edit`
- Shell + Python: `shell_exec`, `python_exec`
- Sandboxed shell: `docker_sandbox` (with their workspace bind-mounted as `/work`)
- Web fetch: `web_fetch`
- Frontier escalation: `codex_exec`, `claude_exec`
- All crypto tools: `query_ohlcv`, `get_latest_price`, `calculate_indicator`, `place_order`, `get_positions`, `scan_tokens`, `get_signals`, `run_backtest`, `get_coinbase_candles`, `get_coinbase_price`, `list_coinbase_products`
- NATS publish: `nats_publish`
- Memory + skills: `memory`, `skills_list`, `skill_view`, `skill_manage`

They do NOT have:

- `nats_request` — sub-agents shouldn't be requesting other sub-agents (cycles risk). Use `nats_publish` for fire-and-forget coordination.
- `spawn_agent` — only the main agent spawns sub-agents.
- `list_agents` — sub-agents shouldn't be enumerating peers.

This is enforced in `agent/tools.py:create_sub_agent_tools` (vs `create_tools` which builds the main agent's tool list).

---

## NATS topics reference

| Topic | Direction | Payload |
|---|---|---|
| `agent.{name}.request` | inbound to sub-agent | `{"task": "...", "from": "..."}` |
| `agent.{name}.response` | outbound from sub-agent | `{"response": "...", "task": "..."}` |
| `agent.{name}.status` | outbound from sub-agent | `{"state": "thinking" \| "fallback" \| "idle", ...}` |
| `system.registry` | outbound from manager | `{"agent": "...", "status": "online" \| "offline", "type": "sub-agent"}` |
| `signals.{strategy}.{symbol}` | inbound to consumer | `{"signal_type": "BUY", "price": ..., ...}` — see [data-sources.md](data-sources.md#signals) |
| `ai.analysis.completed` | inbound to consumer | `{"symbol": "...", "result_id": "...", "use_case": "..."}` |
| `agent.broadcast` | broadcast to everyone | `{"message": "...", "from": "..."}` |

All topics are stable — the consumer ring buffer in `agent/signal_consumer.py` and the alerts panel both subscribe to them.

---

## Adding a new sub-agent

1. Add a new entry to `agents` in `agent-config.json`:

```json
"newrole": {
  "description": "What this agent does",
  "endpoint": "kai-smart",
  "fallback_endpoints": [
    {"endpoint": "kai-local", "model": "qwen35-gptq"},
    {"endpoint": "codex-cli", "model": "gpt-5.4"}
  ],
  "workspace": "newrole",
  "max_iterations": 200
}
```

2. Create `workspaces/newrole/SOUL.md` with the role prompt (use any of the existing SOULs as a template).

3. Optionally drop a few starter skills into `workspaces/newrole/skills/*.md`.

4. Restart the TUI. The new agent will appear in `list_agents()` (which surfaces unspawned agents from config too) and can be spawned via `spawn_agent("newrole")`.

No code changes needed — the entire agent definition lives in config + filesystem.

---

## What to read next

- [models-and-thinking.md](models-and-thinking.md) — endpoint registry, fallback chains, reasoning effort levels
- [learning-and-skills.md](learning-and-skills.md) — the `/learn` reflection loop with a working example
- [configuration.md](configuration.md) — the full `agent-config.json` schema
- [architecture.md](architecture.md) — process model, NATS internals, contributor guide

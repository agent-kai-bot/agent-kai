---
name: how-to-write-an-execution-skill
description: Meta-skill — the template you should follow every time you write an order-execution or position-management skill
category: meta
tags: [meta, authoring, template, execution]
---
# How to write an execution skill

## When to use
Any time you just finished a hard order-placement or position-management task (3+ tool calls, an order that didn't fill how you expected, a manual adjustment) and want to bake the learning in. Execution skills are different from analysis skills — they describe **how to operate the tools** (place_order, get_positions, get_latest_price) under a specific constraint.

## What makes a good execution skill
- **Concrete tool choreography.** Which tool, with which arguments, in which order. Not "place a limit order" but "place_order(symbol, side='buy', type='limit', price=X, qty=Y, tp=Z, sl=W)".
- **Preconditions.** What must already be true before this skill applies? (Existing position? Portfolio cash? Specific volatility regime?)
- **Failure handling.** What do you do when the order partially fills, fails, or gets stuck? Execution skills that assume the happy path are useless.
- **Clean unwind.** Every execution skill should describe how to exit if the setup invalidates mid-operation.

## Required frontmatter
```
---
name: kebab-case-name
description: One sentence — what operation does this skill execute, and under what constraint?
category: execution
tags: [relevant, tags]
---
```

## Required body sections

### When to use
The exact precondition that makes this skill applicable. Include the portfolio state (e.g. "cash available ≥ X"), the instrument state (e.g. "no existing position in this symbol"), and the analyst signal being executed (e.g. "analyst returned a bb-squeeze-breakout verified setup").

### Steps
A numbered list the LLM can execute. Each step is either a tool call with explicit arguments, or a wait-for-condition, or a decision point.

Good step: `place_order(symbol, side='buy', type='limit', price=pullback_level, qty=base_qty, sl=stop_level, tp=target_level)`
Bad step: "Place the limit order at a reasonable price"

### Failure handling
A branch list: "if the order didn't fill within N seconds, do X. If the order filled but price moved against us by Y%, do Z." This is what separates a usable execution skill from a toy.

### Clean unwind
If the setup invalidates mid-skill — analyst revised the signal, price action broke the pattern — how do you exit cleanly? Usually: cancel pending orders, close any filled portion at market, log the invalidation.

## Tool reference quick sheet

| Tool | Use for | Key args |
|---|---|---|
| `place_order` | Entry, stops, targets, exits | symbol, side, type, qty, price (limit), sl, tp |
| `get_positions` | Pre-trade and post-trade state checks | symbol (optional filter) |
| `get_latest_price` | Sanity-check before placing any order | symbol |
| `nats_publish` | Broadcast fill confirmations to the bus | subject, payload |

## Creating, updating, and discarding
- **Create** after executing under an unusual constraint (a partial fill you handled, a retry you had to write). Don't write execution skills for the happy path.
- **Patch** immediately when an execution skill steers you wrong — execution errors compound faster than analysis errors.
- **Delete** a skill that assumes tool behavior that's since changed (e.g. paper-trade engine adds slippage simulation).

## Cost discipline
Execution skills are short by nature — usually 1-3 KB. If yours is longer than that, you're probably mixing in analysis logic that belongs in an analyst skill.

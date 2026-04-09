---
name: how-to-write-a-risk-skill
description: Meta-skill — the template you should follow every time you write a new risk-management skill
category: meta
tags: [meta, authoring, template, risk]
---
# How to write a risk skill

## When to use
Any time you had to make a non-obvious risk call (rejected a trade that the trader pushed back on, sized a position using a formula the default rules didn't cover, caught an exposure problem the standard checks missed) and want to bake it in so future-you is stricter.

## What makes a good risk skill
- **A hard number.** Risk skills that say "be careful about correlation" are useless. Risk skills that say "if 2+ positions have >0.7 Pearson correlation, cap combined size at 1x single-position limit" are useful.
- **A single decision boundary.** "Approve" vs "Reject" vs "Approve with modified size". The job of a risk skill is to automate a call you would otherwise make ad-hoc.
- **Audit trail.** The skill should tell you what to log so that when a bad trade happens, you can tell whether the rule was followed or not.
- **Conservative bias.** When the skill is ambiguous, it should err toward rejection. Risk skills are backstops, not profit centers.

## Required frontmatter
```
---
name: kebab-case-name
description: One sentence — what risk question does this skill answer?
category: risk
tags: [relevant, tags]
---
```

## Required body sections

### When to use
The specific situation this skill is designed to catch. Include the portfolio state, the proposed trade, and the specific dimension being checked (per-trade risk? portfolio heat? correlation? drawdown?).

### The rule
A single crisp rule with a hard number in it. Example: "Total concurrent dollar risk across all open positions must not exceed 4% of current equity."

### Inputs needed
List the numbers you need to pull to evaluate the rule. Where do they come from?
- Positions: `get_positions()`
- Prices: `get_latest_price(symbol)`
- Volatility: `calculate_indicator(symbol, "ATR", interval="1h")`
- Equity: derivable from the paper trading engine's portfolio state

### Decision
Spell out the decision procedure. Pseudocode is fine:
```
if rule_violated:
    return {"decision": "reject", "reason": ..., "numbers": ...}
if rule_violated_with_smaller_size:
    return {"decision": "approve_modified", "max_size": ...}
return {"decision": "approve"}
```

### Logging
What to write to the audit trail (chat history, MEMORY.md, a workspace file) so the decision can be reviewed later.

## Hard limits that override everything
These are the existing hard limits from the risk-manager SOUL. Your skills should respect them — if a skill approves a trade that violates these, the skill is buggy and needs patching:

- **Max single position size**: 5% of portfolio equity.
- **Max total portfolio exposure**: 20% of equity in open positions.
- **Every position must have a stop loss.** No exceptions.
- **Drawdown alert at 5%, halt at 10%.**
- **Correlation cap on concentrated altcoin exposure.**

A risk skill's job is to add finer-grained rules **within** these limits, not replace them.

## Creating, updating, and discarding
- **Create** after a session where the default rules didn't catch something they should have. That gap is the skill.
- **Patch** when a skill's threshold proves too loose or too tight. Risk thresholds drift with market conditions.
- **Delete** a skill whose hard number is now a user-set portfolio constant — move it to config instead.

## Cost discipline
Risk skills are typically the shortest of the three categories — 1-2 KB each. They're usually one table of numbers and a decision procedure. If a risk skill is over 5 KB you're either over-explaining or mixing in analysis that belongs elsewhere.

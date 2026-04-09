---
name: how-to-write-a-ta-skill
description: Meta-skill — the template you should follow every time you write a new TA skill for your own library
category: meta
tags: [meta, authoring, template, ta]
---
# How to write a TA skill

## When to use
Any time you just completed a hard analysis task (5+ tool calls, an initial wrong read, a corrected final answer) and want to make sure future-you doesn't repeat the trial and error. Use `skill_manage` with action `create` to save it. The point isn't to document everything — it's to capture the **specific decision tree** you had to work out the hard way.

## What makes a good TA skill
A good TA skill is:
- **Narrow.** One setup, one timeframe band, one decision. Not "how to trade BTC".
- **Reproducible.** Someone (future-you) could run the exact same steps on a fresh chart and reach the same conclusion.
- **Backed by indicators.** Use `calculate_indicator` outputs as verification, not eyeballing.
- **Honest about failure.** The Pitfalls section is the most valuable part — it captures the mistake you just avoided or made.

## Required frontmatter
Every skill file starts with a YAML block:

```
---
name: kebab-case-name
description: One sentence — what does this skill recognize or decide?
category: analysis
tags: [relevant, tags]
---
```

The `name` must match the filename slug (lowercase letters, digits, hyphens, underscores; 1-64 chars). The `description` is what shows up in `skills_list` — write it like a library index entry, not a tagline.

## Required body sections

### When to use
The specific precondition that makes this skill applicable. Include the timeframe ("on the 1h chart"), the indicator state ("when RSI < 30"), and the market regime ("in a defined downtrend"). If all three aren't true, the skill doesn't apply.

### Steps
A numbered list the LLM can literally execute. Each step should either:
- be a tool call (`calculate_indicator(symbol, "RSI", interval="1h", period=14)`), or
- be a comparison against that tool's output ("if RSI > 70, skip — we're late").

Avoid vague steps like "check the trend". If you find yourself writing one, break it down into the actual numeric check.

### Pitfalls
List the mistakes you just made, or almost made, in the session that produced this skill. Each pitfall should name the false-positive pattern and tell future-you the exact check that rules it out.

### Verification
A final objective check: "the trade is valid if ALL of these are true". This is the gate between "setup looks right" and "actually take action".

## Example structure
See `bb-squeeze-breakout` in the same library for a concrete example that follows this template end to end.

## Creating, updating, and throwing away
- **Create** after a session where you had to figure something out — don't write skills for setups you already knew.
- **Patch** the moment an existing skill steers you wrong. Don't wait for "next time". Use `skill_manage` action `patch` with the specific `old_string` to replace.
- **Delete** a skill that you've overridden twice in a row. A skill you keep ignoring is worse than no skill at all, because it crowds the `skills_list` output.

## Cost discipline
Your skills dir is injected by name + description into every system prompt. Keep descriptions tight (one sentence) and bodies focused. If a skill is pushing 10 KB, it's probably two skills or should move its reference material to a workspace file.

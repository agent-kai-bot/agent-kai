# Learning System

This document explains how the current self-learning loop works, what it is good at, why it sometimes returns `no_skill`, and how to design prompts that make skill creation more likely.

## The core idea

The system does not learn from raw market commentary very well.

It learns best from:

- reusable procedures
- decision trees
- scoring rubrics
- formulas
- validation routines
- repeated tool choreography that solved a non-trivial task

In short:

- skills are for `HOW`
- memory is for `WHAT`

## The runtime loop

The current loop is:

1. A target agent runs a real task.
2. The runtime records its tool calls and final response.
3. The user runs `/learn <agent>` or gets nudged to do so.
4. The TUI builds a reflection bundle from the last session.
5. The `mentor` agent reads that bundle.
6. The mentor returns one of:
   - `DECISION: create`
   - `DECISION: patch`
   - `DECISION: no_skill`
7. The TUI persists the drafted skill into the target agent's skill library.

## Important files

- [agent/learning.py](/home/atc/git/claude-local-ai-agent/agent/learning.py)
- [agent/skills_tool.py](/home/atc/git/claude-local-ai-agent/agent/skills_tool.py)
- [tui/terminal.py](/home/atc/git/claude-local-ai-agent/tui/terminal.py)
- [workspaces/mentor/SOUL.md](/home/atc/git/claude-local-ai-agent/workspaces/mentor/SOUL.md)
- [workspaces/mentor/skills/how-to-reflect-on-a-session.md](/home/atc/git/claude-local-ai-agent/workspaces/mentor/skills/how-to-reflect-on-a-session.md)
- [eval_skill_learning.py](/home/atc/git/claude-local-ai-agent/eval_skill_learning.py)

## Why `/analyze BTC` often does not create a skill

The current learning system explicitly avoids saving plain trading commentary as a skill.

That means a prompt like:

```text
/analyze BTC
```

often produces:

- a market opinion
- a directional read
- a few indicators
- no durable reusable method

From the mentor's point of view, that is often not a new skill.

## What the mentor is looking for

The mentor is looking for signs of:

- novelty
- iteration
- non-trivial workflow discovery
- existing-skill drift
- a repeatable decision process that should be remembered

Strong signals:

- multiple meaningful tool calls
- threshold-based logic
- classification or ranking
- explicit invalidation
- explicit verification
- a playbook or checklist in the final answer

Weak signals:

- one-off commentary
- trivial sessions
- no clear reusable method
- factual answers that belong in memory instead of skills

## What tends to produce `create`

These task shapes are strong candidates:

- multi-timeframe confluence
- breakout-vs-fakeout decision gates
- momentum ranking across several symbols
- pullback quality checks
- risk approval workflows
- leverage safety checks
- portfolio drift and rebalance procedures
- scanner triage and promotion logic
- hypothesis-to-backtest validation workflows

These are strong because they naturally produce:

- steps
- thresholds
- formulas
- failure cases
- verification

## What tends to produce `no_skill`

These task shapes are weak candidates:

- plain `/analyze BTC`
- "is this bullish?"
- "what do you think of SOL?"
- isolated commentary with no reusable logic
- outputs with no checklist, rubric, or formula

## Prompt design rule

If the result is useful but a future agent still would not know the method, the mentor often returns `no_skill`.

If the result gives a future agent an exact process it can reuse tomorrow, `create` becomes much more likely.

## Best prompting pattern

Use prompts that say:

```text
Do not just answer the immediate question. Solve it in a way that extracts a reusable workflow or decision rubric that could be saved as a skill.
```

And end prompts with:

```text
End by summarizing the method as a reusable playbook with when to use, inputs required, steps, pitfalls, and verification.
```

## Best practical user flow

1. Ask a specialist to solve a task as a reusable workflow.
2. Make sure the reply includes:
   - checklist
   - decision tree
   - scoring rubric
   - formulas
   - verification
3. Then run `/learn <agent>`.

Example:

```text
/analyze BTC using the goal of discovering a reusable workflow, not just market commentary. Build a decision checklist, validate it with indicators and, if appropriate, backtest it. End by summarizing the exact reusable procedure.
```

Then:

```text
/learn analyst
```

## Best roles for learning right now

The current system is strongest for:

- `analyst`
- `trader`
- `risk-manager`
- `scanner`
- `mentor`

These roles naturally produce structured, reusable workflows.

## Eval harness relevance

The skill-learning eval harness already contains good examples of skill-shaped tasks.

See:

- [eval_skill_learning.py](/home/atc/git/claude-local-ai-agent/eval_skill_learning.py)

It includes scenarios for:

- multi-symbol momentum ranking
- multi-timeframe confluence
- breakout validation
- dead-cat vs reversal logic
- pullback entry quality
- risk sizing
- daily loss enforcement
- concentration rebalance
- leverage safety

Those scenarios are good models for real-world prompts because they are:

- measurable
- multi-step
- tool-heavy
- reusable

## Related docs

- [docs/prompts.md](/home/atc/git/claude-local-ai-agent/docs/prompts.md)
- [docs/agents.md](/home/atc/git/claude-local-ai-agent/docs/agents.md)
- [docs/mentor/prompts/learn-trigger-patterns.md](/home/atc/git/claude-local-ai-agent/docs/mentor/prompts/learn-trigger-patterns.md)
- [docs/mentor/prompts/no-skill-vs-create.md](/home/atc/git/claude-local-ai-agent/docs/mentor/prompts/no-skill-vs-create.md)

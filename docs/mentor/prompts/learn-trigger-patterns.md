# Learn Trigger Patterns

This file captures the prompt patterns most likely to produce a useful `/learn` outcome.

## What tends to trigger skill creation

These prompt shapes are the strongest candidates for `DECISION: create`:

- prompts that ask for a reusable workflow, not just an answer
- prompts that require multiple tool calls
- prompts that force the agent to compare several alternatives
- prompts that require explicit thresholds, formulas, or scoring rules
- prompts that include invalidation logic
- prompts that include a verification or validation step
- prompts that end with a playbook, checklist, or decision tree
- prompts that produce a procedure that could be reused tomorrow on a different symbol

## What tends to produce no_skill

These prompt shapes are weak candidates for skill capture:

- plain `/analyze BTC` with no request for reusable logic
- one-off market commentary
- prompts that only ask "bullish or bearish?"
- prompts with too little tool use
- prompts with no explicit decision rules
- prompts with no verification step
- prompts that produce facts or observations but no reusable method

## Strong prompt framing

Prefix prompts with language like:

```text
Do not just answer the immediate question. Solve it in a way that extracts a reusable workflow or decision rubric that could be saved as a skill.
```

Or:

```text
Your goal is to discover or refine a reusable alpha-finding workflow, not just provide commentary.
```

## Strong ending pattern

Ask the target agent to end with:

- reusable playbook
- decision tree
- checklist
- candidate skill draft

That gives the mentor a much clearer procedural artifact to reflect on.

## High-probability domains

The following domains are especially skill-shaped:

- multi-timeframe confluence
- breakout validation
- fakeout rejection
- momentum ranking
- pullback quality checks
- entry timing and staging
- stop placement and risk sizing
- exposure caps and daily loss enforcement
- scanner triage and promotion logic
- backtest-before-promotion workflows

## Best practical pattern

1. Ask a specialist to solve a task as a reusable workflow.
2. Require explicit steps, thresholds, and verification.
3. Require a reusable playbook section at the end.
4. Then run `/learn <agent>`.

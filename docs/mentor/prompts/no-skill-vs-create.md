# No Skill Versus Create

This file is a practical guide to why the mentor returns `no_skill` versus `create`.

## When create is more likely

`create` is more likely when the reflected session shows:

- novelty
- iteration
- a non-trivial workflow
- a repeatable decision process
- tool choreography that would be useful in future sessions
- a final output that clearly contains procedural knowledge

In practice that usually means:

- 3 or more meaningful tool calls
- explicit metrics and thresholds
- some kind of ranking, filtering, approval, or classification logic
- a final checklist, playbook, rubric, or formula

## When no_skill is more likely

`no_skill` is more likely when the reflected session is:

- trivial
- already covered by an existing skill
- mostly market commentary
- a single-use answer without a reusable method
- factual rather than procedural

Typical examples:

- "Analyze BTC right now"
- "Is SOL bullish?"
- "What is the price of ETH?"
- "Give me your opinion on the market"

## The key distinction

The current system is much better at learning:

- how to decide
- how to validate
- how to size
- how to rank
- how to reject

It is much worse at learning:

- what the market looked like on one specific run
- a one-time directional opinion
- generic commentary

## Prompt design rule

If a human could say "that answer was useful, but I still do not know the repeatable method," the mentor will often return `no_skill`.

If a human could say "I could reuse this exact process tomorrow on another symbol," `create` becomes much more likely.

## Reliable prompt ingredients

Use prompts that ask for:

- a scoring rubric
- an approval or rejection framework
- explicit trigger conditions
- invalidation logic
- verification steps
- a reusable playbook section

## Recommended closing request

Add this to the end of prompts when you want to bias toward learning:

```text
End by summarizing the method as a reusable playbook with when to use, inputs required, steps, pitfalls, and verification.
```

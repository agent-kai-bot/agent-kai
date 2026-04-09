# Scanner Prompt Library

Use these prompts when the goal is to turn scanner output into ranked, reusable alpha discovery workflows.

## Scanner-driven discovery

```text
Do not just comment on one asset. Build and execute a reusable alpha-discovery workflow.

Objective:
Identify whether {SYMBOL} deserves promotion from scanner/watchlist candidate to active trade candidate.

Task:
- Treat {SYMBOL} as if it came from a scanner or alert.
- Use real data and indicators to evaluate:
  - regime
  - momentum
  - volume or participation
  - extension vs pullback quality
  - invalidation clarity
- Decide whether {SYMBOL} should be classified as:
  - trade now
  - watch for trigger
  - monitor only
  - reject
- If tradable, provide entry, stop, invalidation, and first target.
- If not, explain the rejection reason.

Requirements:
- Use multiple tool calls and exact metrics.
- Produce a triage checklist or scoring rubric.
- Include at least one rule that prevents false positives from low-quality scanner hits.
- End with a reusable playbook section:
  - when to use
  - inputs required
  - steps
  - pitfalls
  - verification

Make the final result look like a reusable scanner-triage skill that /learn would want to preserve.
```

## Multi-symbol ranking

```text
Rank a watchlist of symbols using a reusable scanner procedure.

Requirements:
- Compare at least 5 symbols.
- Use the same metrics for all of them.
- Return a ranked shortlist, not just isolated commentary.
- Explicitly state promotion and rejection rules.
- End with a reusable playbook that could be run daily.
```

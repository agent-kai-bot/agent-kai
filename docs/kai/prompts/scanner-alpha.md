# Scanner Alpha Prompt

```text
Do not just comment on one asset. Build and execute a reusable alpha-discovery workflow.

Objective:
Identify whether {SYMBOL} deserves promotion from scanner/watchlist candidate to active trade candidate.

Task:
- Treat {SYMBOL} as if it came from a scanner or alert.
- Use real data and indicators to evaluate:
  - regime
  - momentum
  - volume/participation
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

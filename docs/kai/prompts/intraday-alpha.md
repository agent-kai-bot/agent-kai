# Fast Intraday Alpha Prompt

```text
Do not just analyze the chart. Find a reusable intraday alpha workflow that could be saved as a skill.

Objective:
Determine whether {SYMBOL} has a valid short-term trade right now.

Task:
- Focus on 5m, 15m, and 1h.
- Use real data and indicators.
- Determine whether the setup is:
  - actionable long
  - actionable short
  - watchlist only
  - no-trade
- Use exact decision rules based on trend, momentum, volatility, and trigger quality.
- If actionable, provide entry, stop, invalidation, and first target.
- If not actionable, explain the failed criteria.

Requirements:
- Use multiple tool calls.
- Build a checklist or scoring rubric, not just commentary.
- Include one anti-fakeout verification step.
- End with a reusable playbook section:
  - when to use
  - inputs required
  - steps
  - pitfalls
  - verification

Optimize the output so /learn can recognize it as procedural knowledge.
```

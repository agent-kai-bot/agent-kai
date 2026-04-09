# Swing Alpha Prompt

```text
Do not just give a market opinion. Build and execute a reusable swing-trade workflow for {SYMBOL}.

Objective:
Decide whether {SYMBOL} has a valid swing setup over the next several days.

Task:
- Focus on 1h, 4h, and 1d.
- Use real data and indicators.
- Determine trend regime, momentum quality, volatility context, and location relative to key moving averages or bands.
- Decide whether the setup is:
  - actionable long
  - actionable short
  - watchlist only
  - no-trade
- If actionable, give entry, stop, invalidation, and first target.
- If not actionable, state exactly what is missing.

Requirements:
- Use exact thresholds and numbers.
- Build a repeatable decision process, not commentary.
- Include one confirmation step and one failure condition that would invalidate the trade idea.
- End with a reusable playbook section:
  - when to use
  - inputs required
  - steps
  - pitfalls
  - verification

Optimize for something /learn would save as a reusable swing-analysis skill.
```

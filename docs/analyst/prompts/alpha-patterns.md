# Analyst Alpha Patterns

Use these prompts when the analyst should discover repeatable setups rather than produce one-off commentary.

## Multi-timeframe confluence

```text
Analyze {SYMBOL} with the goal of discovering a reusable analyst workflow, not just giving market commentary.

Task:
- Determine whether {SYMBOL} currently has a high-quality multi-timeframe directional setup.
- Use 15m, 1h, and 4h.
- For each timeframe, compute 20 EMA, RSI(14), ATR(14), and Bollinger Bands.
- Decide whether each timeframe votes bullish, bearish, or neutral using explicit rules.
- If at least 2 of 3 timeframes agree, define entry, invalidation, and first target.
- If they do not agree, explain exactly why the setup is not tradeable.

Important:
- Return the exact thresholds and numbers used.
- End by summarizing the method as a reusable analyst playbook with:
  - when to use
  - step-by-step decision logic
  - common failure modes
  - verification
- Optimize for a workflow that could be reused tomorrow on a different symbol.
```

## Breakout validation

```text
Build and run a reusable breakout-vs-fakeout workflow for {SYMBOL}.

Requirements:
- Evaluate a break of the 24h high.
- Check volume vs 20-bar average, range vs ATR, close above prior high vs wick-only, and 1-2 bar follow-through.
- Return specific values for each criterion.
- End with one of: enter, wait for pullback, skip.
- Then convert the final logic into a reusable analyst checklist with exact thresholds and failure cases.
- Optimize for something /learn would want to preserve as procedural knowledge.
```

## Healthy pullback validator

```text
Find and formalize a reusable healthy-pullback entry workflow for {SYMBOL}.

Task:
- Determine whether the latest pullback in the current trend is tradeable.
- Check retrace depth versus the last impulse, pullback duration in bars, price location versus the 1h 20 EMA, and whether volume contracted during the pullback.
- If valid, return entry, stop, and first target.
- If invalid, explain which criterion failed.
- Then rewrite the final method as a reusable pullback-entry checklist with exact thresholds, failure cases, and verification steps.
- Structure the result so /learn would see it as a reusable playbook, not commentary.
```

## Range versus trend classifier

```text
Classify {SYMBOL} as ranging or trending using a reusable regime-detection workflow.

Requirements:
- Use 4h data.
- Measure ATR as a percentage of price, Bollinger width, and EMA slope.
- Return the exact numeric values and the final classification.
- If trending, state direction.
- If ranging, state approximate range high and low.
- End with a reusable regime-detection playbook including thresholds, pitfalls, and verification.
```

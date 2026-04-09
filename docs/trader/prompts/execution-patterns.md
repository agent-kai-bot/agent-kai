# Trader Execution Patterns

Use these prompts when the trader should produce reusable execution logic.

## Chase versus wait

```text
Evaluate {SYMBOL} as if you are building a reusable trader entry-decision skill, not just deciding whether to buy right now.

Task:
- Decide whether the correct action is:
  - enter now
  - wait for pullback
  - skip
- Use 15m and 1h data.
- Compute ATR(14), RSI(14), 20 EMA, and the distance of price from the 20 EMA in ATR units.
- Compare the current move to at least one recent similar move in the last 20 bars.
- If the correct action is wait, give the exact pullback entry price.
- If the correct action is enter, give entry, stop, and first target.
- If the correct action is skip, explain the disqualifying condition.

Important:
- Use exact numeric criteria, not vague language.
- End by rewriting the logic as a reusable trader playbook with:
  - trigger conditions
  - decision tree
  - invalidation
  - verification
- Make the output look like a repeatable execution framework, not commentary.
```

## Partial exit ladder

```text
Design a reusable partial-profit and stop-adjustment workflow for an open winning {SYMBOL} position.

Requirements:
- Start with current position data.
- Use current price and ATR on the active timeframe.
- Create a 3-rung exit ladder with exact prices, quantities, and stop adjustments.
- Define what happens after rung 1 fills, after rung 2 fills, and after rung 3 fills.
- Then rewrite the logic as a reusable trader skill for scaling out of winners.
- Make it procedural and reusable, not personalized commentary.
```

## Conviction-weighted allocation

```text
Create and execute a reusable capital-allocation workflow across three symbols.

Requirements:
- Assign conviction weights: HIGH, MEDIUM, LOW.
- Use current price plus a volatility-based stop for each asset.
- Size positions so total dollar allocation follows conviction but dollar risk is approximately balanced.
- Show quantities, stop levels, and risk per position.
- Then turn the method into a reusable trader allocation playbook with formulas, constraints, and verification steps.
```

## Enter now versus stage in

```text
Decide whether {SYMBOL} should be entered immediately or scaled in over multiple orders using a reusable execution workflow.

Requirements:
- Evaluate volatility, extension from mean, nearby invalidation, and liquidity/participation proxies.
- Choose immediate entry, staged entry, or skip.
- If staged, define exact tranche prices and sizing.
- End with a reusable execution playbook covering trigger, staging logic, invalidation, and verification.
```

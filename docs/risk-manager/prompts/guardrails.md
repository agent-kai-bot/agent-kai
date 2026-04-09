# Risk-Manager Guardrails

Use these prompts when the risk-manager should produce reusable approval and protection logic.

## Position approval

```text
Build and execute a reusable risk-approval workflow for a proposed {SYMBOL} trade.

Requirements:
- Use current price, ATR on the active timeframe, and a volatility-based stop.
- Apply a fixed account-risk rule.
- Check single-position cap and total heat cap.
- Return approve, approve_modified, or reject with exact numbers.
- Then formalize the logic as a reusable risk-manager skill with formulas, thresholds, escalation cases, and verification.
```

## Daily loss limit enforcement

```text
Create and run a reusable daily-loss-limit enforcement workflow.

Requirements:
- Inspect current portfolio state including open positions and daily P&L.
- Apply staged rules such as warn-and-reduce at a smaller drawdown and hard halt at a larger drawdown.
- Identify the largest dollar losers.
- Compute how much loss exposure must be reduced to get back under limits.
- Recommend specific exit actions in priority order.
- Then rewrite the method as a reusable risk-manager playbook with thresholds, staged responses, and verification.
```

## Leverage safety check

```text
Build and execute a reusable leverage safety check for a proposed leveraged {SYMBOL} trade.

Requirements:
- Compare the proposed stop size to short-horizon ATR as a noise floor.
- Also assess realized daily range from higher timeframe data.
- Decide whether the stop is too tight for current volatility.
- Return reject or approve_modified if necessary, with exact replacement guidance.
- Then convert the reasoning into a reusable risk-manager procedure with formulas, failure cases, and verification.
```

## Concentration rebalance

```text
Create and execute a reusable concentration-drift and rebalance workflow.

Requirements:
- Inspect current portfolio allocations.
- Compare actual weights to target weights.
- Define the trigger for rebalancing.
- Compute exact sell and buy amounts needed to restore target allocations.
- End with a reusable guardrail playbook covering drift thresholds, rebalance math, pitfalls, and verification.
```

# Risk-Manager Prompt Library

Use these prompts when the goal is to produce reusable approval and guardrail logic.

## Best single prompt

```text
Build and execute a reusable risk-approval workflow for a proposed BTC long.

Requirements:
- Use current price, 1h ATR(14), and a 1.5x ATR stop.
- Apply a 1% account-risk rule.
- Check single-position cap and total heat cap.
- Return approve, approve_modified, or reject with exact numbers.
- Then formalize the logic as a reusable risk-manager skill with formulas, thresholds, escalation cases, and verification.
```

## Daily loss limit enforcement

```text
Create and run a reusable daily-loss-limit enforcement workflow.

Requirements:
- Inspect current portfolio state including open positions and daily P&L.
- Apply these rules: warn and halve size at 2% drawdown, hard halt at 5%.
- Identify the largest dollar losers.
- Compute how much loss exposure must be reduced to get back under limits.
- Recommend specific exit actions in priority order.
- Then rewrite the method as a reusable risk-manager playbook with thresholds, staged responses, and verification.
```

## Leverage safety check

```text
Build and execute a reusable leverage safety check for a proposed BTC leveraged long.

Requirements:
- Evaluate a 3x BTC long with a tight stop.
- Compare the stop size to 1h ATR as a short-horizon noise floor.
- Also assess realized daily range from higher timeframe data.
- Decide whether the stop is too tight for current volatility.
- Return reject or approve_modified if necessary, with exact replacement guidance.
- Then convert the reasoning into a reusable risk-manager procedure with formulas, failure cases, and verification.
```

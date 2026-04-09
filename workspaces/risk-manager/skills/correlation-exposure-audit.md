---
name: correlation-exposure-audit
description: Detect when multiple open positions are effectively one bet via asset correlation, and cap combined size
category: risk
tags: [risk, correlation, concentration, diversification]
---
# Correlation exposure audit

## When to use
The trader is about to add a position in a symbol, and the portfolio already has exposure to a correlated instrument. Crypto is famous for "every coin is BTC on leverage" — five long altcoin positions aren't five trades, they're one leveraged BTC bet.

## The rule
If the proposed trade's symbol has a rolling 14-day correlation of ≥0.7 with any currently-open position, the combined dollar-at-risk across the correlated group must not exceed 1.5× the per-trade risk limit (i.e. if per-trade is 1% of equity, the correlated group cap is 1.5% of equity total).

## Correlation groups (quick heuristic, no math needed for known pairs)
Use these as defaults when there's no time to compute the actual correlation:

- **Tier 1 — highly correlated (≥0.8 typical):** BTC, ETH, SOL, LINK, AVAX and most large-cap alts during risk-on regimes.
- **Tier 2 — moderately correlated (0.5-0.7):** Mid-caps tracking large caps with a lag.
- **Tier 3 — decorrelated or inverse:** Stablecoins, USD, occasionally DeFi tokens during protocol-specific news.

For any large-cap pair (Tier 1 ↔ Tier 1), assume correlation ≥0.7 without computing.

## Inputs needed
1. Current open positions: `get_positions()`.
2. Proposed trade symbol and size.
3. Equity: from portfolio state.
4. (Optional, for precision) 14-day price history of both symbols: `query_ohlcv(symbol, "1d", limit=14)` for each, then compute Pearson correlation of the daily returns.

## Calculation
```
correlated_positions = [p for p in open_positions if correlation(p.symbol, proposed.symbol) >= 0.7]
current_correlated_risk = sum(|p.entry - p.stop| * p.qty for p in correlated_positions)
proposed_risk           = |proposed.entry - proposed.stop| * proposed.qty
combined_risk           = current_correlated_risk + proposed_risk
cap                     = 0.015 * equity   # 1.5% for the correlated group
```

## Decision procedure
1. Build the correlated set. Start with the Tier heuristic, upgrade to actual Pearson if ambiguous or if the trade is large.
2. Compute `combined_risk`.
3. If `combined_risk <= cap` → approve (other skills still need to approve too).
4. If `combined_risk > cap` → reject. Return the specific overlap:
   - Which positions were flagged as correlated
   - Their combined current risk
   - The max new-trade risk that would fit under the cap
   - A suggestion: "Reduce proposed size to Q, or close position X first"

## Logging
```
{timestamp} CORRELATION {result} symbol={sym} correlated_with=[list] combined_risk={usd} cap={usd}
```

## Pitfalls
- **Treating every altcoin trade as independent.** This is the single biggest risk-manager failure mode in crypto. The default assumption should be "correlated unless proven otherwise", not the reverse.
- **Using correlation on the wrong window.** Intra-day correlations are noisy; 14-day daily-return correlations are stable and useful. Don't compute correlation on 5m bars — it'll tell you everything is correlated with everything because of microstructure noise.
- **Forgetting stables.** A USDC position has near-zero correlation with BTC, so stablecoin yield farms don't count in the correlated group. Don't penalize them.
- **Gaming the cap with tiny stops.** If the trader proposes a super-tight stop to slip under the cap, the correlation-exposure-audit still passes but the `atr-stop-sizing` skill should reject the stop for being inside the noise floor. Make sure both skills run.

## Verification
Decision output must include:
- [ ] List of correlated open positions (with symbols and per-position risk)
- [ ] Combined risk USD
- [ ] Cap USD
- [ ] Decision + specific mitigations if rejected
- [ ] Which method was used to determine correlation (heuristic vs computed Pearson)

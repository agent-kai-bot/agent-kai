---
name: ema-workflow-search-with-oos-validation
description: Search EMA crossover strategies with trend filters, then validate candidates out of sample before considering them usable.
category: analysis
tags: [ema, backtest, validation, btc]
---
# EMA Workflow Search with OOS Validation

## When to use
When the user wants to find a reusable EMA-based strategy rather than just inspect one hand-picked parameter set.

## Steps
1. Start with a small in-sample grid on the target timeframe.
2. Use a simple base rule first: short EMA fast crosses above short EMA slow, with optional long-trend filter; exit on reverse cross or loss of structure.
3. Rank candidates by total return, Sharpe, drawdown, and trade count. Reject candidates with very low trade counts unless explicitly testing rarity.
4. Validate top candidates on a separate out-of-sample window.
   - If the backtester only supports trailing-bar windows, be explicit about whether a follow-up test is a truly disjoint unseen window or just a stricter recent subwindow stress test.
   - Track more than Sharpe: total return, buy-and-hold return, max drawdown, number of trades, win rate, avg trade %, best/worst trade %, and exposure %.
5. If entries seem promising but underperform, test exit variants:
   - reverse cross
   - close below EMA
   - stop loss / take profit
   - structure filter (price above EMA200, EMA20>EMA50>EMA200)
6. Cross-check promising candidates on Coinbase data if local data is thin or to sanity-check venue dependence.
7. Only call it alpha if it remains profitable out of sample with acceptable trade count and drawdown.

## Pitfalls
- Overfitting by maximizing return on one short window.
- Mistaking two-trade outperformance for robust edge.
- Using filters so strict that OOS has almost no trades.
- Comparing strategies to buy-and-hold without also checking risk and exposure.

## Verification
A candidate is worth keeping only if it stays profitable out of sample, has more than a trivial number of trades, and does not collapse on a second data source or nearby parameter values.

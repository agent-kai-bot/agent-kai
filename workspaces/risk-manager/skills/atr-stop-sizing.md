---
name: atr-stop-sizing
description: Compute position size from ATR-based stop distance so per-trade risk is always a fixed percent of equity
category: risk
tags: [risk, sizing, atr, stop, volatility]
---
# ATR-based stop sizing

## When to use
The trader is proposing a trade and asks for an approved size. This is the default sizing skill — use it any time there isn't a more specific rule (e.g. a setup-specific skill) that overrides it.

## The rule
Per-trade dollar risk = `0.01 * equity` (1% of current portfolio equity). Position size is derived so that if the stop is hit, the loss equals this risk budget exactly.

## Inputs needed
1. Current equity: derive from the paper trading engine's portfolio state via `get_positions()` (use the reported total portfolio value / NAV).
2. Entry price: from the trader's proposal.
3. ATR on the relevant timeframe: `calculate_indicator(symbol, "ATR", interval="1h", period=14)`.
4. Stop distance multiplier: default is 1.5 × ATR for swing trades, 1.0 × ATR for scalps. The trader or analyst should specify which.

## Calculation
```
equity          = portfolio.total_value          # from get_positions()
risk_budget_usd = 0.01 * equity                  # 1% rule
stop_distance   = multiplier * atr_1h            # 1.5 default
position_size   = risk_budget_usd / stop_distance
```

Position size is in **base units** (BTC, ETH, SOL, etc.), not dollars. Convert to dollars for the exposure cap check below: `dollar_exposure = position_size * entry_price`.

## Decision procedure
1. Compute `position_size` from the formula.
2. Compute `dollar_exposure`.
3. Check against hard limits:
   - If `dollar_exposure > 0.05 * equity` → cap at `0.05 * equity / entry_price` and flag as "capped by single-position limit". The real per-trade risk is now LESS than 1% — that's fine, it means ATR was tight relative to max size.
   - If adding this position would make total open exposure > `0.20 * equity` → reject. Return a suggestion: close or scale down another position first.
4. Check stop requirement: the proposal must include a stop loss. If no stop, reject with "no stop loss".
5. Return the approved size, the stop level (`entry - stop_distance` for long, `entry + stop_distance` for short), and the exact dollar risk.

## Logging
Write one line to the decision audit:
```
{timestamp} APPROVE {symbol} {side} size={qty} entry={px} stop={stop} risk_usd={risk}
```
Or `REJECT` with the reason code. This should go to the risk-manager's MEMORY.md via the memory tool OR a workspace log file.

## Pitfalls
- **Using ATR on the wrong timeframe.** A 1m ATR is tiny and will size you into a huge position; a 4h ATR is large and will tiny-size you. Match the ATR timeframe to the hold duration (scalp = 5m/15m ATR, swing = 1h, position = 4h).
- **Ignoring the single-position cap.** When volatility is very low, the 1% risk formula can call for a position > 5% of equity. The cap wins. Do not override it.
- **Using stale ATR.** Crypto ATR can change 2x in a day during volatility regime shifts. Refetch per trade, don't cache.
- **Accepting a stop the trader picked that's inside the ATR band.** A stop tighter than 1 ATR almost guarantees noise stop-outs. Push back if the trader proposes one.

## Verification
Decision output must include:
- [ ] Approved qty (base units)
- [ ] Approved dollar exposure
- [ ] Stop level
- [ ] Exact dollar risk if stopped
- [ ] Whether the size was capped by the 5% rule
- [ ] Total portfolio exposure after this trade
- [ ] Audit-trail line written

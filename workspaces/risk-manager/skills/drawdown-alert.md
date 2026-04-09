---
name: drawdown-alert
description: Monitor rolling drawdown from peak equity and emit warnings or trading halts at defined thresholds
category: risk
tags: [risk, drawdown, equity-curve, halt]
---
# Drawdown alert

## When to use
Continuously, or at minimum at the start of every risk review and before approving every new trade. This skill is not about sizing individual trades — it's about detecting when the PORTFOLIO is in trouble and the right call is to stop trading rather than keep sizing.

## The rule
- **Peak equity** = the highest equity value recorded since the current trading "session" began. A session resets on a deliberate reset command or when the portfolio crosses a new high.
- **Current drawdown** = `(peak_equity - current_equity) / peak_equity`
- **Warning threshold**: 5% drawdown → emit warning, tighten size, reject aggressive trades.
- **Halt threshold**: 10% drawdown → reject all new trades. Only allow position-reduction or stop-management actions.

## Inputs needed
1. Historical peak equity: stored in a workspace file (e.g. `workspaces/risk-manager/drawdown_state.json`) between sessions.
2. Current equity: from `get_positions()` aggregate or paper trading portfolio state.
3. Any recent equity high-water mark updates.

## State management
The peak equity must persist between sessions because drawdown is defined relative to a peak, not to last session's starting equity. Use:
- `file_read("workspaces/risk-manager/drawdown_state.json")` at session start.
- Initialize to current equity if the file doesn't exist.
- Update peak whenever `current_equity > peak` and write atomically via `file_write`.

State file shape:
```json
{
  "peak_equity": 105000.00,
  "peak_set_at": "2026-04-08T10:00:00Z",
  "last_updated": "2026-04-08T11:30:00Z"
}
```

## Decision procedure
1. Load `peak_equity` from state.
2. Compute `current_equity`.
3. If `current_equity > peak_equity` → update peak, write state, no alert.
4. Compute `dd = (peak - current) / peak`.
5. Decision table:

   | Drawdown | Action |
   |---|---|
   | < 5% | no action |
   | 5% ≤ dd < 7% | emit warning, halve per-trade risk budget (from 1% to 0.5%) |
   | 7% ≤ dd < 10% | emit stronger warning, quarter per-trade risk budget (0.25%), reject new exposure in correlated groups |
   | ≥ 10% | emit HALT alert, reject all new trades, only allow position-reduction |

6. Publish alerts via `nats_publish("alert.drawdown", {level: ..., dd_pct: ..., peak: ..., current: ...})`.

## Logging
Every check should write to a drawdown log (append to a workspace file):
```
{timestamp} DRAWDOWN dd={dd_pct} peak={peak} current={current} action={none|warn|halt}
```

## Pitfalls
- **Drawdown from session start instead of peak.** If you reset peak on every session start, you'll never trigger the alert — a portfolio down 8% from a 2-week peak doesn't show as drawn down in today's session. Peak must be persistent.
- **Halt that's too hard to exit.** The halt should allow position-REDUCTION actions (tightening stops, closing losers, taking profits). If you reject all `place_order` calls when halted, you can't even close the positions that caused the drawdown. Only reject NEW net exposure.
- **Halt without notification.** A silent halt feels like the agent is broken. Always `nats_publish` so the TUI shows an alert.
- **Resetting peak to current on a new high without logging.** When the portfolio hits a new high, the drawdown resets — but you should still log that transition so the eval harness can see the full equity curve.

## Verification
Decision output must include:
- [ ] Current peak equity
- [ ] Current equity
- [ ] Drawdown percent
- [ ] Action taken (none / warn / halt)
- [ ] Any alerts published
- [ ] State file updated if peak changed

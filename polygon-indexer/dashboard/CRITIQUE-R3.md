# Codex Dashboard Final Verdict

## 1. Is every component now specified enough to build without guessing?

No.

Blocking issues in the frozen spec itself:

- The shell layout still includes `VolumeChart` and `WhaleAlertRail` in Row 3 ([spec lines 49-53](/tmp/dashboard-v3-frozen-spec.md:49)), but neither appears in the MVP component list ([lines 7-19](/tmp/dashboard-v3-frozen-spec.md:7)). Meanwhile `GasArc` is in MVP ([line 15](/tmp/dashboard-v3-frozen-spec.md:15)) but has no explicit slot in the shell layout.
- `HeroWhaleFeed` is specified as a component ([lines 203-211](/tmp/dashboard-v3-frozen-spec.md:203)), while the layout names a separate `WhaleAlertRail` ([line 52](/tmp/dashboard-v3-frozen-spec.md:52)). That leaves an unresolved question: one whale surface or two?
- `VolumeChart` has no component spec at all, despite being in the frozen layout ([line 51](/tmp/dashboard-v3-frozen-spec.md:51)).
- `TokenInspectorDrawer` depends on "existing per-token endpoints" instead of frozen response schemas ([lines 228-235](/tmp/dashboard-v3-frozen-spec.md:228)). That still forces implementation-time interpretation.
- Several typography/details are still not frozen per component. `ChainPulseBar`, `TokenRailItem`, and `GasArc` have some type specs, but `HeroWhaleFeed`, `SystemSummary`, the drawer sections, and the BlockTape tooltip do not.

## 2. Are the backend contracts complete?

No.

Missing or incomplete contracts:

- `GET /v1/polygon/overview` does not include everything needed for `SystemSummary`: it lacks an explicit `transfers_indexed_count`, and "Last updated timestamp" is not defined as a specific field ([spec lines 213-218](/tmp/dashboard-v3-frozen-spec.md:213), [lines 75-110](/tmp/dashboard-v3-frozen-spec.md:75)).
- `GasArc` needs percentile position across the last 100 blocks ([lines 220-226](/tmp/dashboard-v3-frozen-spec.md:220)), but no frozen contract provides the 100-block distribution or a precomputed percentile.
- `HeroWhaleFeed` tiers are USD-based ([lines 206-209](/tmp/dashboard-v3-frozen-spec.md:206)), but the frozen `whale-transfers` and SSE `whale` payloads omit `usd_value` ([lines 120-137](/tmp/dashboard-v3-frozen-spec.md:120), [lines 248-255](/tmp/dashboard-v3-frozen-spec.md:248)).
- `GET /v1/polygon/blocks/recent?limit=40` is named, but its response schema is not frozen ([lines 140-143](/tmp/dashboard-v3-frozen-spec.md:140)).
- Drawer APIs are not frozen in this document; they are only referenced indirectly as "existing per-token endpoints" ([line 234](/tmp/dashboard-v3-frozen-spec.md:234)).

Current repo state also confirms these are still net-new or changed contracts:

- There is no `/v1/polygon/overview` or `/v1/polygon/stream` route in the current backend ([src/analytics/app.py](/home/atc/git/claude-local-ai-agent/polygon-indexer/src/analytics/app.py:611)).
- The current `whale-transfers` response shape is different from the frozen spec: it returns `symbol`, `amount`, `usd_value`, and `quote_symbol` today ([src/analytics/app.py](/home/atc/git/claude-local-ai-agent/polygon-indexer/src/analytics/app.py:881)).

## 3. Any remaining ambiguity?

Yes.

- Is `VolumeChart` in v1 or out of scope?
- Is `WhaleAlertRail` a second component, or the right-rail name for `HeroWhaleFeed`?
- Where does `GasArc` actually render in the frozen shell?
- Does v1 SSE include `status` or only `head` and `whale`? Section 3 says only `head` and `whale` ([lines 145-158](/tmp/dashboard-v3-frozen-spec.md:145)); Section 6 adds `status` ([lines 257-261](/tmp/dashboard-v3-frozen-spec.md:257)).
- What exact payloads do the drawer endpoints return?
- What denominator should the drawer holder `%` use: total supply, tracked balances sum, or something else?
- How is the whale feed bootstrapped on first load: time-desc recent items, value-desc whales, or some explicit query?
- If tracked tokens exceed 5, what is the selection/sort rule for `TokenRail`?
- For `<1280`, what is the vertical stack order and scroll ownership after the layout collapses to one column?

## 4. Verdict

**STILL NEEDS MORE**

The spec is close, but it is not yet safe to implement "without guessing." One short freeze pass should still reconcile:

1. Shell layout vs MVP scope (`VolumeChart`, `WhaleAlertRail`, `GasArc`)
2. Exact drawer contracts
3. Missing data fields for `SystemSummary`, `GasArc`, and USD-tiered whale rows
4. Final SSE event list

## 5. If CONVERGED: exact build order for implementation

Not applicable. The spec is not converged.

# Codex Dashboard R4 Verdict

## Verdict

**CONVERGED**

Taken together, `/tmp/dashboard-v3-frozen-spec.md` plus the delta in
`/tmp/dashboard-v4-final-freeze.md` are now specific enough to build
without making product decisions on the fly.

The important distinction is:

- the current repo still does **not** implement several of these backend
  contracts yet
- but that is now implementation work, not spec ambiguity

The only framing note is that v4 is a patch over v3, not a standalone
replacement. Read them together.

## Exact Build Order

1. Implement `GET /v1/polygon/overview`.
   Include the v3 fields plus the v4 additions:
   `total_transfers_indexed`, `last_updated_at`,
   `gas_percentile_rank`, `gas_history_100_blocks`.

2. Implement `GET /v1/polygon/blocks/recent?limit=40`.
   Return the frozen block rows exactly as specified and keep them
   sorted by `block_number DESC`.

3. Enhance `GET /v1/polygon/whale-transfers`.
   Support the v1 feed bootstrap contract:
   `limit=30`, timestamp-desc ordering, and payload fields
   `token_symbol`, `token_decimals`, `amount_human`, `usd_value`.

4. Enhance `GET /v1/polygon/tokens/{address}/holders?limit=10`.
   Add `balance_human` and `pct_of_tracked` with the frozen denominator:
   sum of indexed balances for that token.

5. Implement `GET /v1/polygon/stream` as SSE.
   Ship all three events exactly as frozen:
   `head`, `whale`, `status`.

6. Add backend tests for the new and changed contracts.
   Cover overview shape, block ordering, whale bootstrap ordering,
   holder percentage math, and SSE event payloads.

7. Build the dashboard shell.
   Sticky `ChainPulseBar`, sticky `BlockTape`, 3-column desktop layout,
   `<1280px` stack order, and right-edge overlay drawer behavior.

8. Build the frontend data layer.
   Bootstrap with `/overview` and `/whale-transfers?limit=30`, then
   merge live SSE updates into local state.

9. Implement core overview components in dependency order.
   `ChainPulseBar` → `BlockTape` → `TokenRail` → `HeroWhaleFeed`
   → `SystemSummary` → `GasArc`.

10. Implement `TokenInspectorDrawer`.
    On open, fetch token detail, top holders, and recent transfers, then
    render the frozen sections and typography.

11. Add interaction polish.
    Insert animations, sticky behavior, auto-scroll/pause behavior,
    type system, hover states, and alert tier styling.

12. Run final QA against the real edge cases.
    No-price whales, backfill-in-progress, nonzero lag, more than 5
    tracked tokens, empty states, and `<1280px` behavior.

## Bottom Line

This is now at the point where implementation can start. The remaining
work is coding the frozen contracts and UI, not resolving product
ambiguity.

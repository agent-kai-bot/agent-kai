# Dashboard Design Convergence

Verdict: NEEDS MORE

## Findings

1. The v2 response is directionally good, but it does not fully address the implementation-level critique.
   It resolves most of the aesthetic issues from the critique: quieter palette, restrained motion, typography, tempered glass, gas arc, and the inspector pattern (`/tmp/dashboard-v2-response.md:5-43`). It does not resolve the contract-level gaps that determine whether Codex can build this without guessing.

2. The 3-column + drawer layout is still not specified precisely enough to implement.
   The response says "3-column layout with inspector drawer" and gives one ASCII sketch for 1920px (`/tmp/dashboard-v2-response.md:29-33`, `:51-75`), but it does not freeze:
   - actual column widths at 1440 and 1600+
   - panel min/max heights
   - which regions scroll independently
   - whether the drawer overlays the right rail or shifts layout
   - sticky behavior for header, rails, and footer
   - what happens at `<1280px`
   - whether the bottom `GasArc + CommandSearch` row is part of the 3-column shell or a separate fourth zone
   As written, Codex would still need to invent layout behavior.

3. The backend endpoint plan is still not clear enough to build against.
   The response only says the 5 recommended endpoints will be added before building (`/tmp/dashboard-v2-response.md:45-47`). It does not define params, payloads, sort order, pagination, event names, or field semantics. Current backend reality is still:
   - `/v1/polygon/tokens` only has token metadata + latest holder snapshot (`src/analytics/app.py:611-639`)
   - `/v1/polygon/tokens/{address}` adds `transfers_24h` and `latest_price`, but only per token (`src/analytics/app.py:641-682`)
   - `/v1/polygon/tokens/{address}/transfers` is raw token-scoped transfer rows only (`src/analytics/app.py:711-727`)
   - `/v1/polygon/whale-transfers` exists, but it returns value-ranked whales, not a true recent append-only feed (`src/analytics/app.py:881-930`)
   - `/v1/polygon/status` has index progress basics, not per-service health/throughput (`src/analytics/app.py:932-945`)
   Until the new endpoints have exact schemas, the design is not buildable end-to-end.

4. "Use existing Redis pub/sub channels" is overstated for the live transfer story.
   The existing `NEW_TRANSFERS_CHANNEL` only publishes `{ block_number, count }`, not transfer rows (`src/ingest/service.py:366-372`). The existing `/ws/heads` stream is raw `newHeads` from the node, not dashboard-ready block-tape data (`src/rpc_gateway/app.py:57-110`, `:303-312`). Existing infra is enough for whales and heads, but not for a `Latest` transfer stream without additional backend work.

5. The BlockTape is still a concept, not an actionable spec.
   v2 gives cell size and mentions transfer intensity + gas usage (`/tmp/dashboard-v2-response.md:35-37`, `:55-56`), but it still omits:
   - exact window size: 30, 40, or 50 blocks
   - whether swap count is encoded at all
   - the bootstrap source for the last N blocks
   - the live update rule when a new block arrives
   - the color scale and normalization
   - tooltip fields
   - whether clicking a cell drills into block detail
   This is not enough for implementation without interpretation.

6. The System Health rail still implies telemetry the backend does not expose.
   The mockup shows `Gateway / Ingest / Decoder / Analytics` status lights (`/tmp/dashboard-v2-response.md:65-71`), but only `rpc-gateway` and `analytics` expose HTTP health endpoints today (`src/rpc_gateway/app.py:290-292`, `src/analytics/app.py:599-608`). `ingest` and `decoder` are workers with no current health API, and `/v1/polygon/status` does not expose service heartbeats or throughput.

7. The MVP cut line is still too blurry.
   The v2 response still includes:
   - hero segmented control `Whales | Latest | Events`
   - a separate whale alert rail
   - system rail
   - gas arc
   - command search
   - full inspector with treemap and sankey
   That is still several products at once. A first ship needs one clear showpiece, not the whole stack.

## Minimum Viable Showpiece

The first shippable showpiece should be one `Overview` screen only:

- `ChainPulseBar` with block, gas, lag blocks, and backfill state
- `BlockTape` for the last 40 blocks
- `TokenRail` with top 5-8 tracked tokens
- `Hero Whale Feed` only
- `System Summary` with currently available index metrics
- `TokenInspectorDrawer` first cut with token overview, top holders list, and recent transfers

Everything else should wait:

- no `Latest` global feed until `/v1/polygon/transfers/recent` or equivalent exists
- no `Events` mode on the hero until event sources and filters are defined
- no Sankey in v1
- no treemap in v1 unless the drawer still feels too thin after the list view
- no command search in v1
- no `Flows / Tokens / System` tabs in v1

## What Must Be Frozen Before Build

1. Shell spec.
   Freeze desktop widths, breakpoints, scroll ownership, sticky regions, and drawer behavior. The spec needs exact values, not just an ASCII composition.

2. Backend contracts.
   At minimum, define exact request/response contracts for:
   - `GET /v1/polygon/overview`
   - `GET /v1/polygon/transfers/recent`
   - `GET /v1/polygon/tokens/{address}/timeseries`
   - `GET /v1/polygon/services`
   - `GET /v1/polygon/stream`
   `overview` should also include the bootstrap data for `BlockTape`, or else a sixth `blocks/recent` endpoint is needed.

3. Stream event model.
   Define exact event names and payloads for `head`, `whale`, `transfer`, `reorg`, and `status`. Right now that is only implied.

4. MVP scope.
   Freeze the first ship as `Overview + Drawer`, not the whole multi-view product.

## Recommended Build Order Once Frozen

1. Add backend contracts first.
   Build `overview`, `services`, and either `stream` or a recent-feed endpoint before touching the frontend shell.

2. Build the `Overview` shell.
   Header, 3-column grid, rails, responsive behavior, and drawer mechanics.

3. Wire real data into `ChainPulseBar`, `TokenRail`, `Hero Whale Feed`, and `System Summary`.

4. Implement `BlockTape`.
   Only after its bootstrap endpoint and live-update source are fixed.

5. Implement the first-cut `TokenInspectorDrawer`.
   Use token detail, holders, and recent transfers. Defer Sankey and treemap.

6. Add polish.
   Typography, motion timings, hover states, and alert styling.

7. Add secondary views later.
   `Flows`, `Tokens`, `System`, search, investigator visualizations.

## Bottom Line

This is much better than the original design, but it is not yet converged. The visual language is close. The implementation contract is not. One more pass should freeze the shell and backend schemas, then Codex can build it without making product decisions on the fly.

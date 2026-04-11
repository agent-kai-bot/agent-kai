# Critic Review: Polygon Node Design

Reviewer: Claude (critiquing codex's design)

## Overall assessment

This is a well-structured, phased design that correctly prioritizes
product-level analytics over raw RPC exposure. The layered
architecture (node → gateway → ingest → decode → analytics → API)
is the right pattern. The phasing is practical.

## What's good — preserve these

1. **"No raw RPC to users" principle** — correct. The node is an
   internal data source, not a public endpoint. Agents query
   summarized data through tools, not raw eth_getLogs.

2. **Provider router with fallback** — using the local node as
   primary with Alchemy fallback is exactly right. Capability
   detection at startup is a nice touch.

3. **Curated scope** — starting with tracked tokens, curated DEXs
   (QuickSwap + Uni V3), and specific event types instead of
   "index everything" is realistic.

4. **Phase 1 doesn't depend on archive mode** — critical since the
   node status is "almost synced" and may not be archive.

5. **DEX-derived OHLCV from swap events** — high value, unique to
   owned-node operators. Alchemy rate-limits make this expensive
   via provider; local node makes it free.

## Concerns

### 1. Infrastructure complexity for a small team
The design proposes: RPC Gateway, Ingest Service, Decoder,
Analytics Service, plus Postgres, Redis, and optional ClickHouse.
That's 4 services + 2-3 datastores for Phase 1. For a team that's
currently running one backend container + NATS, this is a big
operational jump.

**Suggestion:** Collapse gateway + ingest + decoder + analytics
into ONE Python service with clear module boundaries. Split into
separate services later if/when scaling demands it. Postgres is
fine alone for Phase 1 — skip Redis until you have a real-time
alerting need.

### 2. Reorg handling is mentioned but not designed
Block reorgs on Polygon are real (though rare). The design says
"reorg detection" as an Ingest Service responsibility but doesn't
specify how. A reorg can invalidate indexed transfers, balances,
and DEX prices.

**Suggestion:** Define the reorg strategy explicitly: how deep is
the finality window? Do you re-index the last N blocks on every
head? Do you mark indexed data as "unfinalized" until N
confirmations?

### 3. Backfill strategy is vague
"Scheduled backfill ranges" is listed but the design doesn't say
how much history to backfill, how long it takes, or how to handle
the gap between "node finishes syncing" and "indexer catches up."

**Suggestion:** Define a backfill target (e.g. last 30 days) and
an estimated time. For tracked tokens only with curated pools,
30 days of getLogs is fast on a local node.

### 4. No disk/resource budget
The design doesn't estimate how much Postgres storage the indexed
data will consume or how much RPC load the ingest workers will
put on the node.

**Suggestion:** Back-of-envelope: Polygon does ~3M tx/day. If
indexing only tracked tokens (say 20), the relevant transfer
logs are maybe 10K-50K events/day. That's ~100MB/month in
Postgres. Very manageable.

### 5. Agent tool design is under-specified
Section says "agent tool adapters" but doesn't list the actual
tools or their signatures. The agent needs concrete tools like
`get_polygon_token_holders(token, limit)` not abstract "on-chain
query surfaces."

**Suggestion:** Define the exact agent tool list for Phase 1:
- `get_polygon_balance(address, token?)`
- `get_polygon_transfers(address, token?, since?)`
- `get_polygon_token_holders(token, limit)`
- `get_polygon_dex_price(token, quote?)`
- `get_polygon_dex_volume(token, period?)`
- `get_polygon_gas()`

### 6. Monetization section is thin
The design lists premium features but doesn't connect them to the
existing custodial balance / per-call pricing model. How do
Polygon queries get priced? Same as AI queries? Free tier?

**Suggestion:** Define pricing tiers:
- Free: gas data, basic balances (cached)
- Standard: transfer history, DEX prices, holder data
- Premium: whale alerts, mempool, historical snapshots

### 7. Missing: how does this feed the strategy optimizer?
The design mentions integration with the optimizer but doesn't
specify HOW on-chain data becomes a signal the optimizer can use.
This is the most valuable integration.

**Suggestion:** Define a concrete path:
- Whale accumulation signal → NATS signal bus → optimizer event
  trigger → "whale buying X" becomes a strategy entry condition
- DEX volume spike → strategy filter condition
- Holder concentration change → risk adjustment signal

## Verdict

**Good design, ship Phase 1 as-is with the complexity reduction
(one service, not four).** The curated approach is right, the
phasing is realistic, and the architecture principles are sound.
The main risk is over-engineering the infrastructure before proving
the product value.

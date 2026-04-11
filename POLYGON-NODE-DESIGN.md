# Design: Polygon Node Integration for agent-k.ai

## Executive Summary

This design integrates the team’s locally run Polygon node into `agent-k.ai` as the primary Polygon data source for real-time and heavy-query workloads, while keeping Alchemy as a fallback and Helius unchanged for Solana.

The core recommendation is:

- Use the local Polygon node as the primary internal RPC source for Polygon.
- Put an indexer and analytics layer between the node and the public `agent-k.ai` API.
- Expose curated REST, WebSocket, and agent-tool surfaces from `kai-new-v2`, not raw node RPC.
- Ship in phases:
  - Phase 1: token transfers, balances, DEX swaps/liquidity, gas, basic alerts.
  - Phase 2: whale tracking, contract/admin event intelligence, richer reports and UI.
  - Phase 3: mempool alpha, historical state/traces, premium custom indexing.

This preserves the zero-rate-limit and zero-marginal-cost advantages of the owned node without turning the backend into a public RPC proxy or forcing agents to reason over raw chain data.

## Goals

- Make Polygon a first-class data source inside `agent-k.ai`.
- Turn raw RPC data into trading-grade analytics and alerting.
- Improve the autonomous agent, optimizer, reports, watchlists, and portfolio views with on-chain awareness.
- Use the owned node to reduce vendor spend and remove Polygon rate-limit bottlenecks.
- Keep the design practical enough to ship incrementally.

## Non-Goals

- Building a generic public Polygon block explorer.
- Exposing unrestricted raw JSON-RPC to end users.
- Indexing every contract and every holder on Polygon from day one.
- Replacing Alchemy entirely on day one.
- Making archive-grade historical tracing a Phase 1 dependency.

## Design Principles

1. The public API should expose product-level data, not raw node internals.
2. Agents should query summarized, typed data through tools, not hit raw RPC.
3. The local node is primary for Polygon, but provider fallback remains important.
4. Zero cost does not mean infinite capacity; protect the node with caches, queues, and query budgets.
5. Index only what creates trading value first: tracked wallets, tracked tokens, major pools, major event classes.

## Capability Assumptions

The design assumes a standard EVM-compatible Polygon node with HTTP and WebSocket RPC enabled. A few capabilities need explicit treatment:

- Standard full-node-safe features:
  - `eth_blockNumber`
  - `eth_getBlockByNumber`
  - `eth_getLogs`
  - `eth_getTransactionReceipt`
  - `eth_call`
  - `eth_feeHistory`
  - `eth_subscribe` for `newHeads` and `logs`
- Optional features that require namespaces or config:
  - `txpool_*`
  - `newPendingTransactions`
  - `debug_traceTransaction`
  - `trace_*`
- Long-range arbitrary historical state, traces, and storage inspection may require archive mode or provider fallback.

Practical implication:

- Phase 1 must not depend on archive-only features.
- On startup, the backend should probe the Polygon node and record a capability matrix.
- The provider router should transparently fall back to Alchemy when the local node cannot satisfy a request.

## What Data to Expose from the Polygon Node

### 1. Token Data

Expose both raw and derived token data for tracked assets and user-selected contracts.

Raw:

- ERC-20 metadata:
  - contract address
  - symbol
  - name
  - decimals
  - total supply
- Wallet balances:
  - native POL/MATIC balance
  - ERC-20 balances
  - balance at latest block
  - balance at specific block where supported
- Transfers:
  - inbound/outbound token transfers
  - mint and burn detection
  - approvals for high-risk workflows

Derived:

- top holders and concentration ratios
- holder count and holder growth
- holder net accumulation/distribution over time
- wallet inflow/outflow by token
- token velocity metrics
- transfer spikes and abnormal activity alerts

Priority:

- Phase 1: balances, transfers, top holders for tracked tokens, holder concentration.
- Phase 2: holder growth curves, wallet cohort analysis, approval-risk analytics.
- Phase 3: arbitrary historical balance snapshots and full holder history for premium plans.

### 2. DEX Data

This is the highest-value on-chain trading surface.

Raw:

- pool discovery for curated Polygon DEXs
- pool reserves / liquidity state
- swap events
- mint / burn liquidity events
- pool fee tier and quote asset mapping

Derived:

- best-liquidity on-chain price for a token
- 1m / 5m / 1h OHLCV built from swaps
- DEX volume
- buy/sell imbalance
- liquidity depth
- slippage estimates for standard trade sizes
- new liquidity pair detection
- liquidity adds/removes
- cross-pool price divergence

Recommended initial DEX scope:

- QuickSwap
- Uniswap v3 on Polygon
- Sushi if already important to the team

Practical rule:

- Start with a curated pool registry for tracked tokens and quote assets (`USDC`, `USDT`, `WETH`, `WBTC`, native gas token).
- Do not attempt universal DEX coverage in Phase 1.

### 3. Smart Contract and Governance Events

Expose contract behavior that can materially change token risk.

Raw:

- ownership transfers
- proxy upgrades
- admin changes
- pause/unpause
- role granted/revoked
- mint/burn
- fee parameter changes where known
- contract creation

Derived:

- token launch detection:
  - contract deployed
  - initial liquidity added
  - first swap activity
- governance/admin risk flags:
  - recently upgraded
  - ownership not renounced
  - pause authority present
  - mint authority present
- protocol change timeline

Priority:

- Phase 1: `OwnershipTransferred`, `Upgraded`, `AdminChanged`, mint/burn, liquidity creation.
- Phase 2: richer ABI-driven event classification and governance timelines.

### 4. Mempool Data

This is valuable but should be opt-in and phased behind operational controls.

Raw:

- pending transaction hashes
- pending transaction details
- txpool counts
- pending swaps into tracked pools
- pending large token transfers
- contract deployments in flight

Derived:

- pending whale trade alerts
- pending pool-impact estimates
- gas spike early warning
- same-wallet burst activity
- replacement/cancel behavior for large pending transactions

Important caveat:

- Mempool features depend on node support for `txpool` or pending subscriptions and can be noisy.
- This should not block Phase 1.

### 5. Historical State Queries

Useful for research, reports, and premium features.

Expose:

- token and wallet balance snapshots at past blocks
- historical holder concentration
- pool liquidity history
- swap-derived OHLCV history
- contract event timeline
- transaction traces where available

Practical boundary:

- Phase 1 should provide history from indexed transfers and swaps.
- Archive-only arbitrary historical state and traces should be Phase 3 or use Alchemy fallback until a dedicated archive node exists.

### 6. Gas and Fee Data

Expose chain execution conditions in a trading-friendly form.

Raw:

- current base fee / effective gas trend
- recent block fullness
- recent gas used
- fee percentiles from `eth_feeHistory`
- txpool pending/queued counts

Derived:

- recommended slow/normal/fast fee presets
- congestion regime labels
- gas spike alerts
- time-of-day gas heatmaps

This data is low-cost to ship and useful across the platform, so it belongs in Phase 1.

## Recommended Architecture

### High-Level Shape

Use a layered architecture:

```text
Polygon Node (private)
    |
    v
Polygon RPC Gateway (internal)
    |
    +--> Real-time Ingest Workers
    |       - heads
    |       - logs
    |       - pending tx / txpool
    |
    +--> Backfill Workers
    |       - token transfers
    |       - pool history
    |       - contract events
    |
    v
Polygon Decoder + Analytics Pipeline
    |
    +--> Postgres (normalized indexed state)
    +--> Redis (hot cache + fanout + ephemeral mempool state)
    +--> Optional ClickHouse later (heavy analytics)
    |
    v
kai-new-v2 On-Chain API
    |
    +--> REST endpoints
    +--> WebSocket channels
    +--> Agent tool adapters
    |
    v
Local daemon / web UI / TUI / report jobs / optimizer / scheduler
```

## Why Not Direct RPC from the Backend Everywhere

Do not make every backend endpoint call the node directly.

Direct RPC is acceptable for:

- chain status
- current gas
- simple latest balance reads
- ad hoc internal debugging

Direct RPC is not appropriate for:

- transfer lists
- holder rankings
- DEX analytics
- whale tracking
- watchlists querying many contracts repeatedly
- report generation
- agent queries that need summaries, comparisons, or history

Reason:

- RPC is a low-level transport, not a product data model.
- Repeated chain scans are expensive even without third-party rate limits.
- A node is not a great serving layer for list endpoints and aggregate analytics.

## Core Services

### A. Polygon RPC Gateway

Purpose:

- The only internal service allowed to talk to the node directly.
- Enforces method allowlists, connection pooling, request timeouts, and fallback policy.

Responsibilities:

- capability detection at startup
- health checks:
  - head lag
  - peer count if exposed
  - WS status
  - backlog health
- provider routing:
  - local node primary
  - Alchemy fallback for unsupported or degraded requests
- per-method budget controls

Suggested internal API:

- `rpc.call(method, params, capability="standard")`
- `rpc.subscribe(kind, params)`
- `rpc.provider_for(capability)`

### B. Polygon Ingest Service

Purpose:

- Convert live node events into durable internal data.

Inputs:

- `newHeads`
- `logs`
- `newPendingTransactions` or `txpool` polling
- scheduled backfill ranges

Responsibilities:

- block ingestion
- receipt enrichment
- reorg detection
- raw log persistence
- pending transaction capture

### C. Polygon Decoder / ABI Registry

Purpose:

- Decode raw logs and selected calldata into meaningful event types.

Responsibilities:

- maintain ABI registry for:
  - ERC-20
  - ERC-721/1155 if later needed
  - QuickSwap / Sushi / Uniswap pool contracts
  - proxy/admin event ABIs
  - governance/admin patterns
- classify events into product-level categories
- attach human labels and risk flags

### D. Polygon Analytics Service

Purpose:

- Turn decoded raw events into trading-grade analytics.

Responsibilities:

- token transfer ledger
- latest balances for tracked wallets
- holder snapshots
- whale detection
- DEX price building
- liquidity monitoring
- gas regime analysis
- watchlist and report rollups

### E. Public On-Chain API in `kai-new-v2`

Purpose:

- Serve product-ready data to the web UI, daemon, reports, and agent tools.

Responsibilities:

- authenticated REST
- authenticated WebSocket
- response envelope consistency
- caching
- billing / feature gating for premium capabilities

## Storage and Caching

### Postgres First

Use Postgres for Phase 1 and Phase 2. It is enough if the scope is curated and indexed.

Recommended tables:

- `polygon_blocks`
- `polygon_transactions`
- `polygon_logs_raw`
- `polygon_events_decoded`
- `polygon_tokens`
- `polygon_token_transfers`
- `polygon_token_balances_latest`
- `polygon_holder_snapshots`
- `polygon_wallet_labels`
- `polygon_wallet_activity_rollups`
- `polygon_pools`
- `polygon_pool_snapshots`
- `polygon_swaps`
- `polygon_price_bars`
- `polygon_contract_risk_flags`
- `polygon_mempool_transactions`
- `polygon_gas_samples`

### Redis

Use Redis for:

- hot response cache
- latest per-token / per-pool summaries
- WS fanout
- mempool TTL state
- alert dedupe
- backfill coordination locks

Examples:

- token overview cache: 15-60s TTL
- gas snapshot cache: 5-10s TTL
- mempool latest state: 30-120s TTL

### ClickHouse Later, Not Now

Add ClickHouse only if:

- public usage grows into heavy history scans
- multi-month DEX analytics become slow in Postgres
- the report system starts demanding large rollups at high concurrency

It is not required for an initial ship.

## Provider Strategy

Do not frame this as “local node or Alchemy.” Use both.

Recommended policy:

- Local Polygon node:
  - default for real-time heads/logs
  - default for tracked-token queries
  - default for gas and mempool
  - default for heavy backfills
- Alchemy:
  - fallback for unsupported namespaces
  - fallback during node lag or maintenance
  - fallback for archive-only reads until archive infrastructure exists
- Helius:
  - unchanged, Solana only

Every API response should include provenance metadata:

- `source`: `polygon-node` or `alchemy`
- `block_number`
- `observed_at`
- `finality`: `pending`, `confirmed`, or `historical`

## How Agents Should Query the Node

Agents should not use raw Polygon RPC.

Recommended pattern:

- agents call typed `agent-k.ai` on-chain endpoints through new backend clients and tools
- the local daemon continues to treat `agent-k.ai` as the upstream data source
- only backend services talk to the node

Why:

- agents need stable schemas and concise summaries
- auth, billing, and caching stay centralized
- product semantics remain consistent across TUI, web, reports, and API users

## API Design

All endpoints should follow the existing `agent-k.ai` envelope pattern:

```json
{
  "v": 1,
  "data": { ... }
}
```

## REST Endpoints

Namespace:

- `GET /v1/onchain/polygon/status`
- `GET /v1/onchain/polygon/...`

### A. Chain Status

- `GET /v1/onchain/polygon/status`
  - node health
  - sync status
  - head block
  - capability flags
  - provider fallback state

- `GET /v1/onchain/polygon/gas`
  - current gas snapshot
  - slow/normal/fast recommendations
  - recent fee history summary

- `GET /v1/onchain/polygon/gas/history?window=24h&resolution=5m`
  - gas time series

### B. Tokens

- `GET /v1/onchain/polygon/tokens/{address}`
  - metadata
  - risk flags
  - liquidity summary
  - holder summary

- `GET /v1/onchain/polygon/tokens/{address}/balances/{wallet}`
  - latest wallet balance

- `GET /v1/onchain/polygon/tokens/{address}/transfers`
  - filters:
    - `wallet`
    - `from`
    - `to`
    - `start_block`
    - `end_block`
    - `cursor`
    - `limit`

- `GET /v1/onchain/polygon/tokens/{address}/holders?limit=100`
  - top holders
  - concentration
  - labeled-wallet info where available

- `GET /v1/onchain/polygon/tokens/{address}/metrics?window=7d`
  - holder growth
  - transfer volume
  - whale netflow
  - liquidity changes

### C. Wallets

- `GET /v1/onchain/polygon/wallets/{address}`
  - high-level wallet summary

- `GET /v1/onchain/polygon/wallets/{address}/holdings`
  - tracked holdings

- `GET /v1/onchain/polygon/wallets/{address}/activity?window=7d`
  - transfers
  - swaps
  - contract interactions

- `GET /v1/onchain/polygon/wallets/{address}/netflows?token={address}&window=30d`
  - token-specific inflow/outflow

### D. DEX / Market Structure

- `GET /v1/onchain/polygon/pools?token={address}`
  - pools containing the token
  - liquidity ranking

- `GET /v1/onchain/polygon/pools/{pool_address}`
  - pool metadata and latest state

- `GET /v1/onchain/polygon/pools/{pool_address}/swaps?window=24h`
  - recent swaps

- `GET /v1/onchain/polygon/prices/{token_address}?resolution=1m&window=7d`
  - swap-derived OHLCV
  - source pool metadata

- `GET /v1/onchain/polygon/quotes/sell`
  - inputs:
    - `token_in`
    - `token_out`
    - `amount_in`
  - returns:
    - best pool/path among indexed venues
    - estimated output
    - slippage estimate

### E. Contracts and Governance

- `GET /v1/onchain/polygon/contracts/{address}`
  - contract type
  - deployer
  - implementation/proxy info if known
  - risk flags

- `GET /v1/onchain/polygon/contracts/{address}/events?types=upgrade,ownership,pause&window=30d`
  - contract/admin event timeline

- `GET /v1/onchain/polygon/contracts/{address}/risk`
  - simple risk profile for agents and UI badges

### F. Mempool

Phase 3 or premium:

- `GET /v1/onchain/polygon/mempool/stats`
- `GET /v1/onchain/polygon/mempool/pending-swaps?token={address}`
- `GET /v1/onchain/polygon/mempool/pending-transfers?token={address}&min_usd=100000`

### G. Historical State

Phase 3 or premium:

- `GET /v1/onchain/polygon/historical/balance?wallet={address}&token={address}&block=...`
- `GET /v1/onchain/polygon/historical/pool-state?pool={address}&block=...`
- `GET /v1/onchain/polygon/historical/trace/{tx_hash}`

## WebSocket Channels

Reuse the existing `agent-k.ai` WebSocket pattern: subscribe to named channels and receive snapshots plus incremental events.

Suggested channels:

- `polygon.heads`
- `polygon.gas`
- `polygon.token.{token_address}.transfers`
- `polygon.token.{token_address}.alerts`
- `polygon.wallet.{wallet_address}.activity`
- `polygon.pool.{pool_address}.swaps`
- `polygon.pool.{pool_address}.liquidity`
- `polygon.whales.{token_address}`
- `polygon.contract.{address}.events`
- `polygon.mempool.{scope}`

Suggested envelope:

```json
{
  "type": "event",
  "channel": "polygon.pool.0xabc...swaps",
  "data": {
    "block_number": 123,
    "tx_hash": "0x...",
    "kind": "swap",
    "price_usd": 1.02,
    "volume_usd": 54211.3,
    "side": "buy"
  }
}
```

For UI safety, only expose curated channels. Do not expose raw subscription passthrough to arbitrary node topics.

## Agent Tools

Add typed agent tools, not one generic “polygon_rpc” tool.

Recommended first set:

- `polygon_token_summary`
  - inputs: `token`, `window`
  - returns: concise token overview with liquidity, holders, flows, and recent contract-risk events

- `polygon_wallet_activity`
  - inputs: `wallet`, `window`, optional `token`
  - returns: transfers, swaps, netflows, noteworthy counterparties

- `polygon_pool_snapshot`
  - inputs: `pool` or `token`
  - returns: liquidity, recent swap imbalance, price, slippage summary

- `polygon_contract_risk`
  - inputs: `contract`
  - returns: ownership, upgradeability, recent admin events, mint/pause powers

- `polygon_whale_alerts`
  - inputs: `token`, `window`, `min_usd`
  - returns: large wallet movements and accumulation/distribution summary

- `polygon_gas_status`
  - inputs: optional `window`
  - returns: current and recent fee regime

Later:

- `polygon_mempool_watch`
- `polygon_historical_state`

Tool output format should mirror existing agent tools:

- concise natural-language summary first
- structured JSON payload optional for advanced clients

## Data Pipeline

### Ingest Flow

### 1. Head Tracker

Subscribe to `newHeads`.

For each head:

- persist block metadata
- fetch full block / receipts
- update head cache
- trigger downstream block processors

### 2. Log Ingest

Use two paths:

- live:
  - `eth_subscribe("logs", ...)` for indexed contracts/topics
- backfill:
  - `eth_getLogs` in block ranges for recovery and initial loads

Store raw logs first, decode second.

This separation matters because:

- ABI coverage will evolve over time
- raw data should survive decoder bugs
- reprocessing is easier

### 3. Pending Transaction Ingest

If supported:

- subscribe to pending transactions or poll txpool
- fetch pending tx details
- filter to tracked pools/tokens/wallets
- keep only short-lived mempool state in Redis plus a limited persistent audit trail

### 4. Backfill Workers

Backfills should be targeted, not global.

Good initial backfills:

- tracked token transfer history
- tracked pool swap history
- tracked contract admin-event history
- tracked whale wallet activity

Avoid:

- indexing the entire chain indiscriminately in Phase 1

## Decode and Normalization

### Event Classification

Build a classifier that turns raw logs into normalized internal event types:

- `token.transfer`
- `token.mint`
- `token.burn`
- `dex.swap`
- `dex.liquidity_add`
- `dex.liquidity_remove`
- `contract.upgrade`
- `contract.ownership_transferred`
- `contract.pause_changed`
- `wallet.large_transfer`

Each normalized event should include:

- `chain`
- `block_number`
- `block_time`
- `tx_hash`
- `log_index`
- `contract_address`
- `event_type`
- `decoded_payload`
- `confidence`
- `source`

### Token Balance Snapshots

Do not recompute balances from chain state for every API call.

Recommended approach:

- maintain `latest balances` for tracked wallets/tokens from indexed transfer events
- validate periodically with direct `balanceOf` reads
- generate scheduled snapshots:
  - hourly for tracked wallets
  - daily for token holder analytics

This gives cheap serving for:

- holdings views
- whale tracking
- historical charts

### Whale Tracking

Whale logic should be deterministic.

Inputs:

- labeled wallets
- top holder sets
- transfer values
- DEX trade sizes

Signals:

- large transfer above USD threshold
- net accumulation over rolling window
- distribution from top holders
- smart-money watchlist movement

Outputs:

- alert events
- rollup metrics
- watchlist badges
- strategy features

### DEX Price Feed Construction

Build on-chain price bars from swap events.

Recommended method:

1. Maintain a curated pool registry per tracked token.
2. Rank pools by recent executable liquidity.
3. Convert swap price into a common USD quote via `USDC`/`USDT`/`WETH` reference pools.
4. Aggregate into 1m bars.
5. Build higher intervals from 1m bars.

For each bar store:

- open
- high
- low
- close
- swap volume
- pool source
- confidence / liquidity score

This is the on-chain equivalent of the existing OHLCV market endpoints and can feed reports, alerts, and optimizer features.

### On-Chain Activity Metrics

Compute rollups per token, pool, and wallet:

- tx count
- unique traders
- volume
- net inflow/outflow
- liquidity delta
- new holder count
- active holder count
- top-holder concentration change

Suggested rollup windows:

- 5m
- 1h
- 24h
- 7d

### Reorg Handling

Polygon reorg risk is low but non-zero. The pipeline must be reorg-aware.

Recommended model:

- store block hash for all ingested records
- mark events as:
  - `pending`
  - `confirmed`
  - `finalized`
- use a configurable confirmation depth
- on hash mismatch, roll back affected ranges and replay

UI and agent behavior:

- live alerts can use `pending` or `confirmed`
- reports and optimizer features should use `confirmed` or stronger

## Integration with Existing Features

### 1. Strategy Optimizer

The current optimizer is OHLCV-centric. Polygon integration should add deterministic on-chain feature feeds, not raw RPC inside the optimizer loop.

Recommended additions:

- extend the optimizer data layer with an `OnChainFeatureFetcher`
- expose a bounded feature catalog such as:
  - whale net accumulation
  - holder growth
  - liquidity growth
  - swap imbalance
  - DEX volume spike
  - gas regime
  - recent contract upgrade flag

Use cases:

- regime filters:
  - only trade when liquidity is above threshold
  - avoid strategies during contract-upgrade windows
  - require positive whale accumulation
- ranking features:
  - compare otherwise similar setups by on-chain strength
- report explanations:
  - why a candidate was promoted or rejected

Implementation note:

- keep feature generation deterministic and versioned
- do not let the LLM invent arbitrary on-chain feature semantics inside the optimizer

### 2. Autonomous Agent

Polygon data should make the agent more aware, not more reckless.

Useful enhancements:

- pre-trade contract-risk check before discussing low-cap tokens
- alert when tracked tokens receive large inflows/outflows
- detect sudden liquidity removal or major sell pressure
- warn about congestion and fee spikes
- surface pending large swaps for monitored pools when mempool support exists

Agent interaction model:

- the main agent or onchain sub-agent calls backend tools
- alerts can also arrive via scheduler/event hooks
- results are summarized into human-readable chain context

### 3. Scheduler

Owned-node data is a strong fit for event-driven scheduled jobs.

Examples:

- “When a tracked whale wallet buys more than $250k of TOKEN, wake the onchain agent”
- “Every hour summarize holder and liquidity changes for watchlist tokens”
- “If gas drops below threshold, trigger batch historical backfills”

Implementation:

- emit normalized on-chain alert events into the existing daemon event path
- add channel types parallel to existing signals and market events

### 4. Report Generator

Add standard Polygon sections to daily/weekly reports:

- top token inflows/outflows
- whale accumulation/distribution summary
- DEX liquidity changes
- major contract upgrades / ownership changes
- gas regime summary
- mempool risk summary for premium reports

This is one of the fastest ways to make the integration visible to users.

### 5. Watchlist and Portfolio Views

Enhance watchlist rows with:

- on-chain liquidity score
- holder growth %
- whale flow badge
- contract risk badge
- DEX volume trend

Enhance portfolio views with:

- wallet-level on-chain holdings for connected/tracked wallets
- concentration by contract address
- contract/admin risk warnings
- recent liquidity deterioration alerts

For the local daemon/TUI/web UI model, this should surface as additional fields on existing watchlist/portfolio endpoints rather than a separate isolated product.

## Monetization

The owned node creates a real product advantage if the platform packages it correctly.

### Free / Standard Tier

- basic Polygon token summaries
- current gas data
- latest transfers for tracked wallets/tokens
- basic DEX price charts for indexed tokens

### Pro Tier

- whale alerts
- top-holder analytics
- holder growth history
- liquidity change alerts
- contract-risk monitoring
- deeper history windows

### Premium / Alpha Tier

- mempool alerts for tracked pools/tokens
- custom indexed wallets
- custom indexed tokens and pools
- historical balance snapshots
- event-driven webhooks
- high-frequency WebSocket channels
- export/API access to advanced analytics

### Enterprise / Power User

- dedicated indexing for customer-selected contracts
- bulk historical exports
- SLA-backed Polygon analytics API
- custom dashboards and alerting rules

### Why Users Will Pay

- zero-rate-limit user experience on Polygon-heavy workflows
- richer historical views than commodity free APIs
- faster and more reliable real-time alerts
- custom indexing that generic providers do not ship
- mempool and whale intelligence packaged for trading decisions

## Phased Rollout

### Phase 0: Foundation

Ship:

- Polygon RPC gateway
- capability probe
- node health endpoint
- provider fallback policy
- internal metrics and alarms

Success criteria:

- backend can reliably prefer local node and fail over to Alchemy
- operators can see node lag and capability state

### Phase 1: Useful Core

Ship:

- token metadata / balances / transfers
- curated DEX pool indexing
- swap-derived price bars
- gas endpoint
- basic contract/admin event flags
- REST + WS + agent tools for token summary, wallet activity, pool snapshot, gas
- watchlist enrichment with liquidity and flow data

Do not ship yet:

- arbitrary historical state
- full mempool analytics
- generic explorer search

Success criteria:

- watchlist/report surfaces materially improved
- onchain agent can answer useful Polygon questions without web scraping
- no meaningful reliance on Alchemy for standard real-time tracked queries

### Phase 2: Intelligence Layer

Ship:

- whale tracking
- holder snapshots and growth analytics
- richer contract risk profiles
- scheduler-triggered on-chain alerts
- report generator sections
- optimizer feature feed

Success criteria:

- on-chain signals become part of optimizer inputs and daily reports
- whale and liquidity alerts drive user-visible value

### Phase 3: Premium Alpha

Ship:

- mempool ingestion and alerts
- historical balance snapshots at specific blocks
- traces / archive-assisted research endpoints
- custom indexing and webhooks

Dependencies:

- either archive-capable local infra or clean Alchemy fallback for archive-only calls
- stronger cache and serving controls

Success criteria:

- premium plan differentiation is obvious
- mempool and historical research workloads do not destabilize core serving

## Operational Considerations

### Security

- never expose the raw node publicly
- keep node RPC on a private network
- allow only the RPC gateway to reach the node
- use endpoint-level auth and plan gating in `agent-k.ai`
- avoid exposing raw `debug` and `trace` namespaces to public users

### Reliability

Track:

- head lag
- WS disconnect rate
- backfill queue depth
- decode failure rate
- cache hit rate
- provider fallback rate
- node RPC latency by method

Set degrade behavior:

- if node lag exceeds threshold, temporarily route supported reads to Alchemy
- if WS is down, keep REST working and backfill gaps

### Capacity Management

Zero vendor cost does not remove node bottlenecks.

Controls:

- concurrency limits per RPC method class
- chunked backfills
- track-first indexing
- cache everything that is repeatedly requested
- rate limits on expensive public history endpoints

If usage grows:

- add a read replica or separate archive node for research workloads

## Suggested Implementation Areas in `kai-new-v2`

Illustrative structure:

```text
backend/app/services/onchain/polygon/
  rpc_gateway.py
  capability_probe.py
  ingest_blocks.py
  ingest_logs.py
  ingest_mempool.py
  decoder.py
  abi_registry.py
  analytics_tokens.py
  analytics_dex.py
  analytics_wallets.py
  analytics_contracts.py
  analytics_gas.py
  provider_router.py

backend/app/routers/v1_onchain_polygon.py
backend/app/ws/onchain_polygon.py
backend/app/models/onchain_polygon.py
backend/app/repos/onchain_polygon.py
```

For the local repo, the integration point is straightforward:

- extend the `agent-k.ai` client with Polygon on-chain endpoint helpers
- add typed agent tools
- enrich daemon watchlist/portfolio/report surfaces with the new backend data

## Final Recommendation

Build this as a curated Polygon intelligence layer inside `kai-new-v2`, backed by the local node but insulated by an RPC gateway, indexer, and analytics service.

The correct architecture is:

- not direct raw RPC from every product surface
- not a giant universal chain indexer on day one
- not “replace Alchemy entirely”

It is:

- local node as primary Polygon infra
- indexed product-ready summaries exposed through `agent-k.ai`
- fallback-aware capability routing
- phased delivery focused first on token flows, DEX prices/liquidity, gas, and contract risk

That plan is both shippable and strategically valuable. It gives the platform immediate product leverage from the owned node while creating a clear path to premium features like whale tracking, mempool alpha, and custom historical indexing.

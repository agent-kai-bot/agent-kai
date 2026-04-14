# Dashboard v4 — Final Freeze (resolving ALL ambiguities)

## Resolved: Shell vs MVP mismatch

### VolumeChart: OUT of v1. Removed from shell.
### WhaleAlertRail: renamed. It IS the HeroWhaleFeed in the center.
### GasArc: lives in the right rail, below SystemSummary.

### Frozen shell (1920px):
```
Row 1: ChainPulseBar (full width, 56px, sticky)
Row 2: BlockTape (full width, 36px, sticky below pulse)
Row 3: 3-column body
  Left (300px):  TokenRail
  Center (1fr):  HeroWhaleFeed (100% of center column)
  Right (340px): SystemSummary (top, auto height)
                 GasArc (below summary, 200px height)
                 [remaining space empty in v1]
```

No VolumeChart. No separate WhaleAlertRail. One whale
surface = HeroWhaleFeed in the center column.

### <1280px vertical stack order:
1. ChainPulseBar (sticky)
2. BlockTape
3. HeroWhaleFeed (takes priority on mobile)
4. TokenRail (horizontal scroll cards)
5. SystemSummary
6. GasArc
Each section scrolls with the page. No independent scrolling
at mobile widths.

## Resolved: SSE events

v1 SSE includes ALL THREE: `head`, `whale`, `status`.
The earlier contradiction is resolved — status is included.

## Resolved: Missing backend fields

### /v1/polygon/overview — add these fields:
```json
{
  "data": {
    ...existing...,
    "total_transfers_indexed": 26000000,
    "last_updated_at": "2026-04-14T...",
    "gas_percentile_rank": 0.45,
    "gas_history_100_blocks": [28.1, 30.2, 32.5, ...]
  }
}
```

`gas_history_100_blocks`: array of last 100 base_fee_gwei values.
`gas_percentile_rank`: where current gas sits in 0.0-1.0 range
relative to the 100-block window. Powers the GasArc color.
`total_transfers_indexed`: powers SystemSummary count.
`last_updated_at`: powers SystemSummary timestamp.

### whale-transfers and SSE whale — add usd_value:
```json
{
  "usd_value": 45230.0
}
```

Computed as: amount_human * latest_price from the token's
cached quote. If no price available, `usd_value: null` and
the feed row shows raw amount without USD tier styling.

### /v1/polygon/blocks/recent — frozen response:
```json
{
  "ok": true,
  "data": [
    {
      "block_number": 85467231,
      "timestamp": "2026-04-14T...",
      "tx_count": 87,
      "transfer_count": 23,
      "swap_count": 4,
      "gas_used_pct": 72.3
    }
  ]
}
```

Sorted by block_number DESC. Default limit=40.

## Resolved: Drawer contracts

### On drawer open for token X, fetch:

**1. Token detail** (existing: GET /v1/polygon/tokens/{address})
Response (already available):
```json
{
  "ok": true,
  "data": {
    "contract_address": "0x3c499c...",
    "symbol": "USDC",
    "name": "USD Coin",
    "decimals": 6,
    "transfers_24h": 142000,
    "latest_price": null,
    "total_holders": 42000,
    "top10_concentration_pct": 62.3,
    "top50_concentration_pct": 81.2,
    "gini_coefficient": 0.87
  }
}
```

**2. Top holders** (existing: GET /v1/polygon/tokens/{address}/holders?limit=10)
Response:
```json
{
  "ok": true,
  "data": {
    "holders": [
      {
        "wallet_address": "0xab..cd",
        "balance": "15000000000000",
        "balance_human": "15000000.0",
        "pct_of_tracked": 12.3
      }
    ]
  }
}
```

`pct_of_tracked`: balance as percentage of SUM of all tracked
balances for this token (not total supply, since we only track
indexed balances).

**3. Recent transfers** (existing: GET /v1/polygon/tokens/{address}/transfers?limit=20&since=...)

Already returns: block_number, tx_hash, from_address, to_address,
value, timestamp. Drawer will use token metadata from call #1
to format the value with correct decimals.

## Resolved: Whale feed bootstrap

On page load, fetch `GET /v1/polygon/whale-transfers?limit=30`
sorted by timestamp DESC. This populates the feed. Then SSE
`whale` events append to the top.

## Resolved: TokenRail sort/selection

If >5 tracked tokens: sort by `transfers_24h` DESC, show all.
TokenRail scrolls vertically within its column to accommodate
any number of tokens.

## Resolved: Typography per component

| Component | Font | Size | Weight |
|---|---|---|---|
| ChainPulseBar labels | Plex Sans | 11px | 600 |
| ChainPulseBar values | Plex Mono | 14px | 500 |
| BlockTape tooltip | Plex Mono | 12px | 400 |
| TokenRailItem symbol | Space Grotesk | 15px | 600 |
| TokenRailItem name | Plex Sans | 12px | 400 |
| TokenRailItem metrics | Plex Mono | 13px | 500 |
| HeroWhaleFeed amount | Plex Mono | 14px | 600 |
| HeroWhaleFeed addresses | Plex Mono | 12px | 400 |
| HeroWhaleFeed time | Plex Sans | 12px | 400 |
| SystemSummary labels | Plex Sans | 12px | 500 |
| SystemSummary values | Plex Mono | 14px | 600 |
| GasArc center value | Space Grotesk | 28px | 600 |
| GasArc subtitle | Plex Sans | 12px | 400 |
| Drawer section title | Space Grotesk | 14px | 600 |
| Drawer body text | Plex Sans | 13px | 400 |
| Drawer holder balance | Plex Mono | 13px | 500 |

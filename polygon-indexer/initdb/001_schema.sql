-- Polygon Indexer Schema — Phase 1
-- Curated indexing for tracked tokens + DEX pools

-- Blocks (lightweight, for reorg detection)
CREATE TABLE IF NOT EXISTS polygon_blocks (
    block_number BIGINT PRIMARY KEY,
    block_hash TEXT NOT NULL,
    parent_hash TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    tx_count INTEGER NOT NULL DEFAULT 0,
    gas_used BIGINT,
    base_fee_per_gas BIGINT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_blocks_timestamp ON polygon_blocks(timestamp);

-- Tracked tokens
CREATE TABLE IF NOT EXISTS polygon_tokens (
    contract_address TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT,
    decimals INTEGER NOT NULL DEFAULT 18,
    total_supply NUMERIC,
    is_tracked BOOLEAN DEFAULT true,
    first_seen_block BIGINT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Token transfers (only for tracked tokens)
CREATE TABLE IF NOT EXISTS polygon_token_transfers (
    id BIGSERIAL PRIMARY KEY,
    block_number BIGINT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    contract_address TEXT NOT NULL REFERENCES polygon_tokens(contract_address),
    from_address TEXT NOT NULL,
    to_address TEXT NOT NULL,
    value NUMERIC NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(tx_hash, log_index)
);

CREATE INDEX idx_transfers_contract ON polygon_token_transfers(contract_address, block_number);
CREATE INDEX idx_transfers_from ON polygon_token_transfers(from_address, timestamp);
CREATE INDEX idx_transfers_to ON polygon_token_transfers(to_address, timestamp);
CREATE INDEX idx_transfers_block ON polygon_token_transfers(block_number);

-- Latest token balances (materialized, updated by analytics)
CREATE TABLE IF NOT EXISTS polygon_token_balances (
    wallet_address TEXT NOT NULL,
    contract_address TEXT NOT NULL REFERENCES polygon_tokens(contract_address),
    balance NUMERIC NOT NULL DEFAULT 0,
    last_updated_block BIGINT NOT NULL,
    last_updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (wallet_address, contract_address)
);

CREATE INDEX idx_balances_contract ON polygon_token_balances(contract_address, balance DESC);

-- Holder snapshots (daily, for tracking concentration over time)
CREATE TABLE IF NOT EXISTS polygon_holder_snapshots (
    id BIGSERIAL PRIMARY KEY,
    contract_address TEXT NOT NULL REFERENCES polygon_tokens(contract_address),
    snapshot_date DATE NOT NULL,
    total_holders INTEGER NOT NULL,
    top10_concentration NUMERIC,  -- % held by top 10
    top50_concentration NUMERIC,  -- % held by top 50
    gini_coefficient NUMERIC,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(contract_address, snapshot_date)
);

-- DEX pools (curated — QuickSwap, Uniswap V3)
CREATE TABLE IF NOT EXISTS polygon_dex_pools (
    pool_address TEXT PRIMARY KEY,
    dex_name TEXT NOT NULL,  -- 'quickswap', 'uniswap_v3'
    token0_address TEXT NOT NULL,
    token1_address TEXT NOT NULL,
    fee_tier INTEGER,  -- basis points for V3
    created_at_block BIGINT,
    is_tracked BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- DEX swaps (only for tracked pools)
CREATE TABLE IF NOT EXISTS polygon_dex_swaps (
    id BIGSERIAL PRIMARY KEY,
    block_number BIGINT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    pool_address TEXT NOT NULL REFERENCES polygon_dex_pools(pool_address),
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    amount0 NUMERIC NOT NULL,
    amount1 NUMERIC NOT NULL,
    sqrt_price_x96 NUMERIC,
    liquidity NUMERIC,
    tick INTEGER,
    timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(tx_hash, log_index)
);

CREATE INDEX idx_swaps_pool ON polygon_dex_swaps(pool_address, timestamp);
CREATE INDEX idx_swaps_block ON polygon_dex_swaps(block_number);

-- DEX OHLCV (built from swaps by analytics service)
CREATE TABLE IF NOT EXISTS polygon_dex_ohlcv (
    id BIGSERIAL PRIMARY KEY,
    pool_address TEXT NOT NULL REFERENCES polygon_dex_pools(pool_address),
    interval TEXT NOT NULL,  -- '1m', '5m', '15m', '1h', '4h', '1d'
    open_time TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    trade_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(pool_address, interval, open_time)
);

CREATE INDEX idx_ohlcv_pool_interval ON polygon_dex_ohlcv(pool_address, interval, open_time);

-- Contract events (governance, ownership, upgrades)
CREATE TABLE IF NOT EXISTS polygon_contract_events (
    id BIGSERIAL PRIMARY KEY,
    block_number BIGINT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    contract_address TEXT NOT NULL,
    event_type TEXT NOT NULL,  -- 'ownership_transferred', 'upgraded', 'paused', etc.
    event_data JSONB,
    timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(tx_hash, log_index)
);

CREATE INDEX idx_events_contract ON polygon_contract_events(contract_address, timestamp);
CREATE INDEX idx_events_type ON polygon_contract_events(event_type, timestamp);

-- Gas metrics (sampled every block)
CREATE TABLE IF NOT EXISTS polygon_gas_metrics (
    block_number BIGINT PRIMARY KEY,
    base_fee_gwei NUMERIC,
    gas_used_pct NUMERIC,  -- block fullness
    tx_count INTEGER,
    timestamp TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_gas_timestamp ON polygon_gas_metrics(timestamp);

-- Indexer state (cursor tracking for restart/resume)
CREATE TABLE IF NOT EXISTS polygon_indexer_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Insert initial state
INSERT INTO polygon_indexer_state (key, value)
VALUES
    ('last_indexed_block', '0'),
    ('last_decoded_block', '0'),
    ('last_analytics_block', '0'),
    ('backfill_start_block', '0'),
    ('backfill_complete', 'false')
ON CONFLICT (key) DO NOTHING;

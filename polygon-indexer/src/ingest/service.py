from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from eth_abi import decode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.shared.config import Settings, load_settings
from src.shared.db import Database, StateStore
from src.shared.events import DEX_SWAP_TOPIC_FILTER, NEW_BLOCK_CHANNEL, NEW_SWAPS_CHANNEL, NEW_TRANSFERS_CHANNEL, REORG_CHANNEL, TRANSFER_TOPIC, V2_SWAP_TOPIC, V3_SWAP_TOPIC
from src.shared.evm import decode_topic_address, from_hex_quantity, normalize_address, to_hex_quantity
from src.shared.logging import configure_logging
from src.shared.redis import RedisClient
from src.shared.rpc import RpcGatewayClient, build_logs_filter, fetch_block_number
from src.shared.utils import chunk_range, to_iso8601, utcnow

LOGGER = logging.getLogger(__name__)
BLOCK_CACHE_MAX_SIZE = 2_048
BLOCK_CACHE_EVICT_COUNT = 256


@dataclass(slots=True)
class PoolRecord:
    pool_address: str
    dex_name: str
    token0_address: str
    token1_address: str
    fee_tier: int | None


class IngestService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings.database_url)
        self.state = StateStore(self.database)
        self.redis = RedisClient(settings.redis_url)
        self.rpc = RpcGatewayClient(settings.rpc_gateway_url, timeout=settings.request_timeout_seconds)
        self.tracked_tokens: list[str] = []
        self.tracked_pools: list[PoolRecord] = []
        self.block_cache: dict[int, dict[str, Any]] = {}
        self._running = True

    def request_shutdown(self) -> None:
        self._running = False

    async def close(self) -> None:
        await self.rpc.close()
        await self.redis.close()
        await self.database.dispose()

    async def run(self) -> None:
        await self.initialize()
        while self._running:
            try:
                await self.process_live_heads()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("head tracking failed, retrying: %s", exc)
                await asyncio.sleep(3)

    async def initialize(self) -> None:
        await self.ensure_tracked_tokens()
        await self.refresh_tracking_sets()
        current_head = await fetch_block_number(self.rpc)
        backfill_complete = await self.state.get_bool("backfill_complete", False)
        if not backfill_complete:
            await self.run_backfill(current_head)
        await self.seed_recent_blocks(current_head)
        last_indexed = await self.state.get_int("last_indexed_block", 0)
        if current_head > last_indexed:
            await self.catch_up_blocks(last_indexed + 1, current_head)

    async def ensure_tracked_tokens(self) -> None:
        if not self.settings.tracked_tokens:
            return
        rows = [
            {
                "contract_address": address,
                "symbol": address[:10],
                "name": None,
                "decimals": 18,
                "first_seen_block": None,
            }
            for address in self.settings.tracked_tokens
        ]
        async with self.database.connection() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO polygon_tokens (
                        contract_address,
                        symbol,
                        name,
                        decimals,
                        total_supply,
                        is_tracked,
                        first_seen_block
                    )
                    VALUES (
                        :contract_address,
                        :symbol,
                        :name,
                        :decimals,
                        NULL,
                        true,
                        :first_seen_block
                    )
                    ON CONFLICT (contract_address) DO UPDATE
                    SET is_tracked = true
                    """
                ),
                rows,
            )

    async def refresh_tracking_sets(self) -> None:
        async with self.database.connection() as connection:
            token_rows = (
                await connection.execute(
                    text("SELECT contract_address FROM polygon_tokens WHERE is_tracked = true ORDER BY contract_address")
                )
            ).mappings().all()
            pool_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT pool_address, dex_name, token0_address, token1_address, fee_tier
                        FROM polygon_dex_pools
                        WHERE is_tracked = true
                        ORDER BY pool_address
                        """
                    )
                )
            ).mappings().all()
        self.tracked_tokens = [normalize_address(row["contract_address"]) for row in token_rows]
        self.tracked_pools = [
            PoolRecord(
                pool_address=normalize_address(row["pool_address"]),
                dex_name=row["dex_name"],
                token0_address=normalize_address(row["token0_address"]),
                token1_address=normalize_address(row["token1_address"]),
                fee_tier=row["fee_tier"],
            )
            for row in pool_rows
        ]

    async def process_live_heads(self) -> None:
        async for head in self.rpc.subscribe_heads():
            if not self._running:
                break
            head_number = from_hex_quantity(head.get("number"))
            last_indexed = await self.state.get_int("last_indexed_block", 0)
            if head_number <= last_indexed:
                continue
            await self.catch_up_blocks(last_indexed + 1, head_number)

    async def run_backfill(self, current_head: int) -> None:
        await self.refresh_tracking_sets()
        if not self.tracked_tokens and not self.tracked_pools:
            await self.state.set_int("backfill_start_block", current_head)
            await self.state.set_bool("backfill_complete", True)
            return

        target_timestamp = int((utcnow() - timedelta(days=self.settings.backfill_days)).timestamp())
        start_block = await self.find_block_for_timestamp(target_timestamp, current_head)
        await self.state.set_int("backfill_start_block", start_block)
        LOGGER.info("starting backfill from block %s to %s", start_block, current_head)

        if self.tracked_tokens:
            await self.backfill_transfers(start_block, current_head)
        if self.tracked_pools:
            await self.backfill_swaps(start_block, current_head)

        await self.seed_recent_blocks(current_head)
        await self.state.set_int("last_indexed_block", current_head)
        await self.state.set_bool("backfill_complete", True)
        LOGGER.info("backfill complete")

    async def seed_recent_blocks(self, current_head: int) -> None:
        start = max(1, current_head - self.settings.reorg_depth + 1)
        for block_number in range(start, current_head + 1):
            await self.store_block_only(block_number)

    async def find_block_for_timestamp(self, target_timestamp: int, high_block: int) -> int:
        low = 1
        high = high_block
        best = high
        while low <= high:
            mid = (low + high) // 2
            block = await self.fetch_block(mid)
            block_timestamp = from_hex_quantity(block["timestamp"])
            if block_timestamp >= target_timestamp:
                best = mid
                high = mid - 1
            else:
                low = mid + 1
        return best

    async def backfill_transfers(self, start_block: int, end_block: int) -> None:
        for chunk_start, chunk_end in chunk_range(start_block, end_block, self.settings.log_range_limit):
            logs = await self.rpc.call(
                "eth_getLogs",
                [
                    build_logs_filter(
                        from_block=chunk_start,
                        to_block=chunk_end,
                        addresses=self.tracked_tokens,
                        topics=[TRANSFER_TOPIC],
                    )
                ],
            )
            await self.persist_transfer_logs(logs)

    async def backfill_swaps(self, start_block: int, end_block: int) -> None:
        addresses = [pool.pool_address for pool in self.tracked_pools]
        for chunk_start, chunk_end in chunk_range(start_block, end_block, self.settings.log_range_limit):
            logs = await self.rpc.call(
                "eth_getLogs",
                [
                    build_logs_filter(
                        from_block=chunk_start,
                        to_block=chunk_end,
                        addresses=addresses,
                        topics=DEX_SWAP_TOPIC_FILTER,
                    )
                ],
            )
            await self.persist_swap_logs(logs)

    async def catch_up_blocks(self, start_block: int, end_block: int) -> None:
        current = start_block
        while current <= end_block:
            reorg_from = await self.process_block(current)
            if reorg_from:
                current = reorg_from
                continue
            current += 1

    async def process_block(self, block_number: int) -> int | None:
        block = await self.fetch_block(block_number)
        reorg_from = await self.verify_parent(block_number, block)
        if reorg_from is not None:
            return reorg_from

        transfer_logs = await self.fetch_transfer_logs_for_block(block_number)
        swap_logs = await self.fetch_swap_logs_for_block(block_number)
        async with self.database.connect() as connection:
            async with connection.begin():
                await self.insert_block(block_number, block, connection=connection)
                transfer_count = await self.persist_transfer_logs(transfer_logs, connection=connection)
                swap_count = await self.persist_swap_logs(swap_logs, connection=connection)
                await self._set_state_ints(connection, {"last_indexed_block": block_number})

        block_timestamp = datetime.fromtimestamp(from_hex_quantity(block["timestamp"]), tz=UTC)
        await self.redis.publish_json(
            NEW_BLOCK_CHANNEL,
            {
                "block_number": block_number,
                "block_hash": block["hash"],
                "parent_hash": block["parentHash"],
                "timestamp": to_iso8601(block_timestamp),
                "transfer_count": transfer_count,
                "swap_count": swap_count,
            },
        )
        if transfer_count:
            await self.redis.publish_json(
                NEW_TRANSFERS_CHANNEL,
                {
                    "block_number": block_number,
                    "count": transfer_count,
                },
            )
        if swap_count:
            await self.redis.publish_json(
                NEW_SWAPS_CHANNEL,
                {
                    "block_number": block_number,
                    "count": swap_count,
                },
            )
        return None

    async def verify_parent(self, block_number: int, block: dict[str, Any]) -> int | None:
        if block_number <= 1:
            return None
        async with self.database.connection() as connection:
            previous = (
                await connection.execute(
                    text("SELECT block_hash FROM polygon_blocks WHERE block_number = :block_number"),
                    {"block_number": block_number - 1},
                )
            ).scalar_one_or_none()
        if previous and previous != block["parentHash"]:
            rollback_from = max(1, block_number - self.settings.reorg_depth)
            LOGGER.warning("reorg detected at block %s, rolling back to %s", block_number, rollback_from)
            await self.rollback_from_block(rollback_from)
            return rollback_from
        return None

    async def rollback_from_block(self, rollback_from: int) -> None:
        reset_block = rollback_from - 1
        async with self.database.connect() as connection:
            async with connection.begin():
                earliest_timestamp = (
                    await connection.execute(
                        text("SELECT min(timestamp) FROM polygon_blocks WHERE block_number >= :rollback_from"),
                        {"rollback_from": rollback_from},
                    )
                ).scalar_one_or_none()
                delete_statements = [
                    "DELETE FROM polygon_token_transfers WHERE block_number >= :rollback_from",
                    "DELETE FROM polygon_dex_swaps WHERE block_number >= :rollback_from",
                    "DELETE FROM polygon_contract_events WHERE block_number >= :rollback_from",
                    "DELETE FROM polygon_gas_metrics WHERE block_number >= :rollback_from",
                    "DELETE FROM polygon_blocks WHERE block_number >= :rollback_from",
                    "DELETE FROM polygon_token_balances WHERE last_updated_block >= :rollback_from",
                ]
                for statement in delete_statements:
                    await connection.execute(text(statement), {"rollback_from": rollback_from})
                if earliest_timestamp is not None:
                    await connection.execute(
                        text("DELETE FROM polygon_dex_ohlcv WHERE open_time >= :timestamp"),
                        {"timestamp": earliest_timestamp - timedelta(days=1)},
                    )
                    await connection.execute(
                        text("DELETE FROM polygon_holder_snapshots WHERE snapshot_date >= :snapshot_date"),
                        {"snapshot_date": earliest_timestamp.date()},
                    )
                await self._set_state_ints(
                    connection,
                    {
                        "last_indexed_block": reset_block,
                        "last_decoded_block": reset_block,
                        "last_analytics_block": reset_block,
                        "balance_updater_block": reset_block,
                        "ohlcv_builder_block": reset_block,
                        "whale_detector_block": reset_block,
                    },
                )
        await self.redis.publish_json(REORG_CHANNEL, {"rollback_from_block": rollback_from})

    async def fetch_block(self, block_number: int) -> dict[str, Any]:
        if block_number in self.block_cache:
            return self.block_cache[block_number]
        block = await self.rpc.call("eth_getBlockByNumber", [to_hex_quantity(block_number), False])
        self.block_cache[block_number] = block
        # Keep a bounded cache for reorg checks without growing unbounded in long-lived workers.
        if len(self.block_cache) > BLOCK_CACHE_MAX_SIZE:
            oldest = sorted(self.block_cache.keys())[:BLOCK_CACHE_EVICT_COUNT]
            for key in oldest:
                self.block_cache.pop(key, None)
        return block

    async def fetch_transfer_logs_for_block(self, block_number: int) -> list[dict[str, Any]]:
        if not self.tracked_tokens:
            return []
        logs = await self.rpc.call(
            "eth_getLogs",
            [
                build_logs_filter(
                    from_block=block_number,
                    to_block=block_number,
                    addresses=self.tracked_tokens,
                    topics=[TRANSFER_TOPIC],
                )
            ],
        )
        return list(logs)

    async def fetch_swap_logs_for_block(self, block_number: int) -> list[dict[str, Any]]:
        if not self.tracked_pools:
            return []
        logs = await self.rpc.call(
            "eth_getLogs",
            [
                build_logs_filter(
                    from_block=block_number,
                    to_block=block_number,
                    addresses=[pool.pool_address for pool in self.tracked_pools],
                    topics=DEX_SWAP_TOPIC_FILTER,
                )
            ],
        )
        return list(logs)

    async def store_block_only(self, block_number: int) -> None:
        block = await self.fetch_block(block_number)
        await self.insert_block(block_number, block)

    @contextlib.asynccontextmanager
    async def _connection_context(self, connection: AsyncConnection | None) -> Any:
        if connection is not None:
            yield connection
            return
        async with self.database.connection() as managed:
            yield managed

    async def _set_state_ints(self, connection: AsyncConnection, values: dict[str, int]) -> None:
        rows = [{"key": key, "value": str(value)} for key, value in values.items()]
        if not rows:
            return
        await connection.execute(
            text(
                """
                INSERT INTO polygon_indexer_state (key, value, updated_at)
                VALUES (:key, :value, now())
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    updated_at = now()
                """
            ),
            rows,
        )

    async def insert_block(
        self,
        block_number: int,
        block: dict[str, Any],
        *,
        connection: AsyncConnection | None = None,
    ) -> None:
        timestamp = datetime.fromtimestamp(from_hex_quantity(block["timestamp"]), tz=UTC)
        gas_used = from_hex_quantity(block.get("gasUsed"))
        gas_limit = max(from_hex_quantity(block.get("gasLimit")), 1)
        tx_count = len(block.get("transactions", []))
        base_fee = from_hex_quantity(block.get("baseFeePerGas"))
        base_fee_gwei = base_fee / 1_000_000_000 if base_fee else 0
        gas_used_pct = (gas_used / gas_limit) * 100
        async with self._connection_context(connection) as active_connection:
            await active_connection.execute(
                text(
                    """
                    INSERT INTO polygon_blocks (
                        block_number,
                        block_hash,
                        parent_hash,
                        timestamp,
                        tx_count,
                        gas_used,
                        base_fee_per_gas
                    )
                    VALUES (
                        :block_number,
                        :block_hash,
                        :parent_hash,
                        :timestamp,
                        :tx_count,
                        :gas_used,
                        :base_fee_per_gas
                    )
                    ON CONFLICT (block_number) DO UPDATE
                    SET block_hash = EXCLUDED.block_hash,
                        parent_hash = EXCLUDED.parent_hash,
                        timestamp = EXCLUDED.timestamp,
                        tx_count = EXCLUDED.tx_count,
                        gas_used = EXCLUDED.gas_used,
                        base_fee_per_gas = EXCLUDED.base_fee_per_gas
                    """
                ),
                {
                    "block_number": block_number,
                    "block_hash": block["hash"],
                    "parent_hash": block["parentHash"],
                    "timestamp": timestamp,
                    "tx_count": tx_count,
                    "gas_used": gas_used,
                    "base_fee_per_gas": base_fee,
                },
            )
            await active_connection.execute(
                text(
                    """
                    INSERT INTO polygon_gas_metrics (
                        block_number,
                        base_fee_gwei,
                        gas_used_pct,
                        tx_count,
                        timestamp
                    )
                    VALUES (
                        :block_number,
                        :base_fee_gwei,
                        :gas_used_pct,
                        :tx_count,
                        :timestamp
                    )
                    ON CONFLICT (block_number) DO UPDATE
                    SET base_fee_gwei = EXCLUDED.base_fee_gwei,
                        gas_used_pct = EXCLUDED.gas_used_pct,
                        tx_count = EXCLUDED.tx_count,
                        timestamp = EXCLUDED.timestamp
                    """
                ),
                {
                    "block_number": block_number,
                    "base_fee_gwei": base_fee_gwei,
                    "gas_used_pct": gas_used_pct,
                    "tx_count": tx_count,
                    "timestamp": timestamp,
                },
            )

    async def persist_transfer_logs(
        self,
        logs: list[dict[str, Any]],
        *,
        connection: AsyncConnection | None = None,
    ) -> int:
        if not logs:
            return 0
        blocks = await self._load_blocks_for_logs(logs)
        rows: list[dict[str, Any]] = []
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            value = self._parse_transfer_value(log)
            if value is None:
                continue
            block_number = from_hex_quantity(log["blockNumber"])
            timestamp = blocks[block_number]
            rows.append(
                {
                    "block_number": block_number,
                    "tx_hash": log["transactionHash"],
                    "log_index": from_hex_quantity(log["logIndex"]),
                    "contract_address": normalize_address(log["address"]),
                    "from_address": decode_topic_address(topics[1]),
                    "to_address": decode_topic_address(topics[2]),
                    "value": value,
                    "timestamp": timestamp,
                }
            )
        if not rows:
            return 0
        async with self._connection_context(connection) as active_connection:
            await active_connection.execute(
                text(
                    """
                    INSERT INTO polygon_token_transfers (
                        block_number,
                        tx_hash,
                        log_index,
                        contract_address,
                        from_address,
                        to_address,
                        value,
                        timestamp
                    )
                    VALUES (
                        :block_number,
                        :tx_hash,
                        :log_index,
                        :contract_address,
                        :from_address,
                        :to_address,
                        :value,
                        :timestamp
                    )
                    ON CONFLICT (tx_hash, log_index) DO NOTHING
                    """
                ),
                rows,
            )
            for row in rows:
                await active_connection.execute(
                    text(
                        """
                        UPDATE polygon_tokens
                        SET first_seen_block = COALESCE(first_seen_block, :block_number)
                        WHERE contract_address = :contract_address
                        """
                    ),
                    {"contract_address": row["contract_address"], "block_number": row["block_number"]},
                )
        return len(rows)

    def _parse_transfer_value(self, log: dict[str, Any]) -> int | None:
        try:
            raw_data = log.get("data", "0x")
            return int(raw_data, 16) if raw_data and raw_data != "0x" else 0
        except (TypeError, ValueError):
            return None

    async def persist_swap_logs(
        self,
        logs: list[dict[str, Any]],
        *,
        connection: AsyncConnection | None = None,
    ) -> int:
        if not logs:
            return 0
        blocks = await self._load_blocks_for_logs(logs)
        rows: list[dict[str, Any]] = []
        for log in logs:
            decoded = self.decode_swap_log(log)
            if decoded is None:
                continue
            block_number = from_hex_quantity(log["blockNumber"])
            decoded["timestamp"] = blocks[block_number]
            rows.append(decoded)
        if not rows:
            return 0
        async with self._connection_context(connection) as active_connection:
            await active_connection.execute(
                text(
                    """
                    INSERT INTO polygon_dex_swaps (
                        block_number,
                        tx_hash,
                        log_index,
                        pool_address,
                        sender,
                        recipient,
                        amount0,
                        amount1,
                        sqrt_price_x96,
                        liquidity,
                        tick,
                        timestamp
                    )
                    VALUES (
                        :block_number,
                        :tx_hash,
                        :log_index,
                        :pool_address,
                        :sender,
                        :recipient,
                        :amount0,
                        :amount1,
                        :sqrt_price_x96,
                        :liquidity,
                        :tick,
                        :timestamp
                    )
                    ON CONFLICT (tx_hash, log_index) DO NOTHING
                    """
                ),
                rows,
            )
        return len(rows)

    def decode_swap_log(self, log: dict[str, Any]) -> dict[str, Any] | None:
        topics = log.get("topics", [])
        if not topics:
            return None
        block_number = from_hex_quantity(log["blockNumber"])
        if topics[0] == V2_SWAP_TOPIC:
            sender = decode_topic_address(topics[1])
            recipient = decode_topic_address(topics[2])
            amount0_in, amount1_in, amount0_out, amount1_out = decode(
                ["uint256", "uint256", "uint256", "uint256"],
                bytes.fromhex(log["data"][2:]),
            )
            # Keep V2 swaps on the same signed delta convention as V3:
            # positive means the pool sent tokens out, negative means it received them.
            return {
                "block_number": block_number,
                "tx_hash": log["transactionHash"],
                "log_index": from_hex_quantity(log["logIndex"]),
                "pool_address": normalize_address(log["address"]),
                "sender": sender,
                "recipient": recipient,
                "amount0": int(amount0_out) - int(amount0_in),
                "amount1": int(amount1_out) - int(amount1_in),
                "sqrt_price_x96": None,
                "liquidity": None,
                "tick": None,
            }
        if topics[0] == V3_SWAP_TOPIC:
            sender = decode_topic_address(topics[1])
            recipient = decode_topic_address(topics[2])
            amount0, amount1, sqrt_price_x96, liquidity, tick = decode(
                ["int256", "int256", "uint160", "uint128", "int24"],
                bytes.fromhex(log["data"][2:]),
            )
            return {
                "block_number": block_number,
                "tx_hash": log["transactionHash"],
                "log_index": from_hex_quantity(log["logIndex"]),
                "pool_address": normalize_address(log["address"]),
                "sender": sender,
                "recipient": recipient,
                "amount0": int(amount0),
                "amount1": int(amount1),
                "sqrt_price_x96": int(sqrt_price_x96),
                "liquidity": int(liquidity),
                "tick": int(tick),
            }
        return None

    async def _load_blocks_for_logs(self, logs: list[dict[str, Any]]) -> dict[int, datetime]:
        block_numbers = sorted({from_hex_quantity(log["blockNumber"]) for log in logs})
        results: dict[int, datetime] = {}
        for block_number in block_numbers:
            block = await self.fetch_block(block_number)
            results[block_number] = datetime.fromtimestamp(from_hex_quantity(block["timestamp"]), tz=UTC)
        return results


async def run_service() -> None:
    settings = load_settings("ingest")
    configure_logging(settings.log_level)
    service = IngestService(settings)
    loop = asyncio.get_running_loop()
    runner = asyncio.create_task(service.run(), name="polygon-ingest-service")

    def handle_shutdown() -> None:
        if not service._running:
            return
        LOGGER.info("shutdown signal received, stopping ingest service")
        service.request_shutdown()
        if not runner.done():
            runner.cancel()

    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, handle_shutdown)
    try:
        await runner
    except asyncio.CancelledError:
        if service._running:
            raise
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signum)
        await service.close()

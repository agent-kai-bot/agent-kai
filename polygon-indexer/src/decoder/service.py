from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from datetime import UTC, datetime
from typing import Any

from eth_abi import decode
from sqlalchemy import text

from src.shared.config import Settings, load_settings
from src.shared.db import Database, StateStore
from src.shared.events import ERC20_METADATA_CALLS, GOVERNANCE_TOPICS, NEW_BLOCK_CHANNEL, REORG_CHANNEL, OWNERSHIP_TRANSFERRED_TOPIC, PAUSED_TOPIC, TRANSFER_TOPIC, UNPAUSED_TOPIC, UPGRADED_TOPIC
from src.shared.evm import ZERO_ADDRESS, decode_string_result, decode_topic_address, decode_uint_result, from_hex_quantity, normalize_address
from src.shared.logging import configure_logging
from src.shared.redis import RedisClient
from src.shared.rpc import EthCall, RpcGatewayClient, build_logs_filter
from src.shared.utils import chunk_range

LOGGER = logging.getLogger(__name__)


class DecoderService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings.database_url)
        self.state = StateStore(self.database)
        self.redis = RedisClient(settings.redis_url)
        self.rpc = RpcGatewayClient(settings.rpc_gateway_url, timeout=settings.request_timeout_seconds)
        self._running = True

    def request_shutdown(self) -> None:
        self._running = False

    async def close(self) -> None:
        await self.rpc.close()
        await self.redis.close()
        await self.database.dispose()

    async def run(self) -> None:
        while self._running:
            await self.catch_up()
            await self.enrich_missing_token_metadata()
            async for channel, payload in self.redis.subscribe(NEW_BLOCK_CHANNEL, REORG_CHANNEL):
                if not self._running:
                    return
                if channel == REORG_CHANNEL:
                    LOGGER.warning("decoder observed reorg event: %s", payload)
                    break
                await self.process_range(payload["block_number"], payload["block_number"])
                await self.enrich_missing_token_metadata()

    async def catch_up(self) -> None:
        last_decoded = await self.state.get_int("last_decoded_block", 0)
        last_indexed = await self.state.get_int("last_indexed_block", 0)
        if last_indexed <= last_decoded:
            return
        await self.process_range(last_decoded + 1, last_indexed)

    async def process_range(self, start_block: int, end_block: int) -> None:
        if start_block > end_block:
            return
        await self.insert_mint_burn_events(start_block, end_block)
        await self.insert_governance_events(start_block, end_block)
        await self.state.set_int("last_decoded_block", end_block)

    async def insert_mint_burn_events(self, start_block: int, end_block: int) -> None:
        async with self.database.connection() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            block_number,
                            tx_hash,
                            log_index,
                            contract_address,
                            from_address,
                            to_address,
                            value,
                            timestamp
                        FROM polygon_token_transfers
                        WHERE block_number BETWEEN :start_block AND :end_block
                        AND (from_address = :zero_address OR to_address = :zero_address)
                        ORDER BY block_number, log_index
                        """
                    ),
                    {
                        "start_block": start_block,
                        "end_block": end_block,
                        "zero_address": ZERO_ADDRESS,
                    },
                )
            ).mappings().all()
        if not rows:
            return
        events = []
        for row in rows:
            event_type = "mint" if row["from_address"] == ZERO_ADDRESS else "burn"
            event_data = {
                "from": row["from_address"],
                "to": row["to_address"],
                "value": str(row["value"]),
            }
            events.append(
                {
                    "block_number": row["block_number"],
                    "tx_hash": row["tx_hash"],
                    "log_index": row["log_index"],
                    "contract_address": normalize_address(row["contract_address"]),
                    "event_type": event_type,
                    "event_data": json.dumps(event_data),
                    "timestamp": row["timestamp"],
                }
            )
        await self.insert_contract_events(events)

    async def insert_governance_events(self, start_block: int, end_block: int) -> None:
        contracts = await self.load_tracked_contracts()
        if not contracts:
            return
        events: list[dict[str, Any]] = []
        for chunk_start, chunk_end in chunk_range(start_block, end_block, self.settings.log_range_limit):
            logs = await self.rpc.call(
                "eth_getLogs",
                [
                    build_logs_filter(
                        from_block=chunk_start,
                        to_block=chunk_end,
                        addresses=contracts,
                        topics=[GOVERNANCE_TOPICS],
                    )
                ],
            )
            for log in logs:
                decoded = await self.decode_governance_log(log)
                if decoded is not None:
                    events.append(decoded)
        if events:
            await self.insert_contract_events(events)

    async def insert_contract_events(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        async with self.database.connection() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO polygon_contract_events (
                        block_number,
                        tx_hash,
                        log_index,
                        contract_address,
                        event_type,
                        event_data,
                        timestamp
                    )
                    VALUES (
                        :block_number,
                        :tx_hash,
                        :log_index,
                        :contract_address,
                        :event_type,
                        CAST(:event_data AS JSONB),
                        :timestamp
                    )
                    ON CONFLICT (tx_hash, log_index) DO NOTHING
                    """
                ),
                rows,
            )

    async def load_tracked_contracts(self) -> list[str]:
        async with self.database.connection() as connection:
            token_rows = (
                await connection.execute(text("SELECT contract_address FROM polygon_tokens WHERE is_tracked = true"))
            ).mappings().all()
            pool_rows = (
                await connection.execute(text("SELECT pool_address FROM polygon_dex_pools WHERE is_tracked = true"))
            ).mappings().all()
        contracts = [normalize_address(row["contract_address"]) for row in token_rows]
        contracts.extend(normalize_address(row["pool_address"]) for row in pool_rows)
        return list(dict.fromkeys(contracts))

    async def decode_governance_log(self, log: dict[str, Any]) -> dict[str, Any] | None:
        topics = log.get("topics", [])
        if not topics:
            return None
        topic0 = topics[0]
        timestamp = await self.load_block_timestamp(from_hex_quantity(log["blockNumber"]))
        event_type = None
        event_data: dict[str, Any] = {}

        if topic0 == OWNERSHIP_TRANSFERRED_TOPIC and len(topics) >= 3:
            event_type = "ownership_transferred"
            event_data = {
                "previous_owner": decode_topic_address(topics[1]),
                "new_owner": decode_topic_address(topics[2]),
            }
        elif topic0 == UPGRADED_TOPIC and len(topics) >= 2:
            event_type = "upgraded"
            event_data = {"implementation": decode_topic_address(topics[1])}
        elif topic0 == PAUSED_TOPIC:
            event_type = "paused"
            if log["data"] and log["data"] != "0x":
                event_data = {"account": decode(["address"], bytes.fromhex(log["data"][2:]))[0]}
        elif topic0 == UNPAUSED_TOPIC:
            event_type = "unpaused"
            if log["data"] and log["data"] != "0x":
                event_data = {"account": decode(["address"], bytes.fromhex(log["data"][2:]))[0]}
        if event_type is None:
            return None

        return {
            "block_number": from_hex_quantity(log["blockNumber"]),
            "tx_hash": log["transactionHash"],
            "log_index": from_hex_quantity(log["logIndex"]),
            "contract_address": normalize_address(log["address"]),
            "event_type": event_type,
            "event_data": json.dumps(event_data, default=str),
            "timestamp": timestamp,
        }

    async def load_block_timestamp(self, block_number: int) -> datetime:
        async with self.database.connection() as connection:
            timestamp = (
                await connection.execute(
                    text("SELECT timestamp FROM polygon_blocks WHERE block_number = :block_number"),
                    {"block_number": block_number},
                )
            ).scalar_one_or_none()
        if timestamp is not None:
            return timestamp
        block = await self.rpc.call("eth_getBlockByNumber", [hex(block_number), False])
        return datetime.fromtimestamp(from_hex_quantity(block["timestamp"]), tz=UTC)

    async def enrich_missing_token_metadata(self) -> None:
        async with self.database.connection() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT contract_address
                        FROM polygon_tokens
                        WHERE is_tracked = true
                        AND (
                            name IS NULL
                            OR symbol IS NULL
                            OR total_supply IS NULL
                            OR symbol LIKE '0x%'
                        )
                        ORDER BY created_at
                        LIMIT 50
                        """
                    )
                )
            ).mappings().all()
        for row in rows:
            await self.enrich_token(normalize_address(row["contract_address"]))

    async def enrich_token(self, contract_address: str) -> None:
        async def call(selector: str) -> str | None:
            try:
                return await self.rpc.call("eth_call", EthCall(to=contract_address, data=selector).to_params())
            except Exception:
                return None

        name = decode_string_result(await call(ERC20_METADATA_CALLS["name"]))
        symbol = decode_string_result(await call(ERC20_METADATA_CALLS["symbol"]))
        decimals = decode_uint_result(await call(ERC20_METADATA_CALLS["decimals"]))
        total_supply = decode_uint_result(await call(ERC20_METADATA_CALLS["totalSupply"]))
        async with self.database.connection() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE polygon_tokens
                    SET name = COALESCE(:name, name),
                        symbol = COALESCE(:symbol, symbol),
                        decimals = COALESCE(:decimals, decimals),
                        total_supply = COALESCE(:total_supply, total_supply)
                    WHERE contract_address = :contract_address
                    """
                ),
                {
                    "contract_address": contract_address,
                    "name": name,
                    "symbol": symbol,
                    "decimals": decimals,
                    "total_supply": total_supply,
                },
            )


async def run_service() -> None:
    settings = load_settings("decoder")
    configure_logging(settings.log_level)
    service = DecoderService(settings)
    loop = asyncio.get_running_loop()
    runner = asyncio.create_task(service.run(), name="polygon-decoder-service")

    def handle_shutdown() -> None:
        if not service._running:
            return
        LOGGER.info("shutdown signal received, stopping decoder service")
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

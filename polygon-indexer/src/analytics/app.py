from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from src.shared.config import Settings, load_settings
from src.shared.db import Database, StateStore
from src.shared.events import INTERVAL_SECONDS, NEW_BLOCK_CHANNEL, QUOTE_SYMBOLS, WHALE_TRANSFERS_CHANNEL
from src.shared.evm import ZERO_ADDRESS, normalize_address, units_to_decimal
from src.shared.http import envelope
from src.shared.logging import configure_logging
from src.shared.redis import RedisClient
from src.shared.rpc import RpcGatewayClient, fetch_block_number
from src.shared.utils import bucket_start, compute_gini, now_date, parse_iso8601, parse_period, to_iso8601, utcnow

LOGGER = logging.getLogger(__name__)
STABLE_QUOTES = {"USDC", "USDT", "DAI"}


def _jsonify(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return to_iso8601(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    return value


@dataclass(slots=True)
class PricePoint:
    token_address: str
    quote_symbol: str
    price: Decimal
    pool_address: str
    open_time: datetime


def _decimal_to_float(value: Decimal | int | float | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _decimal_to_string(value: Decimal | int | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return format(Decimal(value), "f")


def _ratio_to_pct(value: Decimal | int | float | None) -> float | None:
    if value is None:
        return None
    return round(float(Decimal(value) * Decimal(100)), 1)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _compute_backfill_pct(
    *,
    backfill_complete: bool,
    backfill_start_block: int,
    last_indexed_block: int,
    chain_head: int | None,
) -> float:
    if backfill_complete:
        return 100.0
    if chain_head is None or chain_head <= 0:
        return 0.0
    if backfill_start_block <= 0 or backfill_start_block >= chain_head:
        return 100.0 if last_indexed_block >= chain_head else 0.0
    progress = (last_indexed_block - backfill_start_block) / max(chain_head - backfill_start_block, 1)
    return round(_clamp(progress, 0.0, 1.0) * 100, 1)


def _compute_tps(rows: list[dict[str, Any]], window: int = 20) -> float:
    if not rows:
        return 0.0
    sample = rows[:window]
    if len(sample) == 1:
        tx_count = int(sample[0].get("tx_count") or 0)
        return round(tx_count / 2.0, 1)
    newest = sample[0]["timestamp"]
    oldest = sample[-1]["timestamp"]
    if not isinstance(newest, datetime) or not isinstance(oldest, datetime):
        return 0.0
    elapsed = max((newest - oldest).total_seconds(), 1.0)
    total_txs = sum(int(row.get("tx_count") or 0) for row in sample)
    return round(total_txs / elapsed, 1)


def _compute_rate(rows: list[dict[str, Any]], field: str, window: int = 20) -> float:
    if not rows:
        return 0.0
    sample = rows[:window]
    if len(sample) == 1:
        value = int(sample[0].get(field) or 0)
        return round(value / 2.0, 1)
    newest = sample[0]["timestamp"]
    oldest = sample[-1]["timestamp"]
    if not isinstance(newest, datetime) or not isinstance(oldest, datetime):
        return 0.0
    elapsed = max((newest - oldest).total_seconds(), 1.0)
    total = sum(int(row.get(field) or 0) for row in sample)
    return round(total / elapsed, 1)


def _compute_gas_percentile_rank(history: list[Decimal]) -> float:
    if not history:
        return 0.0
    if len(history) == 1:
        return 0.0
    current = history[0]
    less_or_equal = sum(1 for value in history if value <= current)
    return round((less_or_equal - 1) / (len(history) - 1), 4)


def _passes_whale_threshold(amount_human: Decimal, usd_value: Decimal | None, threshold: Decimal) -> bool:
    if usd_value is not None:
        return usd_value >= threshold
    return amount_human >= threshold


def build_whale_transfer_payload(
    row: dict[str, Any],
    *,
    price: PricePoint | None,
    min_usd: Decimal | None = None,
) -> dict[str, Any] | None:
    decimals = int(row.get("decimals") or row.get("token_decimals") or 18)
    amount_human = units_to_decimal(int(row["value"]), decimals)
    usd_value = amount_human * price.price if price else None
    if min_usd is not None and not _passes_whale_threshold(amount_human, usd_value, min_usd):
        return None
    symbol = row.get("symbol") or row.get("token_symbol") or ""
    payload = {
        "block_number": int(row["block_number"]),
        "tx_hash": row["tx_hash"],
        "contract_address": normalize_address(row["contract_address"]),
        "symbol": symbol,
        "token_symbol": symbol,
        "token_decimals": decimals,
        "from_address": normalize_address(row["from_address"]),
        "to_address": normalize_address(row["to_address"]),
        "value": _decimal_to_string(row["value"]),
        "amount": format(amount_human, "f"),
        "amount_human": format(amount_human, "f"),
        "usd_value": _decimal_to_float(usd_value),
        "timestamp": row["timestamp"],
        "quote_symbol": price.quote_symbol if price else None,
    }
    return payload


def normalize_whale_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("symbol", normalized.get("token_symbol", ""))
    normalized.setdefault("token_symbol", normalized.get("symbol", ""))
    normalized.setdefault("token_decimals", int(normalized.get("decimals") or 18))
    normalized.setdefault("value", normalized.get("value") or normalized.get("amount") or "0")
    normalized.setdefault("amount", normalized.get("amount_human") or normalized.get("amount") or "0")
    normalized.setdefault("amount_human", normalized.get("amount_human") or normalized.get("amount") or "0")
    normalized.setdefault("usd_value", normalized.get("usd_value"))
    return normalized


def build_holder_rows(
    holders: list[dict[str, Any]],
    *,
    decimals: int,
    tracked_total_balance: Decimal,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for holder in holders:
        row = dict(holder)
        balance = Decimal(row["balance"])
        balance_human = units_to_decimal(int(balance), decimals)
        row["balance_human"] = format(balance_human, "f")
        row["pct_of_tracked"] = round(float((balance / tracked_total_balance) * Decimal(100)), 1) if tracked_total_balance else 0.0
        normalized.append(row)
    return normalized


def build_recent_block_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "block_number": int(row["block_number"]),
        "timestamp": row["timestamp"],
        "tx_count": int(row.get("tx_count") or 0),
        "transfer_count": int(row.get("transfer_count") or 0),
        "swap_count": int(row.get("swap_count") or 0),
        "gas_used_pct": round(float(Decimal(row.get("gas_used_pct") or 0)), 1),
    }


def format_sse_event(event: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(_jsonify(payload), separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n"


class AnalyticsService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings.database_url)
        self.state = StateStore(self.database)
        self.redis = RedisClient(settings.redis_url)
        self.rpc = RpcGatewayClient(settings.rpc_gateway_url, timeout=settings.request_timeout_seconds)
        self.tasks: list[asyncio.Task[None]] = []

    async def startup(self) -> None:
        self.tasks = [
            asyncio.create_task(
                self._loop(self.run_balance_updater, self.settings.analytics_balance_updater_interval_seconds),
                name="analytics-balance-updater",
            ),
            asyncio.create_task(
                self._loop(self.run_ohlcv_builder, self.settings.analytics_ohlcv_builder_interval_seconds),
                name="analytics-ohlcv-builder",
            ),
            asyncio.create_task(
                self._loop(self.run_whale_detector, self.settings.analytics_whale_detector_interval_seconds),
                name="analytics-whale-detector",
            ),
            asyncio.create_task(
                self._loop(self.run_holder_snapshots, self.settings.analytics_holder_snapshots_interval_seconds),
                name="analytics-holder-snapshots",
            ),
        ]

    async def shutdown(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.rpc.close()
        await self.redis.close()
        await self.database.dispose()

    async def _loop(self, worker, sleep_seconds: int) -> None:
        while True:
            try:
                await worker()
                await self.refresh_last_analytics_block()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("analytics worker failed: %s", exc)
            await asyncio.sleep(sleep_seconds)

    async def refresh_last_analytics_block(self) -> None:
        balance_block = await self.state.get_int("balance_updater_block", 0)
        ohlcv_block = await self.state.get_int("ohlcv_builder_block", 0)
        whale_block = await self.state.get_int("whale_detector_block", 0)
        values = [value for value in (balance_block, ohlcv_block, whale_block) if value]
        value = min(values) if values else 0
        await self.state.set_int("last_analytics_block", value)

    async def get_latest_block(self) -> int:
        return await self.state.get_int("last_indexed_block", 0)

    async def run_balance_updater(self) -> None:
        last_processed = await self.state.get_int("balance_updater_block", 0)
        target_block = await self.state.get_int("last_indexed_block", 0)
        if target_block <= last_processed:
            return
        async with self.database.connection() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            block_number,
                            contract_address,
                            from_address,
                            to_address,
                            value,
                            timestamp
                        FROM polygon_token_transfers
                        WHERE block_number > :last_processed
                        AND block_number <= :target_block
                        ORDER BY block_number, id
                        LIMIT 5000
                        """
                    ),
                    {
                        "last_processed": last_processed,
                        "target_block": target_block,
                    },
                )
            ).mappings().all()
        if not rows:
            await self.state.set_int("balance_updater_block", target_block)
            return

        deltas: dict[tuple[str, str], dict[str, Any]] = {}
        max_block = last_processed
        for row in rows:
            max_block = max(max_block, row["block_number"])
            value = Decimal(row["value"])
            contract = normalize_address(row["contract_address"])
            timestamp = row["timestamp"]
            if row["from_address"] != ZERO_ADDRESS:
                key = (normalize_address(row["from_address"]), contract)
                current = deltas.setdefault(key, {"balance": Decimal(0), "last_updated_block": 0, "last_updated_at": timestamp})
                current["balance"] -= value
                current["last_updated_block"] = max(current["last_updated_block"], row["block_number"])
                current["last_updated_at"] = max(current["last_updated_at"], timestamp)
            if row["to_address"] != ZERO_ADDRESS:
                key = (normalize_address(row["to_address"]), contract)
                current = deltas.setdefault(key, {"balance": Decimal(0), "last_updated_block": 0, "last_updated_at": timestamp})
                current["balance"] += value
                current["last_updated_block"] = max(current["last_updated_block"], row["block_number"])
                current["last_updated_at"] = max(current["last_updated_at"], timestamp)

        updates = [
            {
                "wallet_address": wallet,
                "contract_address": contract,
                "balance": payload["balance"],
                "last_updated_block": payload["last_updated_block"],
                "last_updated_at": payload["last_updated_at"],
            }
            for (wallet, contract), payload in deltas.items()
        ]
        async with self.database.connection() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO polygon_token_balances (
                        wallet_address,
                        contract_address,
                        balance,
                        last_updated_block,
                        last_updated_at
                    )
                    VALUES (
                        :wallet_address,
                        :contract_address,
                        :balance,
                        :last_updated_block,
                        :last_updated_at
                    )
                    ON CONFLICT (wallet_address, contract_address) DO UPDATE
                    SET balance = polygon_token_balances.balance + EXCLUDED.balance,
                        last_updated_block = GREATEST(
                            polygon_token_balances.last_updated_block,
                            EXCLUDED.last_updated_block
                        ),
                        last_updated_at = EXCLUDED.last_updated_at
                    """
                ),
                updates,
            )
            await connection.execute(text("DELETE FROM polygon_token_balances WHERE balance = 0"))
        await self.state.set_int("balance_updater_block", max_block)

    async def run_ohlcv_builder(self) -> None:
        last_processed = await self.state.get_int("ohlcv_builder_block", 0)
        target_block = await self.state.get_int("last_indexed_block", 0)
        if target_block <= last_processed:
            return
        async with self.database.connection() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            s.id,
                            s.block_number,
                            s.log_index,
                            s.pool_address,
                            s.amount0,
                            s.amount1,
                            s.timestamp,
                            p.token0_address,
                            p.token1_address,
                            COALESCE(t0.symbol, 'TOKEN0') AS token0_symbol,
                            COALESCE(t1.symbol, 'TOKEN1') AS token1_symbol,
                            COALESCE(t0.decimals, 18) AS token0_decimals,
                            COALESCE(t1.decimals, 18) AS token1_decimals
                        FROM polygon_dex_swaps s
                        JOIN polygon_dex_pools p ON p.pool_address = s.pool_address
                        LEFT JOIN polygon_tokens t0 ON t0.contract_address = p.token0_address
                        LEFT JOIN polygon_tokens t1 ON t1.contract_address = p.token1_address
                        WHERE s.block_number > :last_processed
                        AND s.block_number <= :target_block
                        ORDER BY s.block_number, s.timestamp, s.log_index
                        LIMIT 5000
                        """
                    ),
                    {
                        "last_processed": last_processed,
                        "target_block": target_block,
                    },
                )
            ).mappings().all()
        if not rows:
            await self.state.set_int("ohlcv_builder_block", target_block)
            return

        max_block = last_processed
        async with self.database.connection() as connection:
            for row in rows:
                max_block = max(max_block, row["block_number"])
                price_info = self._swap_price(row)
                if price_info is None:
                    continue
                price, volume = price_info
                for interval in INTERVAL_SECONDS:
                    open_time = bucket_start(row["timestamp"], interval)
                    await connection.execute(
                        text(
                            """
                            INSERT INTO polygon_dex_ohlcv (
                                pool_address,
                                interval,
                                open_time,
                                open,
                                high,
                                low,
                                close,
                                volume,
                                trade_count
                            )
                            VALUES (
                                :pool_address,
                                :interval,
                                :open_time,
                                :open,
                                :high,
                                :low,
                                :close,
                                :volume,
                                1
                            )
                            ON CONFLICT (pool_address, interval, open_time) DO UPDATE
                            SET high = GREATEST(polygon_dex_ohlcv.high, EXCLUDED.high),
                                low = LEAST(polygon_dex_ohlcv.low, EXCLUDED.low),
                                close = EXCLUDED.close,
                                volume = polygon_dex_ohlcv.volume + EXCLUDED.volume,
                                trade_count = polygon_dex_ohlcv.trade_count + 1
                            """
                        ),
                        {
                            "pool_address": normalize_address(row["pool_address"]),
                            "interval": interval,
                            "open_time": open_time,
                            "open": price,
                            "high": price,
                            "low": price,
                            "close": price,
                            "volume": volume,
                        },
                    )
        await self.state.set_int("ohlcv_builder_block", max_block)

    def _quote_side(self, token0_symbol: str, token1_symbol: str, *, preferred_quote: str | None = None) -> int | None:
        token0_symbol = token0_symbol.upper()
        token1_symbol = token1_symbol.upper()
        if preferred_quote:
            preferred = preferred_quote.upper()
            if token1_symbol == preferred and token0_symbol != preferred:
                return 1
            if token0_symbol == preferred and token1_symbol != preferred:
                return 0
            return None
        token0_is_quote = token0_symbol in QUOTE_SYMBOLS
        token1_is_quote = token1_symbol in QUOTE_SYMBOLS
        if token1_is_quote and not token0_is_quote:
            return 1
        if token0_is_quote and not token1_is_quote:
            return 0
        return None

    def _swap_price(self, row: Any) -> tuple[Decimal, Decimal] | None:
        amount0 = units_to_decimal(int(row["amount0"]), int(row["token0_decimals"]))
        amount1 = units_to_decimal(int(row["amount1"]), int(row["token1_decimals"]))
        abs_amount0 = abs(amount0)
        abs_amount1 = abs(amount1)
        if abs_amount0 == 0 or abs_amount1 == 0:
            return None
        token0_symbol = str(row["token0_symbol"]).upper()
        token1_symbol = str(row["token1_symbol"]).upper()
        quote_side = self._quote_side(token0_symbol, token1_symbol)
        if quote_side == 1:
            return abs_amount1 / abs_amount0, abs_amount1
        if quote_side == 0:
            return abs_amount0 / abs_amount1, abs_amount0
        return abs_amount1 / abs_amount0, abs_amount1

    async def run_whale_detector(self) -> None:
        last_processed = await self.state.get_int("whale_detector_block", 0)
        target_block = await self.state.get_int("last_indexed_block", 0)
        if target_block <= last_processed:
            return
        async with self.database.connection() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            t.block_number,
                            t.tx_hash,
                            t.contract_address,
                            t.from_address,
                            t.to_address,
                            t.value,
                            t.timestamp,
                            COALESCE(tok.symbol, '') AS symbol,
                            COALESCE(tok.decimals, 18) AS decimals
                        FROM polygon_token_transfers t
                        JOIN polygon_tokens tok ON tok.contract_address = t.contract_address
                        WHERE t.block_number > :last_processed
                        AND t.block_number <= :target_block
                        ORDER BY t.block_number, t.id
                        LIMIT 5000
                        """
                    ),
                    {
                        "last_processed": last_processed,
                        "target_block": target_block,
                    },
                )
            ).mappings().all()
        if not rows:
            await self.state.set_int("whale_detector_block", target_block)
            return
        price_map = await self.load_latest_prices()
        max_block = last_processed
        for row in rows:
            max_block = max(max_block, row["block_number"])
            normalized = units_to_decimal(int(row["value"]), int(row["decimals"]))
            price = price_map.get(normalize_address(row["contract_address"]))
            usd_value = normalized * price.price if price else None
            if not _passes_whale_threshold(normalized, usd_value, Decimal(str(self.settings.whale_threshold_usd))):
                continue
            await self.redis.publish_json(
                WHALE_TRANSFERS_CHANNEL,
                {
                    "block_number": row["block_number"],
                    "tx_hash": row["tx_hash"],
                    "contract_address": normalize_address(row["contract_address"]),
                    "token_symbol": row["symbol"],
                    "token_decimals": int(row["decimals"]),
                    "from_address": normalize_address(row["from_address"]),
                    "to_address": normalize_address(row["to_address"]),
                    "value": format(Decimal(row["value"]), "f"),
                    "amount_human": format(normalized, "f"),
                    "usd_value": _decimal_to_float(usd_value),
                    "timestamp": to_iso8601(row["timestamp"]),
                },
            )
        await self.state.set_int("whale_detector_block", max_block)

    async def run_holder_snapshots(self) -> None:
        snapshot_date = now_date()
        async with self.database.connection() as connection:
            tokens = (
                await connection.execute(
                    text("SELECT contract_address FROM polygon_tokens WHERE is_tracked = true ORDER BY contract_address")
                )
            ).mappings().all()
        for token in tokens:
            contract_address = normalize_address(token["contract_address"])
            async with self.database.connection() as connection:
                exists = (
                    await connection.execute(
                        text(
                            """
                            SELECT 1
                            FROM polygon_holder_snapshots
                            WHERE contract_address = :contract_address
                            AND snapshot_date = :snapshot_date
                            """
                        ),
                        {
                            "contract_address": contract_address,
                            "snapshot_date": snapshot_date,
                        },
                    )
                ).scalar_one_or_none()
                if exists:
                    continue
                balances = (
                    await connection.execute(
                        text(
                            """
                            SELECT balance
                            FROM polygon_token_balances
                            WHERE contract_address = :contract_address
                            AND balance > 0
                            ORDER BY balance DESC
                            """
                        ),
                        {"contract_address": contract_address},
                    )
                ).scalars().all()
                if not balances:
                    continue
                total_balance = sum((Decimal(balance) for balance in balances), Decimal(0))
                top10 = sum((Decimal(balance) for balance in balances[:10]), Decimal(0))
                top50 = sum((Decimal(balance) for balance in balances[:50]), Decimal(0))
                gini = compute_gini(Decimal(balance) for balance in balances)
                await connection.execute(
                    text(
                        """
                        INSERT INTO polygon_holder_snapshots (
                            contract_address,
                            snapshot_date,
                            total_holders,
                            top10_concentration,
                            top50_concentration,
                            gini_coefficient
                        )
                        VALUES (
                            :contract_address,
                            :snapshot_date,
                            :total_holders,
                            :top10_concentration,
                            :top50_concentration,
                            :gini_coefficient
                        )
                        ON CONFLICT (contract_address, snapshot_date) DO UPDATE
                        SET total_holders = EXCLUDED.total_holders,
                            top10_concentration = EXCLUDED.top10_concentration,
                            top50_concentration = EXCLUDED.top50_concentration,
                            gini_coefficient = EXCLUDED.gini_coefficient
                        """
                    ),
                    {
                        "contract_address": contract_address,
                        "snapshot_date": snapshot_date,
                        "total_holders": len(balances),
                        "top10_concentration": top10 / total_balance if total_balance else Decimal(0),
                        "top50_concentration": top50 / total_balance if total_balance else Decimal(0),
                        "gini_coefficient": gini,
                    },
                )

    async def load_latest_prices(self, quote: str | None = None) -> dict[str, PricePoint]:
        async with self.database.connection() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT DISTINCT ON (o.pool_address)
                            o.pool_address,
                            o.close,
                            o.open_time,
                            p.token0_address,
                            p.token1_address,
                            COALESCE(t0.symbol, 'TOKEN0') AS token0_symbol,
                            COALESCE(t1.symbol, 'TOKEN1') AS token1_symbol
                        FROM polygon_dex_ohlcv o
                        JOIN polygon_dex_pools p ON p.pool_address = o.pool_address
                        LEFT JOIN polygon_tokens t0 ON t0.contract_address = p.token0_address
                        LEFT JOIN polygon_tokens t1 ON t1.contract_address = p.token1_address
                        WHERE o.interval = '1h'
                        ORDER BY o.pool_address, o.open_time DESC
                        """
                    )
                )
            ).mappings().all()
            token_rows = (
                await connection.execute(
                    text("SELECT contract_address, symbol FROM polygon_tokens WHERE is_tracked = true")
                )
            ).mappings().all()
        prices: dict[str, PricePoint] = {}
        target_quote = quote.upper() if quote else None
        for row in rows:
            close = Decimal(row["close"])
            if close <= 0:
                continue
            token0_symbol = str(row["token0_symbol"]).upper()
            token1_symbol = str(row["token1_symbol"]).upper()
            quote_side = self._quote_side(token0_symbol, token1_symbol, preferred_quote=target_quote)
            if target_quote and quote_side is None:
                continue
            if quote_side == 1 or quote_side is None:
                point = PricePoint(normalize_address(row["token0_address"]), token1_symbol, close, normalize_address(row["pool_address"]), row["open_time"])
            else:
                point = PricePoint(normalize_address(row["token1_address"]), token0_symbol, Decimal(1) / close, normalize_address(row["pool_address"]), row["open_time"])
            existing = prices.get(point.token_address)
            if existing is None or point.open_time > existing.open_time:
                prices[point.token_address] = point
        for row in token_rows:
            symbol = str(row["symbol"] or "").upper()
            contract_address = normalize_address(row["contract_address"])
            if symbol in STABLE_QUOTES and (target_quote is None or symbol == target_quote):
                prices.setdefault(
                    contract_address,
                    PricePoint(
                        token_address=contract_address,
                        quote_symbol=symbol,
                        price=Decimal(1),
                        pool_address="",
                        open_time=utcnow(),
                    ),
                )
        return prices

    async def current_chain_head(self) -> int | None:
        try:
            return await fetch_block_number(self.rpc)
        except Exception:
            return None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self.database.connection() as connection:
            result = await connection.execute(text(sql), params or {})
            return [dict(row) for row in result.mappings().all()]

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None


async def query_recent_blocks(service: AnalyticsService, limit: int) -> list[dict[str, Any]]:
    rows = await service.query_all(
        """
        WITH recent_blocks AS (
            SELECT block_number, timestamp, tx_count
            FROM polygon_blocks
            ORDER BY block_number DESC
            LIMIT :limit
        ),
        transfer_counts AS (
            SELECT block_number, count(*) AS transfer_count
            FROM polygon_token_transfers
            WHERE block_number IN (SELECT block_number FROM recent_blocks)
            GROUP BY block_number
        ),
        swap_counts AS (
            SELECT block_number, count(*) AS swap_count
            FROM polygon_dex_swaps
            WHERE block_number IN (SELECT block_number FROM recent_blocks)
            GROUP BY block_number
        )
        SELECT
            b.block_number,
            b.timestamp,
            b.tx_count,
            COALESCE(t.transfer_count, 0) AS transfer_count,
            COALESCE(s.swap_count, 0) AS swap_count,
            COALESCE(g.gas_used_pct, 0) AS gas_used_pct
        FROM recent_blocks b
        LEFT JOIN transfer_counts t ON t.block_number = b.block_number
        LEFT JOIN swap_counts s ON s.block_number = b.block_number
        LEFT JOIN polygon_gas_metrics g ON g.block_number = b.block_number
        ORDER BY b.block_number DESC
        """,
        {"limit": limit},
    )
    rows.sort(key=lambda row: int(row["block_number"]), reverse=True)
    return [build_recent_block_row(row) for row in rows]


async def query_block_activity(service: AnalyticsService, block_number: int, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    row = await service.query_one(
        """
        WITH transfer_counts AS (
            SELECT count(*) AS transfer_count
            FROM polygon_token_transfers
            WHERE block_number = :block_number
        ),
        swap_counts AS (
            SELECT count(*) AS swap_count
            FROM polygon_dex_swaps
            WHERE block_number = :block_number
        )
        SELECT
            b.block_number,
            b.timestamp,
            b.tx_count,
            COALESCE((SELECT transfer_count FROM transfer_counts), :fallback_transfer_count) AS transfer_count,
            COALESCE((SELECT swap_count FROM swap_counts), :fallback_swap_count) AS swap_count,
            COALESCE(g.gas_used_pct, 0) AS gas_used_pct
        FROM polygon_blocks b
        LEFT JOIN polygon_gas_metrics g ON g.block_number = b.block_number
        WHERE b.block_number = :block_number
        """,
        {
            "block_number": block_number,
            "fallback_transfer_count": int((fallback or {}).get("transfer_count") or 0),
            "fallback_swap_count": int((fallback or {}).get("swap_count") or 0),
        },
    )
    if row:
        return build_recent_block_row(row)
    return {
        "block_number": block_number,
        "timestamp": (fallback or {}).get("timestamp"),
        "tx_count": 0,
        "transfer_count": int((fallback or {}).get("transfer_count") or 0),
        "swap_count": int((fallback or {}).get("swap_count") or 0),
        "gas_used_pct": 0.0,
    }


async def query_system_totals(service: AnalyticsService) -> dict[str, Any]:
    row = await service.query_one(
        """
        SELECT
            (SELECT count(*) FROM polygon_blocks) AS total_blocks_indexed,
            (SELECT count(*) FROM polygon_token_transfers) AS total_transfers_indexed,
            (SELECT count(*) FROM polygon_contract_events) AS total_events_indexed,
            (SELECT count(*) FROM polygon_tokens WHERE is_tracked = true) AS tracked_token_count,
            GREATEST(
                COALESCE((SELECT max(timestamp) FROM polygon_blocks), TIMESTAMPTZ 'epoch'),
                COALESCE((SELECT max(timestamp) FROM polygon_token_transfers), TIMESTAMPTZ 'epoch'),
                COALESCE((SELECT max(timestamp) FROM polygon_gas_metrics), TIMESTAMPTZ 'epoch')
            ) AS last_updated_at
        """
    )
    if not row:
        return {
            "total_blocks_indexed": 0,
            "total_transfers_indexed": 0,
            "total_events_indexed": 0,
            "tracked_token_count": 0,
            "last_updated_at": None,
        }
    last_updated_at = row.get("last_updated_at")
    if isinstance(last_updated_at, datetime) and last_updated_at == datetime(1970, 1, 1, tzinfo=UTC):
        row = dict(row)
        row["last_updated_at"] = None
    return row


async def query_status_snapshot(service: AnalyticsService) -> dict[str, Any]:
    last_indexed_block = await service.get_latest_block()
    chain_head = await service.current_chain_head()
    last_decoded_block = await service.state.get_int("last_decoded_block", 0)
    last_analytics_block = await service.state.get_int("last_analytics_block", 0)
    backfill_complete = await service.state.get_bool("backfill_complete", False)
    backfill_start_block = await service.state.get_int("backfill_start_block", 0)
    totals = await query_system_totals(service)
    lag_blocks = (chain_head - last_indexed_block) if chain_head is not None else None
    backfill_pct = _compute_backfill_pct(
        backfill_complete=backfill_complete,
        backfill_start_block=backfill_start_block,
        last_indexed_block=last_indexed_block,
        chain_head=chain_head,
    )
    return {
        "chain_head": chain_head,
        "last_indexed_block": last_indexed_block,
        "last_decoded_block": last_decoded_block,
        "last_analytics_block": last_analytics_block,
        "last_indexed": last_indexed_block,
        "lag_blocks": lag_blocks,
        "lag": lag_blocks,
        "backfill_complete": backfill_complete,
        "backfill_start_block": backfill_start_block,
        "backfill_pct": backfill_pct,
        "total_blocks_indexed": int(totals["total_blocks_indexed"] or 0),
        "total_transfers_indexed": int(totals["total_transfers_indexed"] or 0),
        "total_events_indexed": int(totals["total_events_indexed"] or 0),
        "tracked_token_count": int(totals["tracked_token_count"] or 0),
        "last_updated_at": totals["last_updated_at"],
    }


async def query_whale_transfers(
    service: AnalyticsService,
    *,
    since_dt: datetime,
    min_usd: float,
    limit: int,
) -> list[dict[str, Any]]:
    scan_limit = min(max(limit * 50, 500), 5000)
    rows = await service.query_all(
        """
        SELECT
            t.block_number,
            t.tx_hash,
            t.contract_address,
            t.from_address,
            t.to_address,
            t.value,
            t.timestamp,
            tok.symbol,
            tok.decimals
        FROM polygon_token_transfers t
        JOIN polygon_tokens tok ON tok.contract_address = t.contract_address
        WHERE t.timestamp >= :since_dt
        ORDER BY t.timestamp DESC
        LIMIT :scan_limit
        """,
        {"since_dt": since_dt, "scan_limit": scan_limit},
    )
    prices = await service.load_latest_prices()
    threshold = Decimal(str(min_usd))
    whales: list[dict[str, Any]] = []
    for row in rows:
        payload = build_whale_transfer_payload(
            row,
            price=prices.get(normalize_address(row["contract_address"])),
            min_usd=threshold,
        )
        if payload is None:
            continue
        whales.append(payload)
        if len(whales) >= limit:
            break
    whales.sort(key=lambda row: row["timestamp"], reverse=True)
    return whales


async def query_overview(service: AnalyticsService) -> dict[str, Any]:
    status = await query_status_snapshot(service)
    since_24h = utcnow() - timedelta(hours=24)
    since_1h = utcnow() - timedelta(hours=1)

    recent_blocks = await query_recent_blocks(service, 40)
    gas_rows = await service.query_all(
        """
        SELECT block_number, base_fee_gwei, gas_used_pct, tx_count, timestamp
        FROM polygon_gas_metrics
        ORDER BY block_number DESC
        LIMIT 100
        """
    )
    token_rows = await service.query_all(
        """
        SELECT
            t.contract_address,
            t.symbol,
            t.name,
            t.decimals,
            hs.snapshot_date,
            COALESCE(hs.total_holders, 0) AS total_holders,
            COALESCE(hs.top10_concentration, 0) AS top10_concentration,
            COALESCE(hs.top50_concentration, 0) AS top50_concentration,
            hs.gini_coefficient,
            COALESCE(stats.recent_activity_1h, 0) AS recent_activity_1h,
            COALESCE(stats.transfers_24h, 0) AS transfers_24h
        FROM polygon_tokens t
        LEFT JOIN LATERAL (
            SELECT snapshot_date, total_holders, top10_concentration, top50_concentration, gini_coefficient
            FROM polygon_holder_snapshots hs
            WHERE hs.contract_address = t.contract_address
            ORDER BY snapshot_date DESC
            LIMIT 1
        ) hs ON true
        LEFT JOIN LATERAL (
            SELECT
                count(*) FILTER (WHERE tr.timestamp >= :since_1h) AS recent_activity_1h,
                count(*) AS transfers_24h
            FROM polygon_token_transfers tr
            WHERE tr.contract_address = t.contract_address
            AND tr.timestamp >= :since_24h
        ) stats ON true
        WHERE t.is_tracked = true
        ORDER BY stats.transfers_24h DESC, t.symbol, t.contract_address
        """,
        {"since_24h": since_24h, "since_1h": since_1h},
    )
    recent_transfer_rows = await service.query_all(
        """
        SELECT contract_address, value
        FROM polygon_token_transfers
        WHERE timestamp >= :since_24h
        """,
        {"since_24h": since_24h},
    )
    prices = await service.load_latest_prices()

    gas_history_values = [Decimal(row["base_fee_gwei"] or 0) for row in gas_rows]
    gas_history = [_decimal_to_float(value) or 0.0 for value in reversed(gas_history_values)]
    current_gas = gas_history_values[0] if gas_history_values else Decimal(0)
    gas_avg_20 = (
        sum(gas_history_values[:20], Decimal(0)) / Decimal(min(len(gas_history_values), 20))
        if gas_history_values
        else Decimal(0)
    )

    decimals_by_token = {normalize_address(row["contract_address"]): int(row["decimals"] or 18) for row in token_rows}
    whale_counts: dict[str, int] = {}
    threshold = Decimal(str(service.settings.whale_threshold_usd))
    for row in recent_transfer_rows:
        contract = normalize_address(row["contract_address"])
        amount_human = units_to_decimal(int(row["value"]), decimals_by_token.get(contract, 18))
        price = prices.get(contract)
        usd_value = amount_human * price.price if price else None
        if not _passes_whale_threshold(amount_human, usd_value, threshold):
            continue
        whale_counts[contract] = whale_counts.get(contract, 0) + 1

    tokens = []
    for row in token_rows:
        contract = normalize_address(row["contract_address"])
        price = prices.get(contract)
        holder_snapshot = {
            "snapshot_date": row.get("snapshot_date"),
            "total_holders": int(row["total_holders"] or 0),
            "top10_concentration_pct": _ratio_to_pct(row["top10_concentration"]) or 0.0,
            "top50_concentration_pct": _ratio_to_pct(row.get("top50_concentration")) or 0.0,
            "gini_coefficient": _decimal_to_float(row.get("gini_coefficient")),
        }
        tokens.append(
            {
                "contract_address": contract,
                "symbol": row["symbol"],
                "name": row["name"],
                "decimals": int(row["decimals"] or 18),
                "transfers_24h": int(row["transfers_24h"] or 0),
                "recent_activity_1h": int(row.get("recent_activity_1h") or 0),
                "recent_activity_24h": int(row["transfers_24h"] or 0),
                "latest_price": _decimal_to_string(price.price) if price else None,
                "price_quote": price.quote_symbol if price else None,
                "holder_snapshot": holder_snapshot,
                "total_holders": holder_snapshot["total_holders"],
                "top10_concentration_pct": holder_snapshot["top10_concentration_pct"],
                "top50_concentration_pct": holder_snapshot["top50_concentration_pct"],
                "gini_coefficient": holder_snapshot["gini_coefficient"],
                "whale_count_24h": whale_counts.get(contract, 0),
            }
        )

    gas_current_gwei = _decimal_to_float(current_gas) or 0.0
    gas_avg_20_block_gwei = _decimal_to_float(gas_avg_20) or 0.0
    tps_current = _compute_tps(recent_blocks)
    transfers_per_second = _compute_rate(recent_blocks, "transfer_count")
    system_summary = {
        "total_blocks_indexed": status["total_blocks_indexed"],
        "total_transfers_indexed": status["total_transfers_indexed"],
        "total_events_indexed": status["total_events_indexed"],
        "tracked_token_count": status["tracked_token_count"],
        "last_updated_at": status["last_updated_at"],
    }
    pulse = {
        "block_number": status["last_indexed_block"],
        "chain_head": status["chain_head"],
        "lag_blocks": status["lag_blocks"],
        "backfill_complete": status["backfill_complete"],
        "backfill_pct": status["backfill_pct"],
        "gas_current_gwei": gas_current_gwei,
        "gas_avg_20_block_gwei": gas_avg_20_block_gwei,
        "tps_current": tps_current,
        "transfers_per_second": transfers_per_second,
    }

    return {
        "status": status,
        "system_summary": system_summary,
        "pulse": pulse,
        "chain_head": status["chain_head"],
        "last_indexed_block": status["last_indexed_block"],
        "head_lag_blocks": status["lag_blocks"],
        "backfill_complete": status["backfill_complete"],
        "backfill_pct": status["backfill_pct"],
        "gas_current_gwei": gas_current_gwei,
        "gas_avg_20_block_gwei": gas_avg_20_block_gwei,
        "tps_current": tps_current,
        "transfers_per_second": transfers_per_second,
        "gas_percentile_rank": _compute_gas_percentile_rank(gas_history_values),
        "gas_history_100_blocks": gas_history,
        "total_blocks_indexed": status["total_blocks_indexed"],
        "total_transfers_indexed": status["total_transfers_indexed"],
        "total_events_indexed": status["total_events_indexed"],
        "tracked_token_count": status["tracked_token_count"],
        "last_updated_at": status["last_updated_at"],
        "tokens": tokens,
        "recent_blocks": recent_blocks,
    }


async def build_head_event_payload(service: AnalyticsService, payload: dict[str, Any]) -> dict[str, Any]:
    block_number = int(payload["block_number"])
    block_row = await query_block_activity(service, block_number, fallback=payload)
    recent_blocks = await query_recent_blocks(service, 20)
    gas_row = await service.query_one(
        """
        SELECT
            g.base_fee_gwei,
            (
                SELECT avg(base_fee_gwei)
                FROM polygon_gas_metrics
                WHERE block_number >= g.block_number - 20
            ) AS avg_base_fee_20
        FROM polygon_gas_metrics g
        WHERE g.block_number = :block_number
        """,
        {"block_number": block_number},
    )
    status = await query_status_snapshot(service)
    return {
        **block_row,
        "last_indexed_block": status["last_indexed_block"],
        "chain_head": status["chain_head"],
        "last_indexed": status["last_indexed"],
        "lag_blocks": status["lag_blocks"],
        "head_lag_blocks": status["lag"],
        "backfill_complete": status["backfill_complete"],
        "backfill_pct": status["backfill_pct"],
        "total_transfers_indexed": status["total_transfers_indexed"],
        "last_updated_at": status["last_updated_at"],
        "gas_current_gwei": _decimal_to_float(gas_row["base_fee_gwei"]) if gas_row else 0.0,
        "gas_avg_20_block_gwei": _decimal_to_float(gas_row["avg_base_fee_20"]) if gas_row else 0.0,
        "tps_current": _compute_tps(recent_blocks),
        "transfers_per_second": _compute_rate(recent_blocks, "transfer_count"),
    }


async def stream_sse_events(
    service: AnalyticsService,
    *,
    status_interval_seconds: float = 10.0,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    async def forward_redis() -> None:
        async for channel, payload in service.redis.subscribe(NEW_BLOCK_CHANNEL, WHALE_TRANSFERS_CHANNEL):
            if channel == NEW_BLOCK_CHANNEL:
                await queue.put(("head", await build_head_event_payload(service, payload)))
            elif channel == WHALE_TRANSFERS_CHANNEL:
                await queue.put(("whale", normalize_whale_event_payload(payload)))

    async def emit_status() -> None:
        while True:
            await queue.put(("status", await query_status_snapshot(service)))
            await asyncio.sleep(status_interval_seconds)

    redis_task = asyncio.create_task(forward_redis(), name="polygon-sse-forward-redis")
    status_task = asyncio.create_task(emit_status(), name="polygon-sse-status")
    try:
        latest_blocks = await query_recent_blocks(service, 1)
        if latest_blocks:
            yield format_sse_event("head", await build_head_event_payload(service, latest_blocks[0]))
        while True:
            event, payload = await queue.get()
            yield format_sse_event(event, payload)
    finally:
        redis_task.cancel()
        status_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await redis_task
        with contextlib.suppress(asyncio.CancelledError):
            await status_task


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings("analytics")
    configure_logging(settings.log_level)
    service = AnalyticsService(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.startup()
        try:
            yield
        finally:
            await service.shutdown()

    app = FastAPI(title="Polygon Analytics", version="1.0.0", lifespan=lifespan)
    app.state.analytics_service = service

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started_at = perf_counter()
        response = await call_next(request)
        duration_ms = (perf_counter() - started_at) * 1000
        LOGGER.info(
            "analytics request method=%s path=%s status=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.get("/health")
    async def health() -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        payload = {
            "service": "analytics",
            "latest_block": latest_block,
            "balance_updater_block": await service.state.get_int("balance_updater_block", 0),
            "ohlcv_builder_block": await service.state.get_int("ohlcv_builder_block", 0),
            "whale_detector_block": await service.state.get_int("whale_detector_block", 0),
        }
        return envelope(_jsonify(payload), block=latest_block)

    @app.get("/v1/polygon/overview")
    async def polygon_overview() -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        overview = await query_overview(service)
        return envelope(_jsonify(overview), block=latest_block)

    @app.get("/v1/polygon/blocks/recent")
    async def polygon_recent_blocks(limit: int = Query(default=40, ge=1, le=100)) -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        rows = await query_recent_blocks(service, limit)
        return envelope(_jsonify(rows), block=latest_block)

    @app.get("/v1/polygon/tokens")
    async def list_tokens() -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        rows = await service.query_all(
            """
            SELECT
                t.contract_address,
                t.symbol,
                t.name,
                t.decimals,
                t.total_supply,
                t.first_seen_block,
                hs.total_holders,
                hs.top10_concentration,
                hs.top50_concentration,
                hs.gini_coefficient
            FROM polygon_tokens t
            LEFT JOIN LATERAL (
                SELECT total_holders, top10_concentration, top50_concentration, gini_coefficient
                FROM polygon_holder_snapshots hs
                WHERE hs.contract_address = t.contract_address
                ORDER BY snapshot_date DESC
                LIMIT 1
            ) hs ON true
            WHERE t.is_tracked = true
            ORDER BY t.symbol, t.contract_address
            """
        )
        for row in rows:
            row["top10_concentration_pct"] = _ratio_to_pct(row.get("top10_concentration"))
            row["top50_concentration_pct"] = _ratio_to_pct(row.get("top50_concentration"))
        return envelope(_jsonify(rows), block=latest_block)

    @app.get("/v1/polygon/tokens/{address}")
    async def token_detail(address: str) -> dict[str, Any]:
        contract = normalize_address(address)
        latest_block = await service.get_latest_block()
        row = await service.query_one(
            """
            SELECT
                t.contract_address,
                t.symbol,
                t.name,
                t.decimals,
                t.total_supply,
                t.first_seen_block,
                hs.total_holders,
                hs.top10_concentration,
                hs.top50_concentration,
                hs.gini_coefficient,
                (
                    SELECT count(*)
                    FROM polygon_token_transfers tr
                    WHERE tr.contract_address = t.contract_address
                    AND tr.timestamp >= now() - interval '24 hours'
                ) AS transfers_24h
            FROM polygon_tokens t
            LEFT JOIN LATERAL (
                SELECT total_holders, top10_concentration, top50_concentration, gini_coefficient
                FROM polygon_holder_snapshots hs
                WHERE hs.contract_address = t.contract_address
                ORDER BY snapshot_date DESC
                LIMIT 1
            ) hs ON true
            WHERE t.contract_address = :contract
            """,
            {"contract": contract},
        )
        prices = await service.load_latest_prices()
        if row:
            price = prices.get(contract)
            row = dict(row)
            row["latest_price"] = format(price.price, "f") if price else None
            row["quote_symbol"] = price.quote_symbol if price else None
            row["top10_concentration_pct"] = _ratio_to_pct(row.get("top10_concentration"))
            row["top50_concentration_pct"] = _ratio_to_pct(row.get("top50_concentration"))
        return envelope(_jsonify(row or {}), block=latest_block)

    @app.get("/v1/polygon/tokens/{address}/holders")
    async def token_holders(address: str, limit: int = Query(default=100, le=500)) -> dict[str, Any]:
        contract = normalize_address(address)
        latest_block = await service.get_latest_block()
        token_meta = await service.query_one(
            """
            SELECT decimals
            FROM polygon_tokens
            WHERE contract_address = :contract
            """,
            {"contract": contract},
        )
        tracked_total_row = await service.query_one(
            """
            SELECT COALESCE(sum(balance), 0) AS tracked_total_balance
            FROM polygon_token_balances
            WHERE contract_address = :contract
            AND balance > 0
            """,
            {"contract": contract},
        )
        holders = await service.query_all(
            """
            SELECT wallet_address, balance, last_updated_block, last_updated_at
            FROM polygon_token_balances
            WHERE contract_address = :contract
            AND balance > 0
            ORDER BY balance DESC
            LIMIT :limit
            """,
            {"contract": contract, "limit": limit},
        )
        snapshot = await service.query_one(
            """
            SELECT snapshot_date, total_holders, top10_concentration, top50_concentration, gini_coefficient
            FROM polygon_holder_snapshots
            WHERE contract_address = :contract
            ORDER BY snapshot_date DESC
            LIMIT 1
            """,
            {"contract": contract},
        )
        decimals = int((token_meta or {}).get("decimals") or 18)
        tracked_total_balance = Decimal((tracked_total_row or {}).get("tracked_total_balance") or 0)
        holders = build_holder_rows(holders, decimals=decimals, tracked_total_balance=tracked_total_balance)
        if snapshot:
            snapshot = dict(snapshot)
            snapshot["top10_concentration_pct"] = _ratio_to_pct(snapshot.get("top10_concentration"))
            snapshot["top50_concentration_pct"] = _ratio_to_pct(snapshot.get("top50_concentration"))
        return envelope(_jsonify({"holders": holders, "snapshot": snapshot}), block=latest_block)

    @app.get("/v1/polygon/tokens/{address}/transfers")
    async def token_transfers(address: str, since: str | None = None, limit: int = Query(default=100, le=500)) -> dict[str, Any]:
        contract = normalize_address(address)
        latest_block = await service.get_latest_block()
        since_dt = parse_iso8601(since) or (utcnow() - timedelta(days=1))
        rows = await service.query_all(
            """
            SELECT block_number, tx_hash, log_index, from_address, to_address, value, timestamp
            FROM polygon_token_transfers
            WHERE contract_address = :contract
            AND timestamp >= :since_dt
            ORDER BY timestamp DESC
            LIMIT :limit
            """,
            {"contract": contract, "since_dt": since_dt, "limit": limit},
        )
        return envelope(_jsonify(rows), block=latest_block)

    @app.get("/v1/polygon/balances/{wallet}")
    async def wallet_balances(wallet: str) -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        rows = await service.query_all(
            """
            SELECT
                b.wallet_address,
                b.contract_address,
                b.balance,
                b.last_updated_block,
                b.last_updated_at,
                t.symbol,
                t.name,
                t.decimals
            FROM polygon_token_balances b
            JOIN polygon_tokens t ON t.contract_address = b.contract_address
            WHERE b.wallet_address = :wallet
            ORDER BY b.balance DESC
            """,
            {"wallet": normalize_address(wallet)},
        )
        return envelope(_jsonify(rows), block=latest_block)

    @app.get("/v1/polygon/dex/pools")
    async def dex_pools() -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        rows = await service.query_all(
            """
            SELECT
                p.pool_address,
                p.dex_name,
                p.token0_address,
                p.token1_address,
                p.fee_tier,
                COALESCE(t0.symbol, 'TOKEN0') AS token0_symbol,
                COALESCE(t1.symbol, 'TOKEN1') AS token1_symbol
            FROM polygon_dex_pools p
            LEFT JOIN polygon_tokens t0 ON t0.contract_address = p.token0_address
            LEFT JOIN polygon_tokens t1 ON t1.contract_address = p.token1_address
            WHERE p.is_tracked = true
            ORDER BY p.dex_name, p.pool_address
            """
        )
        return envelope(_jsonify(rows), block=latest_block)

    @app.get("/v1/polygon/dex/price/{token}")
    async def dex_price(token: str, quote: str = "USDC") -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        prices = await service.load_latest_prices(quote=quote)
        price = prices.get(normalize_address(token))
        payload = None
        if price:
            payload = {
                "token_address": price.token_address,
                "quote_symbol": price.quote_symbol,
                "price": price.price,
                "pool_address": price.pool_address,
                "open_time": price.open_time,
            }
        return envelope(_jsonify(payload or {}), block=latest_block)

    @app.get("/v1/polygon/dex/ohlcv/{pool}")
    async def dex_ohlcv(pool: str, interval: str = "1h", limit: int = Query(default=100, le=1000)) -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        rows = await service.query_all(
            """
            SELECT open_time, open, high, low, close, volume, trade_count
            FROM polygon_dex_ohlcv
            WHERE pool_address = :pool
            AND interval = :interval
            ORDER BY open_time DESC
            LIMIT :limit
            """,
            {"pool": normalize_address(pool), "interval": interval, "limit": limit},
        )
        return envelope(_jsonify(rows), block=latest_block)

    @app.get("/v1/polygon/dex/volume/{token}")
    async def dex_volume(token: str, period: str = "24h") -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        window = parse_period(period, timedelta(hours=24))
        interval = "1m" if window <= timedelta(hours=6) else "1h" if window <= timedelta(days=3) else "1d"
        since_dt = utcnow() - window
        row = await service.query_one(
            """
            SELECT COALESCE(sum(o.volume), 0) AS volume
            FROM polygon_dex_ohlcv o
            JOIN polygon_dex_pools p ON p.pool_address = o.pool_address
            WHERE o.interval = :interval
            AND o.open_time >= :since_dt
            AND (p.token0_address = :token OR p.token1_address = :token)
            """,
            {"interval": interval, "since_dt": since_dt, "token": normalize_address(token)},
        )
        return envelope(_jsonify({"token_address": normalize_address(token), "period": period, "volume": row["volume"] if row else 0}), block=latest_block)

    @app.get("/v1/polygon/gas")
    async def gas_now() -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        row = await service.query_one(
            """
            SELECT
                g.block_number,
                g.base_fee_gwei,
                g.gas_used_pct,
                g.tx_count,
                g.timestamp,
                (
                    SELECT avg(base_fee_gwei)
                    FROM polygon_gas_metrics
                    WHERE block_number >= g.block_number - 20
                ) AS avg_base_fee_20
            FROM polygon_gas_metrics g
            ORDER BY g.block_number DESC
            LIMIT 1
            """
        )
        return envelope(_jsonify(row or {}), block=latest_block)

    @app.get("/v1/polygon/gas/history")
    async def gas_history(hours: int = Query(default=24, ge=1, le=168)) -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        since_dt = utcnow() - timedelta(hours=hours)
        rows = await service.query_all(
            """
            SELECT block_number, base_fee_gwei, gas_used_pct, tx_count, timestamp
            FROM polygon_gas_metrics
            WHERE timestamp >= :since_dt
            ORDER BY block_number DESC
            """,
            {"since_dt": since_dt},
        )
        return envelope(_jsonify(rows), block=latest_block)

    @app.get("/v1/polygon/events/{contract}")
    async def contract_events(contract: str, type: str | None = None, since: str | None = None) -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        since_dt = parse_iso8601(since) or (utcnow() - timedelta(days=7))
        sql = """
            SELECT block_number, tx_hash, log_index, event_type, event_data, timestamp
            FROM polygon_contract_events
            WHERE contract_address = :contract
            AND timestamp >= :since_dt
        """
        params: dict[str, Any] = {"contract": normalize_address(contract), "since_dt": since_dt}
        if type:
            sql += " AND event_type = :event_type"
            params["event_type"] = type
        sql += " ORDER BY timestamp DESC LIMIT 500"
        rows = await service.query_all(sql, params)
        return envelope(_jsonify(rows), block=latest_block)

    @app.get("/v1/polygon/whale-transfers")
    async def whale_transfers(
        since: str | None = None,
        min_usd: float = 10_000.0,
        limit: int = Query(default=30, ge=1, le=500),
    ) -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        since_dt = parse_iso8601(since) or (utcnow() - timedelta(days=1))
        whales = await query_whale_transfers(service, since_dt=since_dt, min_usd=min_usd, limit=limit)
        return envelope(_jsonify(whales), block=latest_block)

    @app.get("/v1/polygon/status")
    async def status() -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        payload = await query_status_snapshot(service)
        return envelope(_jsonify(payload), block=latest_block)

    @app.get("/v1/polygon/stream")
    async def polygon_stream() -> StreamingResponse:
        return StreamingResponse(
            stream_sse_events(service),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Any

from fastapi import FastAPI, Query, Request
from sqlalchemy import text

from src.shared.config import Settings, load_settings
from src.shared.db import Database, StateStore
from src.shared.events import INTERVAL_SECONDS, QUOTE_SYMBOLS, WHALE_TRANSFERS_CHANNEL
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
            usd_value = normalized * price.price if price else Decimal(0)
            if usd_value < Decimal(str(self.settings.whale_threshold_usd)):
                continue
            await self.redis.publish_json(
                WHALE_TRANSFERS_CHANNEL,
                {
                    "block_number": row["block_number"],
                    "tx_hash": row["tx_hash"],
                    "contract_address": normalize_address(row["contract_address"]),
                    "from_address": normalize_address(row["from_address"]),
                    "to_address": normalize_address(row["to_address"]),
                    "amount": format(normalized, "f"),
                    "usd_value": format(usd_value, "f"),
                    "timestamp": to_iso8601(row["timestamp"]),
                    "symbol": row["symbol"],
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
        return envelope(_jsonify(row or {}), block=latest_block)

    @app.get("/v1/polygon/tokens/{address}/holders")
    async def token_holders(address: str, limit: int = Query(default=100, le=500)) -> dict[str, Any]:
        contract = normalize_address(address)
        latest_block = await service.get_latest_block()
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
    async def whale_transfers(since: str | None = None, min_usd: float = 10_000.0) -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        since_dt = parse_iso8601(since) or (utcnow() - timedelta(days=1))
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
            LIMIT 2000
            """,
            {"since_dt": since_dt},
        )
        prices = await service.load_latest_prices()
        whales = []
        for row in rows:
            price = prices.get(normalize_address(row["contract_address"]))
            if not price:
                continue
            amount = units_to_decimal(int(row["value"]), int(row["decimals"]))
            usd_value = amount * price.price
            if usd_value < Decimal(str(min_usd)):
                continue
            whales.append(
                {
                    "block_number": row["block_number"],
                    "tx_hash": row["tx_hash"],
                    "contract_address": normalize_address(row["contract_address"]),
                    "symbol": row["symbol"],
                    "from_address": normalize_address(row["from_address"]),
                    "to_address": normalize_address(row["to_address"]),
                    "amount": amount,
                    "usd_value": usd_value,
                    "timestamp": row["timestamp"],
                    "quote_symbol": price.quote_symbol,
                }
            )
        whales.sort(key=lambda item: Decimal(item["usd_value"]), reverse=True)
        return envelope(_jsonify(whales[:200]), block=latest_block)

    @app.get("/v1/polygon/status")
    async def status() -> dict[str, Any]:
        latest_block = await service.get_latest_block()
        chain_head = await service.current_chain_head()
        payload = {
            "last_indexed_block": latest_block,
            "last_decoded_block": await service.state.get_int("last_decoded_block", 0),
            "last_analytics_block": await service.state.get_int("last_analytics_block", 0),
            "chain_head": chain_head,
            "lag_blocks": (chain_head - latest_block) if chain_head is not None else None,
            "backfill_complete": await service.state.get_bool("backfill_complete", False),
            "backfill_start_block": await service.state.get_int("backfill_start_block", 0),
        }
        return envelope(_jsonify(payload), block=latest_block)

    return app

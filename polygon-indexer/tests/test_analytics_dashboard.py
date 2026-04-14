from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.analytics.app import (
    AnalyticsService,
    PricePoint,
    build_holder_rows,
    create_app,
    format_sse_event,
    query_overview,
    query_recent_blocks,
    query_whale_transfers,
)
from src.shared.config import load_settings


class AnalyticsDashboardHelpersTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_recent_blocks_returns_desc_order(self) -> None:
        service = object.__new__(AnalyticsService)
        service.query_all = AsyncMock(
            return_value=[
                {
                    "block_number": 85467230,
                    "timestamp": datetime(2026, 4, 14, 12, 0, tzinfo=UTC),
                    "tx_count": 80,
                    "transfer_count": 21,
                    "swap_count": 3,
                    "gas_used_pct": Decimal("70.0"),
                },
                {
                    "block_number": 85467231,
                    "timestamp": datetime(2026, 4, 14, 12, 2, tzinfo=UTC),
                    "tx_count": 87,
                    "transfer_count": 23,
                    "swap_count": 4,
                    "gas_used_pct": Decimal("72.3"),
                },
            ]
        )

        rows = await query_recent_blocks(service, 40)

        self.assertEqual([row["block_number"] for row in rows], [85467231, 85467230])
        self.assertEqual(rows[0]["gas_used_pct"], 72.3)

    async def test_query_whale_transfers_orders_desc_and_enriches_fields(self) -> None:
        contract = "0x1111111111111111111111111111111111111111"
        service = object.__new__(AnalyticsService)
        service.query_all = AsyncMock(
            return_value=[
                {
                    "block_number": 10,
                    "tx_hash": "0x" + "12" * 32,
                    "contract_address": contract,
                    "from_address": "0x" + "aa" * 20,
                    "to_address": "0x" + "bb" * 20,
                    "value": Decimal("15000000000"),
                    "timestamp": datetime(2026, 4, 14, 12, 0, tzinfo=UTC),
                    "symbol": "USDC",
                    "decimals": 6,
                },
                {
                    "block_number": 11,
                    "tx_hash": "0x" + "34" * 32,
                    "contract_address": contract,
                    "from_address": "0x" + "cc" * 20,
                    "to_address": "0x" + "dd" * 20,
                    "value": Decimal("20000000000"),
                    "timestamp": datetime(2026, 4, 14, 12, 5, tzinfo=UTC),
                    "symbol": "USDC",
                    "decimals": 6,
                },
            ]
        )
        service.load_latest_prices = AsyncMock(
            return_value={
                contract: PricePoint(
                    token_address=contract,
                    quote_symbol="USDC",
                    price=Decimal("1"),
                    pool_address="0x" + "99" * 20,
                    open_time=datetime(2026, 4, 14, 11, 0, tzinfo=UTC),
                )
            }
        )

        whales = await query_whale_transfers(
            service,
            since_dt=datetime(2026, 4, 13, 12, 0, tzinfo=UTC),
            min_usd=10_000,
            limit=30,
        )

        self.assertEqual([row["block_number"] for row in whales], [11, 10])
        self.assertEqual(whales[0]["token_symbol"], "USDC")
        self.assertEqual(whales[0]["token_decimals"], 6)
        self.assertEqual(whales[0]["amount_human"], "20000")
        self.assertEqual(whales[0]["usd_value"], 20000.0)

    async def test_query_overview_includes_frozen_fields(self) -> None:
        contract = "0x2222222222222222222222222222222222222222"
        service = object.__new__(AnalyticsService)
        service.settings = SimpleNamespace(whale_threshold_usd=10_000.0)
        service.query_all = AsyncMock(
            side_effect=[
                [
                    {
                        "block_number": 120,
                        "base_fee_gwei": Decimal("32.5"),
                        "gas_used_pct": Decimal("72.3"),
                        "tx_count": 87,
                        "timestamp": datetime(2026, 4, 14, 12, 6, tzinfo=UTC),
                    },
                    {
                        "block_number": 119,
                        "base_fee_gwei": Decimal("30.2"),
                        "gas_used_pct": Decimal("68.1"),
                        "tx_count": 79,
                        "timestamp": datetime(2026, 4, 14, 12, 4, tzinfo=UTC),
                    },
                    {
                        "block_number": 118,
                        "base_fee_gwei": Decimal("28.1"),
                        "gas_used_pct": Decimal("64.2"),
                        "tx_count": 70,
                        "timestamp": datetime(2026, 4, 14, 12, 2, tzinfo=UTC),
                    },
                ],
                [
                    {
                        "contract_address": contract,
                        "symbol": "USDC",
                        "name": "USD Coin",
                        "decimals": 6,
                        "snapshot_date": datetime(2026, 4, 14, 0, 0, tzinfo=UTC).date(),
                        "total_holders": 42000,
                        "top10_concentration": Decimal("0.623"),
                        "top50_concentration": Decimal("0.812"),
                        "gini_coefficient": Decimal("0.87"),
                        "recent_activity_1h": 1200,
                        "transfers_24h": 142000,
                    }
                ],
                [{"contract_address": contract, "value": Decimal("15000000000")}],
            ]
        )
        service.load_latest_prices = AsyncMock(
            return_value={
                contract: PricePoint(
                    token_address=contract,
                    quote_symbol="USDC",
                    price=Decimal("1"),
                    pool_address="0x" + "88" * 20,
                    open_time=datetime(2026, 4, 14, 11, 0, tzinfo=UTC),
                )
            }
        )

        recent_blocks = [
            {
                "block_number": 120,
                "timestamp": datetime(2026, 4, 14, 12, 6, tzinfo=UTC),
                "tx_count": 87,
                "transfer_count": 23,
                "swap_count": 4,
                "gas_used_pct": 72.3,
            },
            {
                "block_number": 119,
                "timestamp": datetime(2026, 4, 14, 12, 4, tzinfo=UTC),
                "tx_count": 79,
                "transfer_count": 21,
                "swap_count": 3,
                "gas_used_pct": 68.1,
            },
            {
                "block_number": 118,
                "timestamp": datetime(2026, 4, 14, 12, 2, tzinfo=UTC),
                "tx_count": 70,
                "transfer_count": 20,
                "swap_count": 2,
                "gas_used_pct": 64.2,
            },
        ]
        status_snapshot = {
            "chain_head": 123,
            "last_indexed_block": 120,
            "last_decoded_block": 120,
            "last_analytics_block": 120,
            "last_indexed": 120,
            "lag_blocks": 3,
            "lag": 3,
            "backfill_complete": True,
            "backfill_start_block": 1,
            "backfill_pct": 100.0,
            "total_blocks_indexed": 120,
            "total_transfers_indexed": 26_000_000,
            "total_events_indexed": 1500,
            "tracked_token_count": 1,
            "last_updated_at": datetime(2026, 4, 14, 12, 6, tzinfo=UTC),
        }

        with patch("src.analytics.app.query_recent_blocks", AsyncMock(return_value=recent_blocks)), patch(
            "src.analytics.app.query_status_snapshot", AsyncMock(return_value=status_snapshot)
        ):
            overview = await query_overview(service)

        token = overview["tokens"][0]
        self.assertIn("gas_percentile_rank", overview)
        self.assertEqual(overview["gas_history_100_blocks"], [28.1, 30.2, 32.5])
        self.assertEqual(overview["total_transfers_indexed"], 26_000_000)
        self.assertEqual(token["latest_price"], "1")
        self.assertEqual(token["price_quote"], "USDC")
        self.assertEqual(token["recent_activity_1h"], 1200)
        self.assertEqual(token["recent_activity_24h"], 142000)
        self.assertEqual(token["holder_snapshot"]["top10_concentration_pct"], 62.3)

    def test_build_holder_rows_adds_balance_human_and_pct(self) -> None:
        holders = build_holder_rows(
            [
                {
                    "wallet_address": "0x" + "aa" * 20,
                    "balance": Decimal("15000000000000"),
                    "last_updated_block": 100,
                    "last_updated_at": datetime(2026, 4, 14, 12, 0, tzinfo=UTC),
                }
            ],
            decimals=6,
            tracked_total_balance=Decimal("122000000000000"),
        )

        self.assertEqual(holders[0]["balance_human"], "15000000")
        self.assertEqual(holders[0]["pct_of_tracked"], 12.3)


class AnalyticsDashboardRoutesTest(unittest.TestCase):
    def _make_client(self) -> TestClient:
        app = create_app(load_settings("analytics"))
        service = app.state.analytics_service
        service.startup = AsyncMock()
        service.shutdown = AsyncMock()
        return TestClient(app)

    def test_overview_route_returns_dashboard_payload(self) -> None:
        overview_payload = {
            "total_transfers_indexed": 26_000_000,
            "last_updated_at": "2026-04-14T12:06:00Z",
            "gas_percentile_rank": 0.45,
            "gas_history_100_blocks": [28.1, 30.2, 32.5],
            "tokens": [],
        }

        with self._make_client() as client, patch(
            "src.analytics.app.query_overview", AsyncMock(return_value=overview_payload)
        ):
            client.app.state.analytics_service.get_latest_block = AsyncMock(return_value=120)
            response = client.get("/v1/polygon/overview")

        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["gas_percentile_rank"], 0.45)
        self.assertEqual(body["data"]["total_transfers_indexed"], 26_000_000)

    def test_stream_route_emits_head_whale_and_status(self) -> None:
        async def fake_stream(_service: AnalyticsService, *, status_interval_seconds: float = 10.0):
            del status_interval_seconds
            yield format_sse_event("head", {"block_number": 120})
            yield format_sse_event("whale", {"tx_hash": "0x" + "12" * 32})
            yield format_sse_event("status", {"last_indexed_block": 120})

        with self._make_client() as client, patch("src.analytics.app.stream_sse_events", fake_stream):
            with client.stream("GET", "/v1/polygon/stream") as response:
                payload = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: head", payload)
        self.assertIn("event: whale", payload)
        self.assertIn("event: status", payload)


if __name__ == "__main__":
    unittest.main()

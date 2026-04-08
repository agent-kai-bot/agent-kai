"""Unit tests for agent-k.ai market data helpers."""

import unittest
from datetime import timezone

from data_api.agent_kai_client import (
    aggregate_bars,
    channel_symbol_and_interval,
    event_to_bar,
    normalize_symbol,
    rows_to_bars,
)


class AgentKaiClientHelperTests(unittest.TestCase):
    """Validate upstream payload translation logic."""

    def test_normalize_symbol_strips_quote_suffixes(self):
        """Quote-denominated symbols should normalize to the base ticker."""
        self.assertEqual(normalize_symbol("BTCUSDT"), "BTC")
        self.assertEqual(normalize_symbol("eth-usd"), "ETH")
        self.assertEqual(normalize_symbol("SOL"), "SOL")

    def test_rows_to_bars_sorts_rows_and_converts_schema(self):
        """REST OHLCV rows should become sorted local bar dictionaries."""
        bars = rows_to_bars(
            "BTCUSDT",
            "1m",
            [
                [2_000, 11, 13, 10, 12, 150],
                [1_000, 10, 12, 9, 11, 100],
            ],
        )

        self.assertEqual([bar["close"] for bar in bars], [11.0, 12.0])
        self.assertEqual(bars[0]["symbol"], "BTC")
        self.assertEqual(bars[0]["interval"], "1m")

    def test_aggregate_bars_builds_6h_candle_from_hourly_rows(self):
        """Hourly rows should aggregate into a single 6h candle."""
        hourly = rows_to_bars(
            "BTC",
            "1h",
            [
                [0, 100, 101, 99, 100.5, 10],
                [3_600_000, 100.5, 103, 100, 102, 11],
                [7_200_000, 102, 104, 101, 103, 12],
                [10_800_000, 103, 105, 102, 104, 13],
                [14_400_000, 104, 106, 103, 105, 14],
                [18_000_000, 105, 107, 104, 106, 15],
            ],
        )

        aggregated = aggregate_bars(hourly, "6h")

        self.assertEqual(len(aggregated), 1)
        self.assertEqual(aggregated[0]["open"], 100.0)
        self.assertEqual(aggregated[0]["high"], 107.0)
        self.assertEqual(aggregated[0]["low"], 99.0)
        self.assertEqual(aggregated[0]["close"], 106.0)
        self.assertEqual(aggregated[0]["volume"], 75.0)

    def test_channel_symbol_and_interval_parses_market_channel(self):
        """Websocket channels should parse into normalized symbol and interval."""
        self.assertEqual(channel_symbol_and_interval("market.BTCUSDT.1m"), ("BTC", "1m"))

    def test_event_to_bar_translates_websocket_payload(self):
        """Live websocket payloads should become local bars."""
        bar = event_to_bar(
            {
                "symbol": "ETHUSDT",
                "interval": "1m",
                "ts": 1_000,
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 99,
                "is_closed": True,
                "source": "bingx",
            }
        )

        self.assertEqual(bar["symbol"], "ETH")
        self.assertEqual(bar["interval"], "1m")
        self.assertEqual(bar["close"], 10.5)
        self.assertTrue(bar["is_closed"])
        self.assertEqual(bar["ts"].tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()

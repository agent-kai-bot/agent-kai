from __future__ import annotations

import unittest
from decimal import Decimal

from src.analytics.app import AnalyticsService


class AnalyticsPriceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = object.__new__(AnalyticsService)

    def test_swap_price_uses_quote_token1(self) -> None:
        price, volume = self.service._swap_price(
            {
                "amount0": 10**18,
                "amount1": 2_500_000,
                "token0_decimals": 18,
                "token1_decimals": 6,
                "token0_symbol": "TOKEN",
                "token1_symbol": "USDC",
            }
        )
        self.assertEqual(price, Decimal("2.5"))
        self.assertEqual(volume, Decimal("2.5"))

    def test_swap_price_inverts_when_quote_is_token0(self) -> None:
        price, volume = self.service._swap_price(
            {
                "amount0": 4_000_000,
                "amount1": 2 * 10**18,
                "token0_decimals": 6,
                "token1_decimals": 18,
                "token0_symbol": "USDC",
                "token1_symbol": "TOKEN",
            }
        )
        self.assertEqual(price, Decimal("2"))
        self.assertEqual(volume, Decimal("4"))


if __name__ == "__main__":
    unittest.main()

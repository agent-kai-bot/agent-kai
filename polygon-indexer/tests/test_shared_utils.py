from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.shared.evm import normalize_address
from src.shared.utils import bucket_start, compute_gini, parse_period


class SharedUtilsTest(unittest.TestCase):
    def test_normalize_address_lowercases_and_pads(self) -> None:
        self.assertEqual(
            normalize_address("0xAbC123"),
            "0x0000000000000000000000000000000000abc123",
        )

    def test_bucket_start_aligns_to_interval(self) -> None:
        timestamp = datetime(2026, 4, 11, 13, 17, 42, tzinfo=UTC)
        self.assertEqual(bucket_start(timestamp, "1h"), datetime(2026, 4, 11, 13, 0, 0, tzinfo=UTC))
        self.assertEqual(bucket_start(timestamp, "15m"), datetime(2026, 4, 11, 13, 15, 0, tzinfo=UTC))

    def test_parse_period_and_gini(self) -> None:
        self.assertEqual(parse_period("24h", timedelta(minutes=1)), timedelta(hours=24))
        gini = compute_gini([Decimal("1"), Decimal("2"), Decimal("3")])
        self.assertIsNotNone(gini)
        self.assertGreater(gini, Decimal("0"))


if __name__ == "__main__":
    unittest.main()


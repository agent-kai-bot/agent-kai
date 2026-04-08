"""Unit tests for technical-analysis helper functions."""

import unittest

import pandas as pd

from agent.crypto_tools import _classify_bbands_position, _extract_bbands_levels


class CryptoToolHelperTests(unittest.TestCase):
    """Validate TA helper logic without network access."""

    def test_extract_bbands_levels_uses_named_columns(self):
        """Bollinger Band extraction should respect lower/mid/upper column names."""
        frame = pd.DataFrame(
            {
                "BBL_20_2.0": [101.0, 102.0],
                "BBM_20_2.0": [111.0, 112.0],
                "BBU_20_2.0": [121.0, 122.0],
            }
        )

        lower, mid, upper = _extract_bbands_levels(frame)

        self.assertEqual((lower, mid, upper), (102.0, 112.0, 122.0))

    def test_classify_bbands_position_reports_upper_band_proximity(self):
        """Price near the upper band should be classified correctly."""
        label = _classify_bbands_position(price=119.0, lower=100.0, mid=110.0, upper=120.0)
        self.assertEqual(label, "near upper")

    def test_classify_bbands_position_reports_lower_band_proximity(self):
        """Price near the lower band should be classified correctly."""
        label = _classify_bbands_position(price=101.0, lower=100.0, mid=110.0, upper=120.0)
        self.assertEqual(label, "near lower")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the canonical ASO sample-size gate."""

import unittest

from agent.strategy_sample_size import check_sample_size


class SampleSizeTests(unittest.TestCase):
    """Validate threshold and overlap edge cases."""

    def test_walk_forward_rejects_49_and_accepts_50(self):
        self.assertEqual(check_sample_size(49, 1.0, 500, "walk_forward"), (False, "Need ≥50 trades (overlap-adjusted), got 49"))
        self.assertEqual(check_sample_size(50, 1.0, 500, "walk_forward"), (True, "ok"))

    def test_overlap_adjustment_raises_threshold_to_100(self):
        self.assertEqual(check_sample_size(99, 10.0, 100, "lockbox"), (False, "Need ≥100 trades (overlap-adjusted), got 99"))
        self.assertEqual(check_sample_size(100, 10.0, 100, "lockbox"), (True, "ok"))

    def test_shadow_uses_lower_threshold(self):
        self.assertEqual(check_sample_size(9, 1.0, 50, "shadow"), (False, "Shadow needs ≥10 trades, got 9"))
        self.assertEqual(check_sample_size(10, 1.0, 50, "shadow"), (True, "ok"))

    def test_zero_trades_rejects_cleanly(self):
        self.assertEqual(check_sample_size(0, 0.0, 100, "walk_forward"), (False, "Need ≥50 trades (overlap-adjusted), got 0"))

    def test_non_overlapping_trades_do_not_trigger_cluster_penalty(self):
        self.assertEqual(check_sample_size(60, 0.4, 100, "walk_forward"), (True, "ok"))


if __name__ == "__main__":
    unittest.main()

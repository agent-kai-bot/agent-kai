"""Unit tests for chart workspace layout modes."""

import unittest

from tui.chart_modes import (
    chart_layout_choices,
    cycle_chart_layout_mode,
    normalize_chart_layout_mode,
)


class ChartLayoutModeTests(unittest.TestCase):
    """Validate chart workspace mode helpers."""

    def test_normalize_chart_layout_mode_supports_aliases(self):
        """Legacy and friendly aliases should normalize to stable modes."""

        self.assertEqual(normalize_chart_layout_mode("full"), "dashboard")
        self.assertEqual(normalize_chart_layout_mode("default"), "dashboard")
        self.assertEqual(normalize_chart_layout_mode("detail"), "inspect")
        self.assertEqual(normalize_chart_layout_mode("half"), "chat")
        self.assertEqual(normalize_chart_layout_mode("FOCUS"), "focus")

    def test_normalize_chart_layout_mode_falls_back_to_dashboard(self):
        """Unknown modes should not leak into persisted state."""

        self.assertEqual(normalize_chart_layout_mode("unknown-mode"), "dashboard")
        self.assertEqual(normalize_chart_layout_mode(""), "dashboard")
        self.assertEqual(normalize_chart_layout_mode(None), "dashboard")

    def test_cycle_chart_layout_mode_wraps(self):
        """Cycling should rotate through the configured order."""

        choices = chart_layout_choices()
        self.assertEqual(choices, ["dashboard", "inspect", "focus", "chat"])
        self.assertEqual(cycle_chart_layout_mode("dashboard"), "inspect")
        self.assertEqual(cycle_chart_layout_mode("chat"), "dashboard")


if __name__ == "__main__":
    unittest.main()

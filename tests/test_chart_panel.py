"""Unit tests for chart viewport and aggregation helpers."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tui.panels.chart import (
    ChartPanel,
    ChartViewportState,
    aggregate_view_bars,
    build_view_window,
)


def _sample_bars(count: int) -> list[dict]:
    """Build deterministic sample OHLCV bars."""

    start = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
    bars = []
    price = 100.0
    for index in range(count):
        open_price = price
        close_price = price + (1.0 if index % 2 == 0 else -0.5)
        high = max(open_price, close_price) + 0.4
        low = min(open_price, close_price) - 0.3
        volume = 10.0 + index
        bars.append(
            {
                "ts": start + timedelta(minutes=index),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": volume,
            }
        )
        price = close_price
    return bars


class ChartPanelDataTests(unittest.TestCase):
    """Validate the viewport math that drives the chart renderer."""

    def test_chart_panel_is_focusable(self):
        """The chart must participate in focus traversal for arrow-key UX."""

        panel = ChartPanel()
        self.assertTrue(panel.can_focus)

    def test_aggregate_view_bars_preserves_ohlcv_semantics(self):
        """Aggregation should preserve open/high/low/close/volume correctly."""

        bars = _sample_bars(4)
        aggregated = aggregate_view_bars(bars, factor=2)

        self.assertEqual(len(aggregated), 2)
        self.assertEqual(aggregated[0]["ts"], bars[0]["ts"])
        self.assertEqual(aggregated[0]["open"], bars[0]["open"])
        self.assertEqual(aggregated[0]["close"], bars[1]["close"])
        self.assertEqual(
            aggregated[0]["high"],
            max(bars[0]["high"], bars[1]["high"]),
        )
        self.assertEqual(
            aggregated[0]["low"],
            min(bars[0]["low"], bars[1]["low"]),
        )
        self.assertEqual(
            aggregated[0]["volume"],
            bars[0]["volume"] + bars[1]["volume"],
        )

    def test_build_view_window_applies_right_offset(self):
        """The visible slice should move left when the viewport pans."""

        bars = _sample_bars(40)
        viewport = ChartViewportState(zoom_index=3, right_offset=5)
        view = build_view_window(bars, plot_width=10, viewport=viewport)

        self.assertEqual(len(view.bars), 10)
        self.assertEqual(view.capacity, 10)
        self.assertEqual(view.aggregated_count, 40)
        self.assertEqual(view.hidden_right, 5)
        self.assertEqual(view.hidden_left, 25)
        self.assertEqual(view.bars[-1]["ts"], bars[-6]["ts"])

    def test_build_view_window_uses_zoom_aggregation(self):
        """Zoomed-out profiles should aggregate multiple raw bars."""

        bars = _sample_bars(24)
        viewport = ChartViewportState(zoom_index=1, right_offset=0)
        view = build_view_window(bars, plot_width=10, viewport=viewport)

        self.assertEqual(view.zoom_level.aggregation, 4)
        self.assertEqual(view.aggregated_count, 6)
        self.assertEqual(len(view.bars), 6)

    def test_chart_panel_exposes_viewport_controls(self):
        """The chart widget should provide stateful zoom and pan controls."""

        panel = ChartPanel()
        panel.set_layout_mode("focus")
        panel.set_source("kai-api")
        panel.set_data("BTC", "1m", _sample_bars(80))

        state = panel.get_view_state(plot_width=30)
        self.assertEqual(state["layout_mode"], "focus")
        self.assertEqual(state["right_offset"], 0)
        self.assertTrue(state["show_volume"])

        panned = panel.pan_left(12)
        self.assertEqual(panned["right_offset"], 12)

        latest = panel.pan_to_latest()
        self.assertEqual(latest["right_offset"], 0)

        toggled = panel.toggle_volume(False)
        self.assertFalse(toggled["show_volume"])

        zoomed = panel.zoom_in()
        self.assertIn(zoomed["zoom"], {"2col", "2+1"})

        reset = panel.reset_view()
        self.assertEqual(reset["right_offset"], 0)
        self.assertTrue(reset["show_volume"])

    def test_chart_panel_action_helpers_mutate_view_state(self):
        """Widget actions should drive the same viewport controls as commands."""

        panel = ChartPanel()
        panel.set_data("BTC", "1m", _sample_bars(80))

        panel.action_pan_left()
        self.assertGreater(panel.get_view_state(plot_width=30)["right_offset"], 0)

        panel.action_pan_to_latest()
        self.assertEqual(panel.get_view_state(plot_width=30)["right_offset"], 0)

        before_zoom = panel.get_view_state(plot_width=30)["zoom"]
        panel.action_zoom_in()
        after_zoom = panel.get_view_state(plot_width=30)["zoom"]
        self.assertNotEqual(before_zoom, after_zoom)

        before_volume = panel.get_view_state(plot_width=30)["show_volume"]
        panel.action_toggle_volume()
        self.assertNotEqual(
            before_volume,
            panel.get_view_state(plot_width=30)["show_volume"],
        )


if __name__ == "__main__":
    unittest.main()

"""Tests for agent-facing chart-view tools."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest import mock

from agent.tools import create_chart_view_tools, create_watchlist_tools
from daemon.core import Session


class ChartViewToolTests(unittest.TestCase):
    """Validate chart tools mutate the bound daemon session."""

    def test_chart_view_tools_read_and_update_session_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with mock.patch.multiple(
                "daemon.core",
                SESSIONS_ROOT_DIR=base_dir,
                SESSION_INDEX_PATH=base_dir / "index.json",
            ):
                session = Session("alpha")
                tool_map = {
                    tool.name: tool
                    for tool in create_chart_view_tools(session)
                }

                updated = tool_map["set_chart_view"].invoke(
                    {
                        "symbol": "eth",
                        "timeframe": "15m",
                        "source": "coinbase",
                        "mode": "mini",
                    }
                )
                current = tool_map["get_chart_view"].invoke({})

                self.assertEqual(updated["chart_symbol"], "ETH")
                self.assertEqual(updated["chart_timeframe"], "15m")
                self.assertEqual(current, updated)

    def test_watchlist_tools_read_and_update_session_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            with mock.patch.multiple(
                "daemon.core",
                SESSIONS_ROOT_DIR=base_dir,
                SESSION_INDEX_PATH=base_dir / "index.json",
            ):
                session = Session("alpha")
                tool_map = {
                    tool.name: tool
                    for tool in create_watchlist_tools(session)
                }

                added = tool_map["add_watchlist_symbol"].invoke({"symbol": "bio"})
                removed = tool_map["remove_watchlist_symbol"].invoke({"symbol": "BTC"})
                replaced = tool_map["set_watchlist"].invoke(
                    {"symbols": ["eth", "bio", "ETH"]}
                )
                current = tool_map["get_watchlist"].invoke({})

                self.assertIn("BIO", added["watchlist_symbols"])
                self.assertNotIn("BTC", removed["watchlist_symbols"])
                self.assertEqual(replaced["watchlist_symbols"], ["ETH", "BIO"])
                self.assertEqual(current, replaced)


if __name__ == "__main__":
    unittest.main()

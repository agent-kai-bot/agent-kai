"""Tests for autonomous-mode tool policy classification."""

from __future__ import annotations

import unittest

from agent.tool_policy import get_tool_policy, is_auto_safe, is_readonly


class ToolPolicyTests(unittest.TestCase):
    """Validate central tool policy registry behavior."""

    def test_host_mutation_tools_require_approval(self):
        file_write = get_tool_policy("file_write")
        shell_exec = get_tool_policy("shell_exec")

        self.assertFalse(file_write.read_only)
        self.assertTrue(file_write.persistent)
        self.assertTrue(file_write.requires_approval_in_auto)

        self.assertFalse(shell_exec.read_only)
        self.assertTrue(shell_exec.persistent)
        self.assertTrue(shell_exec.requires_approval_in_auto)

    def test_frontier_and_external_tools_are_flagged(self):
        codex_exec = get_tool_policy("codex_exec")
        place_order = get_tool_policy("place_order")
        spawn_agent = get_tool_policy("spawn_agent")

        self.assertTrue(codex_exec.external_side_effects)
        self.assertTrue(codex_exec.long_running)
        self.assertTrue(codex_exec.requires_approval_in_auto)

        self.assertTrue(place_order.external_side_effects)
        self.assertTrue(place_order.requires_approval_in_auto)

        self.assertTrue(spawn_agent.external_side_effects)
        self.assertTrue(spawn_agent.requires_approval_in_auto)

    def test_taskboard_submit_review_verdict_is_persistent_write(self):
        policy = get_tool_policy("taskboard_submit_review_verdict")

        self.assertFalse(policy.read_only)
        self.assertTrue(policy.persistent)
        self.assertTrue(policy.external_side_effects)
        self.assertTrue(policy.requires_approval_in_auto)

    def test_read_only_tools_are_safe(self):
        self.assertTrue(is_readonly("query_ohlcv"))
        self.assertTrue(is_readonly("get_signals"))
        self.assertTrue(is_readonly("run_backtest"))
        self.assertTrue(is_readonly("list_strategies"))
        self.assertTrue(is_readonly("show_strategy"))
        self.assertTrue(is_readonly("optimizer_status"))
        self.assertTrue(is_auto_safe("get_positions"))

    def test_aliases_resolve_to_real_tools(self):
        self.assertTrue(is_readonly("get_ohlcv"))
        self.assertTrue(is_readonly("get_portfolio"))

    def test_skill_and_memory_writes_are_not_read_only(self):
        self.assertFalse(is_readonly("memory"))
        self.assertFalse(is_readonly("skill_manage"))
        self.assertTrue(is_readonly("skills_list"))
        self.assertTrue(is_readonly("skill_view"))

    def test_unknown_tools_default_to_read_only_without_auto_approval(self):
        policy = get_tool_policy("unknown_tool_name")

        self.assertEqual(policy.name, "unknown_tool_name")
        self.assertTrue(policy.read_only)
        self.assertTrue(is_auto_safe("unknown_tool_name"))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for prompt composition helpers."""

import unittest

from agent.prompts import build_main_system_prompt, build_sub_agent_system_prompt


class PromptBuilderTests(unittest.TestCase):
    """Validate prompt composition behavior."""

    def test_build_main_system_prompt_includes_base_and_role(self):
        """Main prompts should include shared instructions plus the role prompt."""
        role_prompt = "# Analyst Agent\nUse RSI and MACD."
        prompt = build_main_system_prompt(role_prompt)

        self.assertIn("You are KAI, a crypto trading AI assistant", prompt)
        self.assertIn("Role-specific instructions:", prompt)
        self.assertIn(role_prompt, prompt)

    def test_build_sub_agent_system_prompt_includes_workspace_context(self):
        """Sub-agent prompts should include shared, role, and workspace sections."""
        prompt = build_sub_agent_system_prompt(
            "analyst",
            role_prompt="# Analyst Agent\nUse tools.",
            workspace="/tmp/analyst",
        )

        self.assertIn("You are KAI, a crypto trading AI assistant", prompt)
        self.assertIn("specialized sub-agent `analyst`", prompt)
        self.assertIn("# Analyst Agent", prompt)
        self.assertIn("/tmp/analyst", prompt)


if __name__ == "__main__":
    unittest.main()

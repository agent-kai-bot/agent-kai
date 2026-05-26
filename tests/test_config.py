"""Unit tests for environment-backed endpoint configuration."""

import os
import unittest

from config import AGENTS, get_agent_config, get_endpoint
from agent.core import create_llm


class ConfigTests(unittest.TestCase):
    """Validate endpoint configuration behavior."""

    def test_get_endpoint_prefers_env_api_key(self):
        """Endpoint API keys should resolve from the configured environment variable."""
        previous = os.environ.get("AGENT_KAI_API_KEY")
        os.environ["AGENT_KAI_API_KEY"] = "kai-test-key"
        try:
            endpoint = get_endpoint("kai-smart")
        finally:
            if previous is None:
                os.environ.pop("AGENT_KAI_API_KEY", None)
            else:
                os.environ["AGENT_KAI_API_KEY"] = previous

        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint["api_key"], "kai-test-key")

    def test_unknown_agent_config_fails_closed_without_endpoint_fallback(self):
        """Unknown sub-agent names must not silently default to kai-fast/agent-k.ai."""
        self.assertNotIn("retro-analyst-unit-test", AGENTS)
        with self.assertRaisesRegex(KeyError, "unknown agent 'retro-analyst-unit-test'"):
            get_agent_config("retro-analyst-unit-test")

    def test_create_llm_requires_explicit_endpoint_config(self):
        """The LLM factory should not choose the first configured endpoint implicitly."""
        with self.assertRaisesRegex(ValueError, "endpoint_cfg is required"):
            create_llm(None)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for environment-backed endpoint configuration."""

import os
import unittest

from config import get_endpoint


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


if __name__ == "__main__":
    unittest.main()

"""Unit tests for runtime response helpers."""

import unittest

from agent.runtime_utils import EMPTY_RESPONSE_ERROR, ensure_non_empty_response


class RuntimeUtilsTests(unittest.TestCase):
    """Validate empty-response normalization."""

    def test_ensure_non_empty_response_keeps_real_text(self):
        """Non-empty responses should pass through unchanged."""
        self.assertEqual(ensure_non_empty_response("hello"), "hello")

    def test_ensure_non_empty_response_replaces_blank_text(self):
        """Blank responses should become a stable error string."""
        self.assertEqual(ensure_non_empty_response("   "), EMPTY_RESPONSE_ERROR)


if __name__ == "__main__":
    unittest.main()

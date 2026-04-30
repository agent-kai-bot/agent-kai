"""Tests for the claude-cli endpoint adapter."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import core
from agent.core import _load_anthropic_api_key, create_llm


class ClaudeCliEndpointTests(unittest.TestCase):
    """Validate Claude endpoint construction and auth failures."""

    def test_missing_auth_raises_clean_runtime_error(self) -> None:
        """Missing Claude credentials fail before any model call is attempted."""

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(os.environ, {}, clear=True):
            auth_path = str(Path(temp_dir) / "missing-auth.json")
            with self.assertRaisesRegex(RuntimeError, "Claude endpoint requires Anthropic credentials"):
                _load_anthropic_api_key(
                    {"api_key_env": "ANTHROPIC_API_KEY", "auth_path": auth_path}
                )

    def test_auth_json_supplies_api_key(self) -> None:
        """Claude auth JSON is accepted when the environment is empty."""

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(os.environ, {}, clear=True):
            auth_path = Path(temp_dir) / "auth.json"
            auth_path.write_text(json.dumps({"api_key": "sk-ant-test"}), encoding="utf-8")

            self.assertEqual(
                _load_anthropic_api_key({"auth_path": str(auth_path)}),
                "sk-ant-test",
            )

    @unittest.skipIf(core.ChatAnthropic is None, "langchain-anthropic is not installed")
    def test_create_llm_routes_claude_cli_to_chat_claude(self) -> None:
        """provider=claude-cli builds a ChatClaude instance."""

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            llm = create_llm(
                {
                    "provider": "claude-cli",
                    "model": "sonnet",
                    "api_key_env": "ANTHROPIC_API_KEY",
                    "max_tokens": 256,
                    "temperature": 0.1,
                    "top_p": 0.9,
                }
            )

        self.assertIsInstance(llm, core.ChatClaude)


if __name__ == "__main__":
    unittest.main()

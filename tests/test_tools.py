"""Tests for host tool safety caps."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import config
from agent import tools


class ToolSafetyCapTests(unittest.TestCase):
    """Validate env-backed caps consumed by agent tools."""

    def test_shell_timeout_default_env_override_and_malformed_fallback(self) -> None:
        """shell_exec uses the raised default and reads env overrides at call time."""

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertGreaterEqual(config.get_shell_timeout_seconds(), 1800)
            self.assertEqual(config.get_shell_timeout_seconds(), 1800)

        seen: dict[str, int] = {}

        def fake_run(*_args, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            return SimpleNamespace(stdout="ok", stderr="", returncode=0)

        with mock.patch.dict("os.environ", {"KAI_SHELL_TIMEOUT_SECONDS": "77"}, clear=True):
            self.assertEqual(config.get_shell_timeout_seconds(), 77)
            with mock.patch("agent.tools.subprocess.run", side_effect=fake_run):
                self.assertEqual(tools._shell_exec("true"), "ok")
            self.assertEqual(seen["timeout"], 77)

        with mock.patch.dict("os.environ", {"KAI_SHELL_TIMEOUT_SECONDS": "bad"}, clear=True):
            self.assertEqual(config.get_shell_timeout_seconds(), 1800)

    def test_file_read_cap_default_env_override_and_malformed_fallback(self) -> None:
        """file_read uses the raised default and reads env overrides at call time."""

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertGreaterEqual(config.get_max_file_read_chars(), 250_000)
            self.assertEqual(config.get_max_file_read_chars(), 250_000)

        handle = tempfile.NamedTemporaryFile("w", delete=False)
        try:
            handle.write("abcdefghijklmnopqrstuvwxyz")
            handle.close()
            with mock.patch.dict("os.environ", {"KAI_MAX_FILE_READ_CHARS": "10"}, clear=True):
                self.assertEqual(config.get_max_file_read_chars(), 10)
                output = tools._file_read(handle.name)
            self.assertTrue(output.startswith("abcdefghij"))
            self.assertIn("... [truncated at 10 chars]", output)
        finally:
            os.unlink(handle.name)

        with mock.patch.dict("os.environ", {"KAI_MAX_FILE_READ_CHARS": "bad"}, clear=True):
            self.assertEqual(config.get_max_file_read_chars(), 250_000)

    def test_output_cap_default_env_override_and_malformed_fallback(self) -> None:
        """Tool output truncation uses the raised cap and env override."""

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertGreaterEqual(config.get_max_output_chars(), 200_000)
            self.assertEqual(config.get_max_output_chars(), 200_000)

        with mock.patch.dict("os.environ", {"KAI_MAX_OUTPUT_CHARS": "12"}, clear=True):
            self.assertEqual(config.get_max_output_chars(), 12)
            output = tools._python_exec("print('abcdefghijklmnopqrst')")
        self.assertTrue(output.startswith("abcdefghijkl"))
        self.assertIn("... [truncated at 12 chars]", output)

        with mock.patch.dict("os.environ", {"KAI_MAX_OUTPUT_CHARS": "bad"}, clear=True):
            self.assertEqual(config.get_max_output_chars(), 200_000)


if __name__ == "__main__":
    unittest.main()

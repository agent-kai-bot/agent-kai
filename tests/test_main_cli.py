"""Tests for the Phase 2 daemon and remote CLI flags."""

from __future__ import annotations

import unittest

from main import build_parser, validate_args


class MainCliTests(unittest.TestCase):
    """Validate the new daemon/remote/standalone flag semantics."""

    def test_remote_requires_terminal(self):
        parser = build_parser()
        args = parser.parse_args(["--remote", "ws://127.0.0.1:8765"])

        with self.assertRaises(SystemExit):
            validate_args(parser, args)

    def test_remote_and_standalone_conflict(self):
        parser = build_parser()
        args = parser.parse_args(
            ["--terminal", "--remote", "ws://127.0.0.1:8765", "--standalone"]
        )

        with self.assertRaises(SystemExit):
            validate_args(parser, args)

    def test_daemon_flag_is_valid_on_its_own(self):
        parser = build_parser()
        args = parser.parse_args(["--daemon"])

        validate_args(parser, args)
        self.assertTrue(args.daemon)
        self.assertFalse(args.terminal)

    def test_terminal_remote_combination_is_valid(self):
        parser = build_parser()
        args = parser.parse_args(["--terminal", "--remote", "ws://example.com:9999"])

        validate_args(parser, args)
        self.assertTrue(args.terminal)
        self.assertEqual(args.remote, "ws://example.com:9999")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for global terminal input focus shortcuts."""

from __future__ import annotations

import unittest

from textual import events

from tui.terminal import TradingTerminal


class _FakeInput:
    """Minimal input stub for terminal key-routing tests."""

    def __init__(self, value: str = "", cursor_position: int = 0, has_focus: bool = False):
        self.value = value
        self.cursor_position = cursor_position
        self.has_focus = has_focus
        self.disabled = False
        self.focus_calls = 0

    def focus(self) -> None:
        """Mark the fake input as focused."""

        self.has_focus = True
        self.focus_calls += 1

    def insert_text_at_cursor(self, text: str) -> None:
        """Insert text at the current cursor position."""

        self.value = (
            self.value[: self.cursor_position]
            + text
            + self.value[self.cursor_position :]
        )
        self.cursor_position += len(text)


class TradingTerminalInputTests(unittest.TestCase):
    """Validate global slash-to-input routing behavior."""

    @staticmethod
    def _make_terminal(fake_input: _FakeInput) -> TradingTerminal:
        """Build a minimal terminal object with a stubbed input lookup."""

        terminal = TradingTerminal.__new__(TradingTerminal)
        terminal.query_one = lambda _selector, _widget_type=None: fake_input
        return terminal

    def test_focus_input_with_text_preserves_existing_buffer(self):
        """Slash redirect should focus the input and insert at the cursor."""

        fake_input = _FakeInput(value="buy BTC", cursor_position=0, has_focus=False)
        terminal = self._make_terminal(fake_input)

        updated = terminal._focus_input_with_text("/")

        self.assertTrue(updated)
        self.assertTrue(fake_input.has_focus)
        self.assertEqual(fake_input.focus_calls, 1)
        self.assertEqual(fake_input.value, "/buy BTC")
        self.assertEqual(fake_input.cursor_position, 1)

    def test_on_key_routes_slash_to_input_when_other_panel_has_focus(self):
        """Typing slash away from the input should move focus and insert it."""

        fake_input = _FakeInput(value="", cursor_position=0, has_focus=False)
        terminal = self._make_terminal(fake_input)
        event = events.Key("slash", "/")

        terminal.on_key(event)

        self.assertTrue(fake_input.has_focus)
        self.assertEqual(fake_input.value, "/")
        self.assertTrue(getattr(event, "_stop_propagation", False))

    def test_on_key_skips_redirect_when_input_already_has_focus(self):
        """Focused input should receive slash normally without app-level insertion."""

        fake_input = _FakeInput(value="existing", cursor_position=8, has_focus=True)
        terminal = self._make_terminal(fake_input)
        event = events.Key("slash", "/")

        terminal.on_key(event)

        self.assertEqual(fake_input.value, "existing")
        self.assertEqual(fake_input.focus_calls, 0)
        self.assertFalse(getattr(event, "_stop_propagation", False))


if __name__ == "__main__":
    unittest.main()

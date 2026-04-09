"""Chat row showing a queued input with a clickable [X] to remove it.

Mounted into the chat panel by ``TradingTerminal.on_input_submitted``
when the user types a command while the agent is busy. The row sits
in chat history alongside normal messages so the user can see exactly
what's pending and click the [X] to drop any item from the FIFO queue
without having to wait for it to come up.

Communication contract: when the user clicks the [X] this widget
posts a ``QueuedInputRow.Removed`` message containing a reference to
itself. The parent ``TradingTerminal`` handles that message in
``on_queued_input_row_removed`` (snake_case auto-dispatch from the
CamelCase class name) which finds the row in ``_queue_widgets``,
drops the matching string from ``_input_queue``, removes the widget
from the DOM, and renumbers the remaining rows.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Static


class QueuedInputRow(Horizontal):
    """A queued input awaiting dispatch. Removable by clicking [X]."""

    DEFAULT_CSS = """
    QueuedInputRow {
        height: 1;
        width: 100%;
        background: $surface;
        padding: 0 0;
    }
    QueuedInputRow > .queue-text {
        width: 1fr;
        height: 1;
        color: $text-muted;
        content-align: left middle;
        padding: 0 1;
    }
    QueuedInputRow > .queue-x {
        width: 5;
        min-width: 5;
        height: 1;
        background: $error 60%;
        color: $text;
        border: none;
        text-style: bold;
        padding: 0 1;
    }
    QueuedInputRow > .queue-x:hover {
        background: $error;
    }
    """

    class Removed(Message):
        """Posted when the user clicks the [X] on a queued row.

        The parent ``TradingTerminal`` handles this in
        ``on_queued_input_row_removed`` and uses ``self.row`` to
        locate the matching string in ``_input_queue``.
        """

        def __init__(self, row: "QueuedInputRow") -> None:
            super().__init__()
            self.row = row

    def __init__(self, queued_text: str, position: int):
        super().__init__()
        self._queued_text = queued_text
        self._position = position

    @property
    def queued_text(self) -> str:
        """The original (untruncated) text that was queued."""
        return self._queued_text

    def compose(self) -> ComposeResult:
        yield Static(self._render_label(), classes="queue-text")
        yield Button("[X]", classes="queue-x")

    def set_position(self, position: int) -> None:
        """Update the displayed (#N) label after the queue has shifted.

        Called by ``TradingTerminal._renumber_queue_widgets`` whenever
        an item is removed (by drain or by user click) so the
        remaining rows display their current FIFO position.
        """
        self._position = position
        try:
            self.query_one(".queue-text", Static).update(self._render_label())
        except Exception:
            # Widget not yet mounted (compose hasn't run) — the
            # initial label will be correct from compose() anyway.
            pass

    def _render_label(self) -> str:
        """Render the row's left-side text from the current state.

        Truncates long inputs to 60 chars + ellipsis so the row
        stays single-line and the [X] stays visible at the right
        edge regardless of input length.
        """
        preview = self._queued_text[:60].replace("\n", " ")
        if len(self._queued_text) > 60:
            preview += "…"
        return f"queued (#{self._position}): {preview}"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Translate a button click into the higher-level Removed message.

        We don't act on the click here — the parent terminal owns
        the queue state and the widget list, so we just hand off the
        click as a typed message it can route through the
        ``_drop_queue_item`` path.
        """
        self.post_message(self.Removed(self))

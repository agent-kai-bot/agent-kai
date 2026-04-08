"""Agent chat panel — extracted from tui/app.py for reuse."""

from textual.containers import VerticalScroll
from textual.widgets import Static


class ChatPanel(VerticalScroll):
    """Scrollable chat panel that displays agent conversation."""

    DEFAULT_CSS = """
    ChatPanel {
        height: 1fr;
    }
    """

    def append_message(self, markup: str, css_class: str = "agent-msg"):
        """Append a styled message."""
        widget = Static(markup, classes=css_class)
        self.mount(widget)
        self.scroll_end(animate=False)

    def create_response_widget(self) -> Static:
        """Create and mount a new response widget for streaming."""
        widget = Static("", classes="agent-msg")
        self.mount(widget)
        return widget

    def clear_messages(self):
        """Clear all messages."""
        self.remove_children()

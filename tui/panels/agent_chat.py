"""Agent chat panel — extracted from tui/app.py for reuse."""

from textual.containers import VerticalScroll
from textual.widgets import Static


class ChatPanel(VerticalScroll):
    """Scrollable chat panel that displays agent conversation.

    Two rendering paths for agent output:

    1. **Streaming raw text** — ``create_response_widget()`` returns
       an empty Static; the caller calls ``widget.update(text)`` on
       each token chunk to grow the visible response in real time.
       Renders as plain text with no markdown formatting (the half-
       formed markdown looks worse than plain text mid-stream).

    2. **Final markdown render** — once the stream completes, the
       caller calls ``render_widget_as_markdown(widget, text)`` to
       swap the widget's content for a Rich ``Markdown`` renderable.
       Bold, italic, code blocks, lists, headings, and tables all
       format properly. The raw text is stashed on ``widget._raw_text``
       so the Ctrl+Y copy path can grab clean text without parsing
       the rendered markdown.
    """

    DEFAULT_CSS = """
    ChatPanel {
        height: 1fr;
    }
    """

    def append_message(self, markup: str, css_class: str = "agent-msg"):
        """Append a styled message (Rich markup string, no markdown)."""
        widget = Static(markup, classes=css_class)
        self.mount(widget)
        self.scroll_end(animate=False)

    def append_markdown(self, text: str, css_class: str = "agent-msg") -> Static:
        """Append a message rendered as Rich Markdown.

        Used for one-shot agent responses (i.e. not streamed). For
        streamed responses, use create_response_widget + token updates
        + render_widget_as_markdown when the stream finishes.
        """
        from rich.markdown import Markdown
        widget = Static(Markdown(text), classes=css_class)
        widget._raw_text = text  # for Ctrl+Y copy path
        self.mount(widget)
        self.scroll_end(animate=False)
        return widget

    def create_response_widget(self) -> Static:
        """Create and mount a new empty response widget for streaming.

        Caller updates it with raw token text via ``widget.update(text)``
        on each chunk, then converts to markdown via
        ``render_widget_as_markdown(widget, final_text)`` when the
        stream completes.
        """
        widget = Static("", classes="agent-msg")
        widget._raw_text = ""
        self.mount(widget)
        return widget

    def render_widget_as_markdown(self, widget: Static, text: str) -> None:
        """Swap a streaming widget's content for a Markdown render.

        Called from the final-event handler in the TUI's agent loop
        once token streaming finishes. Replaces the plain-text
        renderable with a Rich ``Markdown`` object so bold / italic /
        code / lists / headings / tables actually format. The raw
        text is stored on ``widget._raw_text`` so Ctrl+Y can copy
        the original markdown source instead of the rendered output.
        """
        from rich.markdown import Markdown
        widget._raw_text = text
        try:
            widget.update(Markdown(text))
        except Exception:
            # If markdown rendering fails for any reason (malformed
            # input, Rich version mismatch, etc) fall back to the
            # plain text already on the widget so the user still
            # sees the response.
            widget.update(text)

    def clear_messages(self):
        """Clear all messages."""
        self.remove_children()

"""Textual TUI for the local AI agent."""

from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Input, RichLog, Static


class AgentTUI(App):
    """Chat TUI for the LangChain agent with NATS and tool panels."""

    CSS_PATH = str(Path(__file__).parent / "styles.tcss")
    TITLE = "Local AI Agent"

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear"),
        ("ctrl+t", "toggle_tool_panel", "Toggle tools inline"),
    ]

    def __init__(self, agent_runner, bus=None):
        super().__init__()
        self.agent_runner = agent_runner
        self.bus = bus
        self._agent_working = False
        self._tools_inline = False  # when True, tool output goes in chat instead of side panel

    def compose(self) -> ComposeResult:
        yield Static("Local AI Agent  [ctrl+t: toggle tools]", id="header")
        yield VerticalScroll(id="chat-scroll")
        yield Vertical(
            RichLog(id="nats-panel", markup=True, wrap=True),
            RichLog(id="tool-panel", markup=True, wrap=True),
            id="right-panel",
        )
        yield Static("Status: idle", id="status-bar")
        yield Input(placeholder="Type your message...", id="input-area")

    async def on_mount(self):
        if self.bus:
            self.bus.on_message(self._on_nats_message)
            await self.bus.subscribe(
                f"agent.{self.bus.agent_name}.request",
                self._handle_nats_request,
            )
            await self.bus.subscribe("agent.broadcast", self._handle_nats_broadcast)
            self._nats_log("[bold green]NATS connected[/]")
            from config import AGENTS
            for name, cfg in AGENTS.items():
                if name == "nano":
                    continue
                desc = cfg.get("description", "")
                self._nats_log(f"  [dim]{name}[/] [dim italic]{desc[:30]}[/]")

        self._tool_log("[dim]Tool calls appear here[/]")
        self._append_chat("[bold dim]Welcome! Type a message to chat with the agent.[/]", "agent-msg")
        self.query_one("#input-area", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        if self._agent_working:
            self._append_chat("[dim]Agent is busy, please wait...[/]", "agent-msg")
            return

        self._append_chat(f"[bold green]> {text}[/]", "user-msg")
        self._agent_working = True
        self._set_status("thinking...")
        self.run_worker(self._process_agent(text), thread=False)

    async def _process_agent(self, user_input: str):
        chat = self.query_one("#chat-scroll", VerticalScroll)
        response_widget = Static("", classes="agent-msg")
        chat.mount(response_widget)
        accumulated = ""

        try:
            async for event in self.agent_runner.run(user_input):
                etype = event["type"]

                if etype == "token":
                    accumulated += event["data"]
                    response_widget.update(accumulated)
                    chat.scroll_end(animate=False)

                elif etype == "tool_start":
                    tool = event["data"]["tool"]
                    tool_input = event["data"]["input"]
                    if isinstance(tool_input, dict):
                        input_summary = ", ".join(
                            f"{k}={repr(v)[:60]}" for k, v in tool_input.items()
                        )
                    else:
                        input_summary = str(tool_input)[:80]

                    msg = f"[bold yellow]>> {tool}({input_summary})[/]"
                    self._tool_log(msg)
                    if self._tools_inline:
                        self._append_chat(msg, "tool-msg")
                    self._set_status(f"running {tool}...")

                elif etype == "tool_end":
                    tool = event["data"]["tool"]
                    output = event["data"]["output"]
                    preview = output[:300].replace("\n", " ")

                    msg = f"[yellow]<< {tool}: {preview}[/]"
                    self._tool_log(msg)
                    if self._tools_inline:
                        self._append_chat(msg, "tool-msg")
                    self._set_status("thinking...")

                elif etype == "final":
                    final_text = event["data"]
                    if final_text and final_text != accumulated:
                        response_widget.update(final_text)
                    chat.scroll_end(animate=False)

                elif etype == "status":
                    self._set_status(event["data"])

                elif etype == "error":
                    self._append_chat(f"[bold red]Error: {event['data']}[/]", "error-msg")
                    self._tool_log(f"[bold red]Error: {event['data']}[/]")

        except Exception as e:
            self._append_chat(f"[bold red]Error: {e}[/]", "error-msg")
        finally:
            self._agent_working = False
            self._set_status("idle")
            chat.scroll_end(animate=False)

    def _append_chat(self, markup: str, css_class: str = "agent-msg"):
        chat = self.query_one("#chat-scroll", VerticalScroll)
        widget = Static(markup, classes=css_class)
        chat.mount(widget)
        chat.scroll_end(animate=False)

    def _set_status(self, text: str):
        self.query_one("#status-bar", Static).update(f"Status: {text}")

    def _on_nats_message(self, direction: str, subject: str, payload: dict):
        arrows = {"pub": ">", "sub": "<", "req": ">>", "rep": "<<"}
        arrow = arrows.get(direction, "?")
        ts = datetime.now().strftime("%H:%M:%S")
        short_subject = subject.replace("agent.", "a.").replace("system.", "s.")
        self.call_from_thread(
            self._nats_log,
            f"[dim]{ts}[/] {arrow} [bold]{short_subject}[/]",
        )

    def _nats_log(self, markup: str):
        try:
            self.query_one("#nats-panel", RichLog).write(markup)
        except Exception:
            pass

    def _tool_log(self, markup: str):
        try:
            self.query_one("#tool-panel", RichLog).write(markup)
        except Exception:
            pass

    async def _handle_nats_request(self, subject: str, payload: dict):
        task = payload.get("task") or payload.get("message", "")
        if not task:
            return
        self._append_chat(f"[bold cyan][NATS] {payload.get('from', '?')}: {task}[/]", "agent-msg")
        self.run_worker(self._process_agent(task), thread=False)

    async def _handle_nats_broadcast(self, subject: str, payload: dict):
        msg = payload.get("message", str(payload))
        self._append_chat(f"[bold magenta][broadcast] {msg}[/]", "agent-msg")

    def action_clear_chat(self):
        self.query_one("#chat-scroll", VerticalScroll).remove_children()
        self.agent_runner.chat_history.clear()

    def action_toggle_tool_panel(self):
        self._tools_inline = not self._tools_inline
        mode = "inline (chat)" if self._tools_inline else "side panel"
        self._set_status(f"Tool output: {mode}")
        self._tool_log(f"[dim]Tool output mode: {mode}[/]")

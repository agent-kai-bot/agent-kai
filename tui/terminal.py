"""KAI Trading Terminal — multi-panel crypto TUI."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import requests
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog, Static

from tui.panels.alerts import AlertsPanel
from tui.panels.agent_chat import ChatPanel
from tui.panels.chart import ChartPanel
from tui.panels.positions import PositionsPanel
from tui.panels.watchlist import WatchlistPanel

from data_api.config import API_PORT

API_BASE = f"http://localhost:{API_PORT}/api/v1"
TRACKED_SYMBOLS = ["BTC", "ETH", "SOL"]


class TradingTerminal(App):
    """KAI Crypto Trading Terminal."""

    CSS_PATH = str(Path(__file__).parent / "terminal_styles.tcss")
    TITLE = "KAI Trading Terminal"

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear chat"),
        ("ctrl+t", "cycle_timeframe", "Timeframe"),
        ("ctrl+w", "toggle_watchlist_add", "Add symbol"),
    ]

    TIMEFRAMES = ["1m", "5m", "15m", "1h"]

    def __init__(self, agent_runner, bus=None):
        super().__init__()
        self.agent_runner = agent_runner
        self.bus = bus
        self._agent_working = False
        self._current_tf_idx = 0
        self._chart_symbol = "BTC"

    def compose(self) -> ComposeResult:
        yield Static(
            "KAI Trading Terminal (agent-k.ai)  [ctrl+t: timeframe] [ctrl+w: add symbol]",
            id="header",
        )
        # Left column
        yield WatchlistPanel(tracked_symbols=list(TRACKED_SYMBOLS), id="watchlist-panel")
        # Center column
        yield ChartPanel(id="chart-panel")
        # Right column
        yield AlertsPanel(id="alerts-panel")
        # Second row
        yield PositionsPanel(id="positions-panel")
        yield ChatPanel(id="chat-panel")
        yield RichLog(id="nats-panel", markup=True, wrap=True)
        # Bottom
        yield Static("Status: idle | Portfolio: $100,000.00", id="status-bar")
        yield Input(placeholder="/buy BTC 0.1 | /analyze SOL | /scan trending | or just chat...", id="input-area")

    async def on_mount(self):
        # Wire NATS
        if self.bus:
            self.bus.on_message(self._on_nats_message)
            await self.bus.subscribe(
                f"agent.{self.bus.agent_name}.request",
                self._handle_nats_request,
            )
            await self.bus.subscribe("agent.broadcast", self._handle_nats_broadcast)
            self._nats_log("[bold green]NATS connected[/]")

            # List available agents
            from config import AGENTS
            for name, cfg in AGENTS.items():
                if name == "nano":
                    continue
                desc = cfg.get("description", "")[:35]
                self._nats_log(f"  [dim]{name}[/] [dim italic]{desc}[/]")

            # Subscribe to market data subjects
            await self.bus.subscribe("market.>", self._handle_market_data)
            await self.bus.subscribe("portfolio.>", self._handle_portfolio_data)
            await self.bus.subscribe("alert.>", self._handle_alert)

        # Load initial data
        self.run_worker(self._load_initial_data(), thread=False)

        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message("[bold dim]Welcome to KAI. Type a message or use slash commands.[/]")
        chat.append_message("[dim]/buy /sell /analyze /scan /risk /chart /watch[/]")
        self.query_one("#input-area", Input).focus()

    async def _load_initial_data(self):
        """Load initial prices and chart data from the API."""
        try:
            # Load watchlist prices
            watchlist = self.query_one("#watchlist-panel", WatchlistPanel)
            for sym in TRACKED_SYMBOLS:
                try:
                    resp = requests.get(f"{API_BASE}/price/{sym}", timeout=5)
                    data = resp.json()
                    watchlist.update_price(data["symbol"], data["price"], data.get("volume"))
                except Exception:
                    pass

            # Load chart
            await self._load_chart(self._chart_symbol, self.TIMEFRAMES[self._current_tf_idx])

            # Load positions
            await self._refresh_positions()
        except Exception as e:
            self._nats_log(f"[red]Init error: {e}[/]")

    async def _load_chart(self, symbol: str, interval: str):
        """Load chart data from API."""
        try:
            resp = requests.get(
                f"{API_BASE}/ohlcv/{symbol}",
                params={"interval": interval, "limit": 120},
                timeout=10,
            )
            bars = resp.json()
            chart = self.query_one("#chart-panel", ChartPanel)
            chart.set_data(symbol, interval, bars)
            self._chart_symbol = symbol
        except Exception as e:
            self._nats_log(f"[red]Chart load error: {e}[/]")

    async def _refresh_positions(self):
        """Refresh positions panel from paper trading engine."""
        try:
            from data_api.paper_trading import portfolio
            positions = portfolio.get_positions()
            pnl = portfolio.get_pnl()
            panel = self.query_one("#positions-panel", PositionsPanel)
            panel.update_positions(positions, pnl)
            self._set_status(
                f"idle | Portfolio: ${pnl['total_value']:,.2f} | "
                f"P&L: ${pnl['total_pnl']:+,.2f} ({pnl['total_pnl_pct']:+.1f}%)"
            )
        except Exception:
            pass

    # ── Input handling ────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        if self._agent_working:
            chat = self.query_one("#chat-panel", ChatPanel)
            chat.append_message("[dim]Agent busy, please wait...[/]")
            return

        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message(f"[bold green]> {text}[/]", "user-msg")

        # Parse slash commands
        routed = await self._handle_slash_command(text)
        if routed:
            return

        # Default: send to agent
        self._agent_working = True
        self._set_status("thinking...")
        self.run_worker(self._process_agent(text), thread=False)

    async def _handle_slash_command(self, text: str) -> bool:
        """Parse and route slash commands. Returns True if handled."""
        parts = text.strip().split()
        cmd = parts[0].lower() if parts else ""

        if cmd in ("/buy", "/sell"):
            if len(parts) < 3:
                self._chat_msg("[red]Usage: /buy SYMBOL QTY [limit PRICE][/]")
                return True
            symbol = parts[1].upper()
            qty = parts[2]
            order_type = "market"
            price = ""
            if len(parts) >= 5 and parts[3].lower() == "limit":
                order_type = "limit"
                price = parts[4]
            side = "buy" if cmd == "/buy" else "sell"
            task = f"Place a {order_type} {side} order for {qty} {symbol}"
            if price:
                task += f" at ${price}"
            self._agent_working = True
            self._set_status(f"placing {side} order...")
            self.run_worker(self._run_agent_task("trader", task), thread=False)
            return True

        elif cmd == "/analyze":
            symbol = parts[1].upper() if len(parts) > 1 else "BTC"
            tf = parts[2] if len(parts) > 2 else "1m"
            task = f"Run a full technical analysis on {symbol} {tf} timeframe. Include RSI, MACD, Bollinger Bands, and key support/resistance levels."
            self._agent_working = True
            self._set_status(f"analyzing {symbol}...")
            self.run_worker(self._run_agent_task("analyst", task), thread=False)
            return True

        elif cmd == "/scan":
            filter_type = parts[1] if len(parts) > 1 else "trending"
            task = f"Scan pump.fun for {filter_type} tokens and summarize the most interesting ones."
            self._agent_working = True
            self._set_status("scanning tokens...")
            self.run_worker(self._run_agent_task("scanner", task), thread=False)
            return True

        elif cmd == "/risk":
            task = "Review the current portfolio. Check all open positions, calculate total exposure, and flag any risk concerns."
            self._agent_working = True
            self._set_status("checking risk...")
            self.run_worker(self._run_agent_task("risk-manager", task), thread=False)
            return True

        elif cmd == "/chart":
            symbol = parts[1].upper() if len(parts) > 1 else self._chart_symbol
            tf = parts[2] if len(parts) > 2 else self.TIMEFRAMES[self._current_tf_idx]
            await self._load_chart(symbol, tf)
            self._chat_msg(f"[dim]Chart: {symbol} {tf}[/]")
            return True

        elif cmd == "/watch":
            if len(parts) < 2:
                self._chat_msg("[red]Usage: /watch SYMBOL[/]")
                return True
            symbol = parts[1].upper()
            watchlist = self.query_one("#watchlist-panel", WatchlistPanel)
            if symbol in watchlist.tracked_symbols:
                watchlist.remove_symbol(symbol)
                self._chat_msg(f"[dim]Removed {symbol} from watchlist[/]")
            else:
                watchlist.add_symbol(symbol)
                self._chat_msg(f"[dim]Added {symbol} to watchlist[/]")
            return True

        elif cmd == "/positions" or cmd == "/pos":
            await self._refresh_positions()
            self._chat_msg("[dim]Positions refreshed[/]")
            return True

        return False

    async def _run_agent_task(self, agent_name: str, task: str):
        """Spawn an agent (if needed) and send it a task, display results in chat."""
        chat = self.query_one("#chat-panel", ChatPanel)
        try:
            # Use nats_request if bus available
            if self.bus:
                from agent.sub_agents import SubAgentManager
                # Check if we have the sub_agent_manager
                if hasattr(self, '_sub_agent_manager') and self._sub_agent_manager:
                    mgr = self._sub_agent_manager
                    if agent_name not in mgr.agents:
                        chat.append_message(f"[dim]Spawning {agent_name}...[/]")
                        await mgr.spawn(agent_name)

                    reply = await self.bus.request(
                        f"agent.{agent_name}.request",
                        {"task": task, "from": self.bus.agent_name},
                        timeout=120,
                    )
                    response = reply.get("response", str(reply))
                    chat.append_message(f"[bold cyan][{agent_name}][/] {response}")
                else:
                    # Fallback to main agent
                    await self._process_agent(f"[For {agent_name}]: {task}")
                    return
            else:
                await self._process_agent(task)
                return

        except Exception as e:
            chat.append_message(f"[bold red]Error from {agent_name}: {e}[/]", "error-msg")
        finally:
            self._agent_working = False
            self._set_status("idle")
            await self._refresh_positions()

    # ── Agent streaming ───────────────────────────────────────

    async def _process_agent(self, user_input: str):
        """Stream agent output to chat."""
        chat = self.query_one("#chat-panel", ChatPanel)
        response_widget = chat.create_response_widget()
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
                    self._nats_log(f"[yellow]>> {tool}[/]")
                    self._set_status(f"running {tool}...")
                elif etype == "tool_end":
                    tool = event["data"]["tool"]
                    self._nats_log(f"[yellow]<< {tool}[/]")
                    self._set_status("thinking...")
                elif etype == "final":
                    final = event["data"]
                    if final and final != accumulated:
                        response_widget.update(final)
                    chat.scroll_end(animate=False)
                elif etype == "error":
                    chat.append_message(f"[bold red]Error: {event['data']}[/]", "error-msg")
        except Exception as e:
            chat.append_message(f"[bold red]Error: {e}[/]", "error-msg")
        finally:
            self._agent_working = False
            self._set_status("idle")
            await self._refresh_positions()

    # ── NATS handlers ─────────────────────────────────────────

    async def _handle_market_data(self, subject: str, payload: dict):
        """Handle market.{symbol}.{type} messages."""
        parts = subject.split(".")
        if len(parts) < 3:
            return
        symbol = parts[1]
        msg_type = parts[2]

        if msg_type == "price":
            watchlist = self.query_one("#watchlist-panel", WatchlistPanel)
            watchlist.update_price(symbol, payload.get("price", 0), payload.get("volume"))

        elif msg_type == "1m":
            chart = self.query_one("#chart-panel", ChartPanel)
            if chart.symbol == symbol and chart.interval == "1m":
                chart.update_last_bar(payload)

        elif msg_type == "signal":
            alerts = self.query_one("#alerts-panel", AlertsPanel)
            alerts.add_signal(
                payload.get("from", "?"),
                symbol,
                payload.get("direction", "?"),
                payload.get("reasoning", payload.get("message", "")),
            )

    async def _handle_portfolio_data(self, subject: str, payload: dict):
        """Handle portfolio.* messages."""
        await self._refresh_positions()

    async def _handle_alert(self, subject: str, payload: dict):
        """Handle alert.* messages."""
        parts = subject.split(".")
        alert_type = parts[1] if len(parts) > 1 else "unknown"
        alerts = self.query_one("#alerts-panel", AlertsPanel)
        msg = payload.get("message", str(payload)[:150])
        alerts.add_alert(alert_type, msg)

    async def _handle_nats_request(self, subject: str, payload: dict):
        task = payload.get("task") or payload.get("message", "")
        if not task:
            return
        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message(f"[bold cyan][NATS] {payload.get('from', '?')}: {task}[/]")
        self.run_worker(self._process_agent(task), thread=False)

    async def _handle_nats_broadcast(self, subject: str, payload: dict):
        msg = payload.get("message", str(payload))
        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message(f"[bold magenta][broadcast] {msg}[/]")

    # ── UI helpers ────────────────────────────────────────────

    def _chat_msg(self, markup: str):
        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message(markup)

    def _set_status(self, text: str):
        try:
            from data_api.paper_trading import portfolio
            pnl = portfolio.get_pnl()
            self.query_one("#status-bar", Static).update(
                f"Status: {text} | Portfolio: ${pnl['total_value']:,.2f} | "
                f"P&L: ${pnl['total_pnl']:+,.2f} ({pnl['total_pnl_pct']:+.1f}%)"
            )
        except Exception:
            self.query_one("#status-bar", Static).update(f"Status: {text}")

    def _on_nats_message(self, direction: str, subject: str, payload: dict):
        arrows = {"pub": ">", "sub": "<", "req": ">>", "rep": "<<"}
        arrow = arrows.get(direction, "?")
        ts = datetime.now().strftime("%H:%M:%S")
        short = subject.replace("market.", "m.").replace("agent.", "a.").replace("portfolio.", "p.").replace("system.", "s.")
        self.call_from_thread(
            self._nats_log,
            f"[dim]{ts}[/] {arrow} [bold]{short}[/]",
        )

    def _nats_log(self, markup: str):
        try:
            self.query_one("#nats-panel", RichLog).write(markup)
        except Exception:
            pass

    # ── Actions ───────────────────────────────────────────────

    def action_clear_chat(self):
        self.query_one("#chat-panel", ChatPanel).clear_messages()
        self.agent_runner.chat_history.clear()

    def action_cycle_timeframe(self):
        self._current_tf_idx = (self._current_tf_idx + 1) % len(self.TIMEFRAMES)
        tf = self.TIMEFRAMES[self._current_tf_idx]
        self.run_worker(self._load_chart(self._chart_symbol, tf), thread=False)
        self._chat_msg(f"[dim]Chart: {self._chart_symbol} {tf}[/]")

    def action_toggle_watchlist_add(self):
        """Focus input with /watch prefix."""
        inp = self.query_one("#input-area", Input)
        inp.value = "/watch "
        inp.focus()

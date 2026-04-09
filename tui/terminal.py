"""KAI Trading Terminal — multi-panel crypto TUI."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import requests
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Input, RichLog, Static

from agent.learning import (
    NUDGE_THRESHOLD,
    parse_mentor_reply,
    save_reflection_record,
)
from agent.signal_consumer import SignalConsumer
from agent.skills_store import SkillStore
from config import get_skills_dir
from tui.panels.alerts import AlertsPanel
from tui.panels.agent_chat import ChatPanel
from tui.panels.chart import ChartPanel
from tui.panels.positions import PositionsPanel
from tui.panels.watchlist import WatchlistPanel

from data_api.config import API_PORT

API_BASE = f"http://localhost:{API_PORT}/api/v1"
TRACKED_SYMBOLS = ["BTC", "ETH", "SOL"]

# Persisted UI state — chart symbol / timeframe / watchlist survive a
# restart. Lives under workspaces/terminal/ to match the pattern the
# paper trading engine uses (workspaces/trader/portfolio.json).
TERMINAL_STATE_PATH = Path(__file__).resolve().parent.parent / "workspaces" / "terminal" / "state.json"


def _load_terminal_state() -> dict:
    """Load persisted terminal state, or an empty dict if it doesn't exist."""
    try:
        with open(TERMINAL_STATE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        # Corrupt state file — start fresh rather than crash the TUI.
        return {}


def _save_terminal_state(state: dict) -> None:
    """Persist the terminal state atomically. Swallows errors — a write
    failure should never crash the TUI, since the state is only a UX
    convenience (chart symbol, timeframe, etc.)."""
    try:
        TERMINAL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = TERMINAL_STATE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        tmp.replace(TERMINAL_STATE_PATH)
    except OSError:
        pass


class TradingTerminal(App):
    """KAI Crypto Trading Terminal."""

    CSS_PATH = str(Path(__file__).parent / "terminal_styles.tcss")
    TITLE = "KAI Trading Terminal"

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear chat"),
        ("ctrl+t", "cycle_timeframe", "Timeframe"),
        ("ctrl+s", "cycle_symbol", "Symbol"),
        ("ctrl+w", "toggle_watchlist_add", "Add symbol"),
    ]

    TIMEFRAMES = ["1m", "5m", "15m", "1h"]

    def __init__(self, agent_runner, bus=None, signal_consumer: SignalConsumer | None = None):
        super().__init__()
        self.agent_runner = agent_runner
        self.bus = bus
        self._agent_working = False
        # Track the most recently-dispatched sub-agent so `/learn`
        # with no args knows which session to reflect on.
        self._last_sub_agent: str | None = None
        # Live signal feed — receives signals from the vpn-stack
        # signal scanners via NATS and buffers them for the
        # ``get_signals`` tool + the AlertsPanel display.
        self.signal_consumer = signal_consumer or SignalConsumer()
        # Chart data source — "local" (data_api) or "coinbase"
        # (direct WebSocket + REST fallback). The Coinbase feed
        # owns an async task for streaming; the local feed is
        # one-shot per /chart call.
        self._chart_source: str = "local"
        self._coinbase_stream = None  # CoinbaseCandleStream instance
        self._coinbase_task = None    # asyncio.Task running the feed loop

        # Restore persisted chart state from workspaces/terminal/state.json.
        # Falls back to the BTC + 1m defaults on first run or corrupt file.
        state = _load_terminal_state()
        self._chart_symbol = state.get("chart_symbol", "BTC")
        saved_tf = state.get("chart_timeframe", "1m")
        self._saved_color_scheme = state.get("chart_color_scheme", "default")
        self._chart_source = state.get("chart_source", "local")
        try:
            self._current_tf_idx = self.TIMEFRAMES.index(saved_tf)
        except ValueError:
            self._current_tf_idx = 0

    def compose(self) -> ComposeResult:
        yield Static(
            "KAI Trading Terminal (agent-k.ai)  "
            "[ctrl+s: symbol] [ctrl+t: timeframe] [ctrl+w: add symbol] "
            "[click a watchlist row to chart it]",
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
        yield Input(placeholder="/buy BTC 0.1 | /analyze SOL | /scan trending | /learn | or just chat...", id="input-area")

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

            # Subscribe the signal consumer to live signal scanner events.
            # Signals arriving on ``signals.>`` are buffered in the
            # consumer and also routed to the AlertsPanel via a callback.
            await self.signal_consumer.subscribe(self.bus)
            self.signal_consumer.on_signal = self._on_live_signal

        # Restore saved chart color scheme
        try:
            chart = self.query_one("#chart-panel", ChartPanel)
            chart.set_color_scheme(self._saved_color_scheme)
        except Exception:
            pass

        # Load initial data
        self.run_worker(self._load_initial_data(), thread=False)

        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message("[bold dim]Welcome to KAI. Type a message or use slash commands.[/]")
        chat.append_message("[dim]/buy /sell /analyze /scan /risk /chart /watch /learn[/]")
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
        """Load chart data using the current source (local or coinbase).

        This is the source-aware entry point — all call sites (slash
        commands, watchlist clicks, timeframe cycling) go through
        here so the selected source is always honored.
        """
        if self._chart_source == "coinbase":
            await self._start_coinbase_feed(symbol, interval)
            return

        # Local source: one-shot fetch from the project data_api.
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
            try:
                self._current_tf_idx = self.TIMEFRAMES.index(interval)
            except ValueError:
                pass
            self._save_chart_state(interval)
        except Exception as e:
            self._nats_log(f"[red]Chart load error: {e}[/]")

    def _save_chart_state(self, interval: str) -> None:
        """Persist chart symbol/timeframe/source/scheme to state.json."""
        try:
            current_scheme = self.query_one("#chart-panel", ChartPanel).color_scheme
        except Exception:
            current_scheme = "default"
        _save_terminal_state({
            "chart_symbol": self._chart_symbol,
            "chart_timeframe": interval,
            "chart_color_scheme": current_scheme,
            "chart_source": self._chart_source,
        })

    # ── Coinbase feed lifecycle ───────────────────────────────

    async def _start_coinbase_feed(self, symbol: str, interval: str) -> None:
        """Start (or restart) the Coinbase data feed for the chart.

        1) Stop any existing feed
        2) Fetch historical bars via REST (bootstrap)
        3) Launch a live feeder task — WebSocket for 5m, REST polling
           for all other intervals
        """
        await self._stop_coinbase_feed()

        try:
            # These are heavier imports — done lazily so a stack that
            # doesn't use Coinbase doesn't pay the import cost.
            from agent.data_sources.coinbase import (
                CoinbaseCandleStream,
                fetch_candles,
                normalize_product_id,
            )
        except Exception as e:
            self._nats_log(f"[red]Coinbase module import failed: {e}[/]")
            return

        product_id = normalize_product_id(symbol)

        # 1) Historical bootstrap via REST (off the event loop)
        try:
            hist = await asyncio.to_thread(
                fetch_candles, product_id, interval, 120
            )
        except Exception as e:
            self._nats_log(f"[red]Coinbase historical fetch failed ({product_id} {interval}): {e}[/]")
            return

        # Populate the chart with the historical window
        try:
            chart = self.query_one("#chart-panel", ChartPanel)
            chart.set_data(product_id, interval, hist)
        except Exception as e:
            self._nats_log(f"[red]Chart set_data failed: {e}[/]")
            return

        # Persist new state
        self._chart_symbol = product_id
        try:
            self._current_tf_idx = self.TIMEFRAMES.index(interval)
        except ValueError:
            pass
        self._save_chart_state(interval)

        # 2) Live feed — WebSocket for 5m, REST polling otherwise.
        # Coinbase's public candles WS channel only emits 5m candles;
        # for anything else we poll REST every 15s. The chart panel
        # doesn't care which one is feeding it.
        if interval == "5m":
            self._coinbase_stream = CoinbaseCandleStream([product_id])
            self._coinbase_task = asyncio.create_task(
                self._run_coinbase_ws_consumer(product_id)
            )
            self._nats_log(f"[bold cyan]Coinbase WS[/] {product_id} 5m live")
        else:
            self._coinbase_task = asyncio.create_task(
                self._run_coinbase_rest_poller(product_id, interval)
            )
            self._nats_log(f"[bold cyan]Coinbase REST[/] {product_id} {interval} polling 15s")

    async def _stop_coinbase_feed(self) -> None:
        """Cleanly stop any running Coinbase feed task + WS stream."""
        if self._coinbase_stream is not None:
            try:
                self._coinbase_stream.stop()
            except Exception:
                pass
        task = self._coinbase_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._coinbase_stream = None
        self._coinbase_task = None

    async def _run_coinbase_ws_consumer(self, product_id: str) -> None:
        """Consume the Coinbase WebSocket candle stream, updating the chart live."""
        try:
            chart = self.query_one("#chart-panel", ChartPanel)
            async for candle in self._coinbase_stream:
                # Only forward candles matching our current product
                if candle.get("product_id") and candle["product_id"] != product_id:
                    continue
                # The chart's update_last_bar merges by ts — WS emits
                # the same 5m candle multiple times as it fills, so
                # we want to overwrite the last bar until it closes.
                bar = {
                    "ts": candle["ts"],
                    "open": candle["open"],
                    "high": candle["high"],
                    "low": candle["low"],
                    "close": candle["close"],
                    "volume": candle["volume"],
                }
                chart.update_last_bar(bar)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._nats_log(f"[red]Coinbase WS consumer error: {e}[/]")

    async def _run_coinbase_rest_poller(self, product_id: str, interval: str) -> None:
        """Poll Coinbase REST every 15s and refresh the chart."""
        from agent.data_sources.coinbase import fetch_candles
        try:
            chart = self.query_one("#chart-panel", ChartPanel)
            while True:
                await asyncio.sleep(15)
                try:
                    bars = await asyncio.to_thread(
                        fetch_candles, product_id, interval, 120
                    )
                    chart.set_data(product_id, interval, bars)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._nats_log(f"[red]Coinbase poll error: {e}[/]")
        except asyncio.CancelledError:
            raise

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

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Click / Enter on a watchlist row -> load that symbol in the chart.

        Fires for any DataTable in the app so we filter by id. The row
        key was set to the symbol string when the watchlist was built,
        so ``event.row_key.value`` gives us the symbol directly.
        """
        table = event.data_table
        if getattr(table, "id", None) != "watchlist-panel":
            return
        symbol = event.row_key.value if event.row_key else None
        if not symbol:
            return
        tf = self.TIMEFRAMES[self._current_tf_idx]
        self.run_worker(self._load_chart(symbol, tf), thread=False)
        self._chat_msg(f"[dim]Chart: {symbol} {tf}[/]")

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
            # /chart                 — reload current
            # /chart BTC 1h          — change symbol + timeframe
            # /chart symbol BTC-USD  — change just the symbol (keep tf + source)
            # /chart source coinbase — switch data source to coinbase
            # /chart source local    — switch data source back to local data_api
            # /chart source          — show current source
            # /chart color classic   — switch color scheme
            # /chart color           — list available schemes
            # /chart on              — show chart panel
            # /chart off             — hide chart panel
            sub = parts[1].lower() if len(parts) > 1 else ""

            if sub == "color":
                chart = self.query_one("#chart-panel", ChartPanel)
                if len(parts) > 2:
                    scheme_name = parts[2].lower()
                    if chart.set_color_scheme(scheme_name):
                        self._chat_msg(f"[dim]Chart color scheme: {scheme_name}[/]")
                        self._save_chart_state(self.TIMEFRAMES[self._current_tf_idx])
                    else:
                        avail = ", ".join(chart.available_schemes())
                        self._chat_msg(f"[red]Unknown scheme '{scheme_name}'. Available: {avail}[/]")
                else:
                    avail = ", ".join(chart.available_schemes())
                    current = chart.color_scheme
                    self._chat_msg(f"[dim]Current: {current} | Available: {avail}[/]")
                return True

            if sub == "off":
                chart = self.query_one("#chart-panel", ChartPanel)
                chart.toggle_visible(False)
                self._chat_msg("[dim]Chart hidden[/]")
                return True

            if sub == "on":
                chart = self.query_one("#chart-panel", ChartPanel)
                chart.toggle_visible(True)
                self._chat_msg("[dim]Chart visible[/]")
                return True

            if sub == "source":
                if len(parts) < 3:
                    self._chat_msg(
                        f"[dim]Current source: {self._chart_source}[/] "
                        "[dim](available: local, coinbase)[/]"
                    )
                    return True
                source_name = parts[2].lower()
                if source_name not in ("local", "coinbase"):
                    self._chat_msg(
                        f"[red]Unknown source '{source_name}'. Use: local, coinbase[/]"
                    )
                    return True
                if source_name == self._chart_source:
                    self._chat_msg(f"[dim]Already on {source_name} source[/]")
                    return True
                # Stop any coinbase feed if we're moving away from it
                await self._stop_coinbase_feed()
                self._chart_source = source_name
                symbol = self._chart_symbol
                tf = self.TIMEFRAMES[self._current_tf_idx]
                # If switching TO coinbase, normalize the symbol
                # (e.g. BTC → BTC-USD) on first load. The feed does this.
                await self._load_chart(symbol, tf)
                self._chat_msg(f"[bold cyan]Chart source:[/] {source_name}")
                return True

            if sub == "symbol":
                if len(parts) < 3:
                    self._chat_msg("[red]Usage: /chart symbol SYMBOL[/]")
                    return True
                symbol = parts[2].upper()
                tf = self.TIMEFRAMES[self._current_tf_idx]
                await self._load_chart(symbol, tf)
                self._chat_msg(f"[dim]Chart symbol: {symbol} {tf} ({self._chart_source})[/]")
                return True

            # Default: /chart [SYMBOL] [TIMEFRAME]
            symbol = parts[1].upper() if len(parts) > 1 else self._chart_symbol
            tf = parts[2] if len(parts) > 2 else self.TIMEFRAMES[self._current_tf_idx]
            await self._load_chart(symbol, tf)
            self._chat_msg(f"[dim]Chart: {symbol} {tf} ({self._chart_source})[/]")
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

        elif cmd == "/learn":
            # /learn           — reflect on the last sub-agent that ran
            # /learn <agent>   — reflect on that specific agent's last session
            target = parts[1] if len(parts) > 1 else self._last_sub_agent
            if not target:
                self._chat_msg(
                    "[red]Usage: /learn [agent][/]  "
                    "[dim](no prior session — run /analyze first)[/]"
                )
                return True
            self._agent_working = True
            self._set_status(f"reflecting on {target}...")
            self.run_worker(self._run_learn_flow(target), thread=False)
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

                    # Remember this agent for /learn-with-no-args and
                    # emit an auto-nudge if the session used enough
                    # tools to be worth reflecting on.
                    self._last_sub_agent = agent_name
                    self._maybe_nudge_learn(agent_name)
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

    def _maybe_nudge_learn(self, agent_name: str) -> None:
        """Emit a one-line hint if the agent's last session is worth reflecting on.

        Fires when the agent used ``NUDGE_THRESHOLD`` or more tools in
        a single task AND did not call ``skill_manage(create)`` during
        that same task. Silent otherwise.
        """
        try:
            mgr = getattr(self, "_sub_agent_manager", None)
            if not mgr or agent_name not in mgr.agents:
                return
            agent = mgr.agents[agent_name]
            session = agent.get_last_session()
            if not session:
                return
            if session.tool_count < NUDGE_THRESHOLD:
                return
            if session.skill_was_created():
                return
            self._chat_msg(
                f"[dim]Tip: {agent_name} used {session.tool_count} tools. "
                f"Run [bold]/learn {agent_name}[/] to distill this session into a skill.[/]"
            )
        except Exception:
            # Nudging should never crash the terminal — if the hint
            # path has any issue we silently skip.
            pass

    # ── /learn reflection flow ────────────────────────────────

    async def _run_learn_flow(self, target_agent: str) -> None:
        """Collect a reflection bundle for ``target_agent`` and hand it to the mentor."""
        chat = self.query_one("#chat-panel", ChatPanel)
        try:
            mgr = getattr(self, "_sub_agent_manager", None)
            if not mgr:
                chat.append_message(
                    "[red]/learn requires the sub-agent manager — not available in this mode.[/]"
                )
                return
            if target_agent not in mgr.agents:
                chat.append_message(
                    f"[red]No running {target_agent} sub-agent — run a task with it first "
                    "(e.g. /analyze BTC).[/]"
                )
                return

            target = mgr.agents[target_agent]
            session = target.get_last_session()
            if not session:
                chat.append_message(
                    f"[red]{target_agent} has no prior session to reflect on.[/]"
                )
                return

            existing = target.list_existing_skills()
            # chat_turns: the TUI owns these — use the on-screen history.
            chat_turns = list(getattr(chat, "message_texts", [])) if hasattr(chat, "message_texts") else []
            bundle = session.to_bundle(chat_turns=chat_turns, existing_skills=existing)

            # Ensure the mentor is running
            if "mentor" not in mgr.agents:
                chat.append_message("[dim]Spawning mentor...[/]")
                await mgr.spawn("mentor")

            # Build the reflection prompt — the mentor's SOUL tells it
            # the expected bundle shape and reply format. We hand it a
            # pretty-printed JSON bundle + a reminder of the output
            # contract so it's obvious what to return.
            reflection_prompt = (
                "You have been called via the /learn reflection flow. Read the "
                "bundle below and decide whether it warrants a new skill, a "
                "patch to an existing skill, or no skill at all. Follow your "
                "how-to-reflect-on-a-session meta-skill. Return a structured "
                "reply with DECISION:, TARGET_AGENT:, SKILL_NAME:, OP:, and "
                "either SKILL_CONTENT: (for create) or OLD_STRING:/NEW_STRING: "
                "(for patch).\n\n"
                f"REFLECTION_BUNDLE:\n{json.dumps(bundle, indent=2, default=str)}"
            )

            chat.append_message(f"[dim]Reflecting on {target_agent} (tool_count={session.tool_count})...[/]")

            reply = await self.bus.request(
                "agent.mentor.request",
                {"task": reflection_prompt, "from": self.bus.agent_name},
                timeout=180,
            )
            mentor_reply = reply.get("response", str(reply))
            chat.append_message(f"[bold magenta][mentor][/] {mentor_reply}")

            parsed = parse_mentor_reply(mentor_reply)
            outcome = await self._apply_mentor_decision(parsed, target_agent)

            # Persist the full reflection so the user and I can review
            # later — path printed in chat so the user can find it.
            try:
                path = save_reflection_record(bundle, mentor_reply, outcome)
                chat.append_message(f"[dim]Reflection saved: {path}[/]")
            except Exception as e:  # noqa: BLE001
                chat.append_message(f"[dim]Reflection save failed: {e}[/]")

        except Exception as e:
            chat.append_message(f"[bold red]/learn error: {e}[/]", "error-msg")
        finally:
            self._agent_working = False
            self._set_status("idle")

    async def _apply_mentor_decision(self, parsed: dict, target_agent: str) -> dict:
        """Persist the mentor's drafted skill into the target agent's library.

        Returns a dict summarizing the outcome (kept for the saved
        reflection record so reviews are self-contained).
        """
        chat = self.query_one("#chat-panel", ChatPanel)
        decision = parsed.get("decision", "unknown")

        # Safety: trust the mentor's decision but ALWAYS use the
        # TARGET agent we were invoked with as the skill owner, not
        # whatever TARGET_AGENT the mentor echoed. This prevents a
        # mentor hallucination from writing into the wrong library.
        store = SkillStore(Path(get_skills_dir(target_agent)))

        if decision == "no_skill":
            chat.append_message("[dim]Mentor: no new skill worth capturing.[/]")
            return {"decision": "no_skill"}

        skill_name = parsed.get("skill_name")
        if not skill_name:
            chat.append_message("[yellow]Mentor reply missing SKILL_NAME — skipping persistence.[/]")
            return {"decision": decision, "error": "missing_skill_name"}

        if decision == "create":
            content = parsed.get("content", "")
            if not content:
                chat.append_message("[yellow]Mentor reply missing SKILL_CONTENT — skipping.[/]")
                return {"decision": "create", "error": "missing_content"}
            result = store.create(skill_name, content)
            if result.get("success"):
                chat.append_message(
                    f"[bold green]+ skill created:[/] {target_agent}/{skill_name}"
                )
            else:
                chat.append_message(
                    f"[yellow]Skill create failed: {result.get('error')}[/]"
                )
            return {"decision": "create", "skill_name": skill_name, "result": result}

        if decision == "patch":
            old_s = parsed.get("old_string", "")
            new_s = parsed.get("new_string", "")
            if not old_s:
                chat.append_message("[yellow]Mentor reply missing OLD_STRING — skipping.[/]")
                return {"decision": "patch", "error": "missing_old_string"}
            result = store.patch(skill_name, old_s, new_s)
            if result.get("success"):
                chat.append_message(
                    f"[bold green]~ skill patched:[/] {target_agent}/{skill_name}"
                )
            else:
                chat.append_message(
                    f"[yellow]Skill patch failed: {result.get('error')}[/]"
                )
            return {"decision": "patch", "skill_name": skill_name, "result": result}

        chat.append_message(f"[yellow]Mentor returned unknown decision: {decision}[/]")
        return {"decision": decision, "error": "unknown_decision"}

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

    def _on_live_signal(self, sig) -> None:
        """Route a live signal from the SignalConsumer to the AlertsPanel.

        Called synchronously from the consumer's ``_ingest`` method.
        Uses ``call_from_thread`` to be safe if the NATS callback
        fires from a background thread.
        """
        try:
            alerts = self.query_one("#alerts-panel", AlertsPanel)
            alerts.add_signal(
                sig.source,
                sig.symbol,
                sig.signal_type,
                sig.summary(),
            )
            self._nats_log(f"[bold yellow]SIGNAL[/] {sig.summary()}")
        except Exception:
            pass

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

    def action_cycle_symbol(self):
        """Cycle the chart through the current watchlist.

        Uses the live watchlist (not the TRACKED_SYMBOLS constant) so
        symbols the user added via ``/watch`` are included in the
        rotation. Wraps around at the end.
        """
        try:
            watchlist = self.query_one("#watchlist-panel", WatchlistPanel)
        except Exception:
            return
        symbols = list(watchlist.tracked_symbols)
        if not symbols:
            self._chat_msg("[dim]Watchlist is empty. Use /watch SYMBOL to add one.[/]")
            return
        try:
            idx = symbols.index(self._chart_symbol)
        except ValueError:
            idx = -1
        next_symbol = symbols[(idx + 1) % len(symbols)]
        tf = self.TIMEFRAMES[self._current_tf_idx]
        self.run_worker(self._load_chart(next_symbol, tf), thread=False)
        self._chat_msg(f"[dim]Chart: {next_symbol} {tf}[/]")

    def action_toggle_watchlist_add(self):
        """Focus input with /watch prefix."""
        inp = self.query_one("#input-area", Input)
        inp.value = "/watch "
        inp.focus()

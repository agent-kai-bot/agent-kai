"""KAI Trading Terminal — multi-panel crypto TUI."""

import asyncio
import json
import time
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
from agent_logger import get_logger
from config import get_skills_dir
from tui.panels.alerts import AlertsPanel
from tui.panels.agent_chat import ChatPanel
from tui.panels.chart import ChartPanel
from tui.panels.history_input import HistoryInput
from tui.panels.positions import PositionsPanel
from tui.panels.queue_row import QueuedInputRow
from tui.panels.watchlist import WatchlistPanel

# Cloud market data endpoint (agent-k.ai). Bearer-authenticated via the
# user's AGENT_KAI_API_KEY (env var, .env file, or AGENT-KAI-API-KEY.txt
# in the project root — auto-loaded by config.py at import time).
KAI_API_BASE = "https://agent-k.ai/v1"

TRACKED_SYMBOLS = ["BTC", "ETH", "SOL"]

# Maximum number of inputs that can stack up while the agent is busy.
# Anything beyond this is rejected with a "queue full" message rather
# than allowing unbounded type-ahead. 10 is a generous upper bound for
# realistic interactive use; the user can always /queue clear and
# retry if they hit the cap.
MAX_INPUT_QUEUE = 10


def _kai_api_headers() -> dict:
    """Bearer-auth headers for cloud agent-k.ai market data calls."""
    import os
    key = os.environ.get("AGENT_KAI_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _normalize_kai_bar(arr: list) -> dict:
    """Convert a positional cloud bar to the dict shape the chart panel expects.

    Cloud `/v1/market/ohlcv` returns each bar as
    ``[ts_ms, open, high, low, close, volume]``. Local data_api returns
    each bar as ``{"ts": "...", "open": ..., ...}``. The chart panel
    expects the dict shape, so we adapt here.
    """
    if not isinstance(arr, list) or len(arr) < 6:
        return {}
    from datetime import datetime, timezone
    ts_ms = int(arr[0])
    return {
        "ts": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "open": float(arr[1]),
        "high": float(arr[2]),
        "low": float(arr[3]),
        "close": float(arr[4]),
        "volume": float(arr[5]),
    }

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
        ("ctrl+y", "copy_last_response", "Copy last reply"),
        ("ctrl+shift+c", "copy_selection", "Copy selection"),
    ]

    TIMEFRAMES = ["1m", "5m", "15m", "1h"]

    def __init__(self, agent_runner, bus=None, signal_consumer: SignalConsumer | None = None):
        super().__init__()
        self.agent_runner = agent_runner
        self.bus = bus
        self._agent_working = False
        # File logger so chart / signal / feed errors land in
        # logs/tui_YYYY-MM-DD.log instead of disappearing when the
        # TUI closes. Mirrors the per-agent logger pattern.
        self.logger = get_logger("tui")
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
        # Default to the cloud agent-k.ai market data API. Self-hosted
        # users can /chart source local to switch to a local data_api,
        # or /chart source coinbase for a direct Coinbase feed.
        self._chart_source: str = "kai-api"
        self._coinbase_stream = None  # CoinbaseCandleStream instance
        self._coinbase_task = None    # asyncio.Task running the feed loop
        self._kai_api_stream = None   # KaiApiCandleStream instance
        self._kai_api_task = None     # asyncio.Task running the WS consumer
        self._kai_api_refresh_task = None  # asyncio.Task running the periodic REST refresh
        self._kai_api_last_refresh: float = 0.0  # epoch timestamp of last successful REST refetch
        # Last text the auto-copy-on-mouseup handler pushed to the
        # OS clipboard. Used to suppress duplicate OSC 52 emissions
        # when TextSelected fires repeatedly during a single drag.
        self._last_auto_copied: str = ""
        # System clipboard backend, lazily detected on first copy.
        # One of: "wl-copy", "xclip", "xsel", "osc52".  See
        # _detect_clipboard_backend for the picking logic and
        # _set_system_clipboard for the actual write path.
        self._clipboard_backend: str | None = None
        # Inputs that arrived while the agent was busy. Queued
        # FIFO and drained from every busy-task finally block via
        # _drain_input_queue, mirroring how a shell stacks up
        # commands when you keep typing while the previous one
        # hasn't returned. Empty list = nothing pending.
        self._input_queue: list[str] = []
        # Path where chat_history is persisted across TUI restarts.
        # Saved after every successful turn (in _process_agent's
        # finally block) AND on app teardown via on_unmount, so
        # closing the TUI / killing it / Ctrl+C all preserve the
        # most recent conversation. Loaded in on_mount before the
        # welcome banner so the user sees their old session
        # immediately on relaunch.
        self._chat_history_path = Path("workspaces/terminal/chat_history.json")
        # Widget references for the queued items, kept in lockstep
        # with _input_queue so each row's [X] click can locate and
        # drop its matching string. Same length, same order, same
        # mutations — never one without the other.
        self._queue_widgets: list[QueuedInputRow] = []
        # Auto-trade gate. When True, signal_handlers configured
        # to dispatch to the trader sub-agent (or any agent in
        # AUTOTRADE_GATED_AGENTS) are allowed to fire. When False,
        # those handlers are skipped with a "gated — autotrade off"
        # log line in chat. Toggled at runtime via /autotrade on|off.
        # Persisted to workspaces/terminal/state.json so the setting
        # survives restarts. Default is OFF — accidentally leaving
        # autotrade enabled across a restart shouldn't be possible
        # without explicit user opt-in.
        self._autotrade_enabled: bool = False
        # SignalHandlerRunner is constructed in on_mount once the
        # NATS bus is connected and the chat panel is mounted (the
        # dispatchers need both). Initialized to None here so the
        # _on_live_signal callback can no-op gracefully if it fires
        # before construction.
        self._signal_handler_runner = None

        # Restore persisted chart state from workspaces/terminal/state.json.
        # Falls back to the BTC + 1m defaults on first run or corrupt file.
        state = _load_terminal_state()
        self._chart_symbol = state.get("chart_symbol", "BTC")
        saved_tf = state.get("chart_timeframe", "1m")
        self._saved_color_scheme = state.get("chart_color_scheme", "classic")
        self._chart_source = state.get("chart_source", "kai-api")
        self._autotrade_enabled = bool(state.get("autotrade_enabled", False))
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
        # HistoryInput gives us bash-style up/down arrow recall of
        # previously submitted lines. The history file lives next
        # to the rest of the terminal state and persists across
        # restarts (one entry per line, like .bash_history).
        yield HistoryInput(
            placeholder="/buy BTC 0.1 | /analyze SOL | /scan trending | /learn | or just chat...",
            id="input-area",
            history_path=Path("workspaces/terminal/input_history.txt"),
        )

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
                if name == "kai":
                    continue
                desc = cfg.get("description", "")[:35]
                self._nats_log(f"  [dim]{name}[/] [dim italic]{desc}[/]")

            # Subscribe to market data subjects
            await self.bus.subscribe("market.>", self._handle_market_data)
            await self.bus.subscribe("portfolio.>", self._handle_portfolio_data)
            await self.bus.subscribe("alert.>", self._handle_alert)

            # Subscribe the signal consumer to live signal scanner events.
            # Signals arriving on ``signals.>`` are buffered in the
            # consumer and also routed to the AlertsPanel + the
            # SignalHandlerRunner via the on_signal callback.
            await self.signal_consumer.subscribe(self.bus)
            self.signal_consumer.on_signal = self._on_live_signal

            # Build the SignalHandlerRunner now that the bus + chat
            # panel are wired up. Loads the signal_handlers block
            # from agent-config.json, parses each entry, and binds
            # the action dispatchers (dispatch_agent, dispatch_kai,
            # chat_message, publish, webhook) to the live bus +
            # chat + agent_runner. From this point forward every
            # signal that lands in _on_live_signal also walks the
            # handler list.
            self._build_signal_handler_runner()

        # Restore saved chart color scheme. If the saved name no
        # longer exists (e.g. the legacy "default" scheme that we
        # renamed), log it and fall through — the panel constructor
        # already picked the new default and the next state save
        # will persist that.
        try:
            chart = self.query_one("#chart-panel", ChartPanel)
            if not chart.set_color_scheme(self._saved_color_scheme):
                self.logger.info(
                    "saved chart color scheme %r is unknown, using current default %r",
                    self._saved_color_scheme,
                    chart.color_scheme,
                )
        except Exception as exc:
            self.logger.warning("chart color scheme restore failed: %s", exc)

        # Load initial data
        self.run_worker(self._load_initial_data(), thread=False)

        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message("[bold dim]Welcome to KAI. Type a message or use slash commands.[/]")
        chat.append_message(
            "[dim]/buy /sell /analyze /scan /risk /chart /watch /learn /remember "
            "/react /autotrade /model /think /queue /login codex /exit /reset[/]"
        )
        chat.append_message("[dim]Mouse-drag any panel to copy. Ctrl+Y = last reply. Ctrl+Shift+C = current selection.[/]")

        # Restore the previous session's chat history if any. We do
        # this AFTER the welcome banner so the visual order is
        # "welcome → restored history → fresh prompts." If no saved
        # history exists this is a no-op and the user gets a clean
        # start. The restore mounts both the agent's chat_history
        # list (so the LLM has context for the next turn) and the
        # chat panel widgets (so the user sees what was there).
        try:
            n = self._load_chat_history()
            if n > 0:
                chat.append_message(
                    f"[dim italic]restored {n} message{'s' if n != 1 else ''} from "
                    f"the previous session — /reset to start fresh[/]"
                )
        except Exception as exc:
            self.logger.warning("chat history restore failed: %s", exc)

        self.query_one("#input-area", Input).focus()

    async def _load_initial_data(self):
        """Load initial prices and chart data from the active source."""
        try:
            # Seed watchlist prices. Source matches the chart source —
            # kai-api derives the latest close from the most recent
            # OHLCV bar (the cloud doesn't expose a separate /price
            # endpoint), local hits the local data_api, coinbase uses
            # the existing fetch_latest_price client.
            watchlist = self.query_one("#watchlist-panel", WatchlistPanel)
            await asyncio.to_thread(self._seed_watchlist_prices, watchlist, TRACKED_SYMBOLS)

            # Load chart
            await self._load_chart(self._chart_symbol, self.TIMEFRAMES[self._current_tf_idx])

            # Load positions
            await self._refresh_positions()
        except Exception as e:
            self._log_error("init error", e)

    def _seed_watchlist_prices(self, watchlist, symbols: list[str]) -> None:
        """Synchronously fetch latest prices for the watchlist via the
        currently-selected chart source. Runs off the event loop via
        ``asyncio.to_thread`` so the TUI doesn't block.
        """
        if self._chart_source == "coinbase":
            try:
                from agent.data_sources.coinbase import fetch_latest_price
            except Exception as exc:
                self.logger.warning("watchlist coinbase import failed: %s", exc)
                return
            for sym in symbols:
                try:
                    info = fetch_latest_price(sym)
                    watchlist.update_price(sym, info["price"], info.get("volume_24h"))
                except Exception as exc:
                    self.logger.warning("watchlist coinbase price %s failed: %s", sym, exc)
            return

        # Default: kai-api. Derive the latest close from the most
        # recent 1m bar — the cloud doesn't expose a separate /price
        # endpoint and a 1-bar OHLCV call is cheap.
        headers = _kai_api_headers()
        if not headers:
            self.logger.warning(
                "watchlist seed skipped: AGENT_KAI_API_KEY not configured"
            )
            return
        for sym in symbols:
            try:
                r = requests.get(
                    f"{KAI_API_BASE}/market/ohlcv/{sym}",
                    params={"interval": "1m", "limit": 1},
                    headers=headers,
                    timeout=5,
                )
                if r.status_code != 200:
                    self.logger.warning(
                        "watchlist kai-api %s failed: HTTP %s", sym, r.status_code
                    )
                    continue
                bars = r.json().get("data") or []
                if not bars:
                    continue
                last = _normalize_kai_bar(bars[-1])
                watchlist.update_price(sym, last["close"], last["volume"])
            except Exception as exc:
                self.logger.warning("watchlist kai-api %s exception: %s", sym, exc)

    async def _load_chart(self, symbol: str, interval: str):
        """Load chart data using the current source.

        Two sources are supported:
        - ``kai-api`` (default): cloud agent-k.ai REST bootstrap +
          live WebSocket stream. Bearer-authenticated via
          AGENT_KAI_API_KEY. This is the cloud-first default that
          works out of the box for any user with a key.
        - ``coinbase``: direct Coinbase REST + WebSocket feed (no
          auth required, BTC-USD focused).

        Anything else falls back to kai-api so stale state from an
        earlier dev iteration can never leave the chart in a broken
        "Chart load error: localhost" state.

        All call sites (slash commands, watchlist clicks, timeframe
        cycling) funnel through here so the selected source is
        always honored.
        """
        if self._chart_source == "coinbase":
            await self._start_coinbase_feed(symbol, interval)
            return

        # Default + safety net: anything other than coinbase routes
        # through the cloud kai-api feed.
        if self._chart_source != "kai-api":
            self.logger.info(
                "chart source '%s' is no longer supported, falling back to kai-api",
                self._chart_source,
            )
            self._chart_source = "kai-api"
        await self._load_chart_from_kai_api(symbol, interval)

    async def _load_chart_from_kai_api(self, symbol: str, interval: str) -> None:
        """Hand off to the kai-api WebSocket feed lifecycle.

        REST historical bootstrap (up to 200 bars) seeds the chart,
        then a live WebSocket subscription on the same channel keeps
        it updated as new candles tick. See ``_start_kai_api_feed``
        for the full sequence.
        """
        await self._start_kai_api_feed(symbol, interval)

    async def _start_kai_api_feed(self, symbol: str, interval: str) -> None:
        """Start (or restart) the cloud agent-k.ai chart feed.

        1) Stop any existing kai-api WS task and Coinbase feed
        2) Fetch historical bars via REST (200 bars, oldest → newest)
        3) Seed the chart panel with the historical window
        4) Spin up a KaiApiCandleStream consumer task that listens for
           live ``event`` frames and overwrites / appends bars on the
           chart as they arrive
        """
        await self._stop_kai_api_feed()
        await self._stop_coinbase_feed()

        try:
            from agent.data_sources.kai_api import (
                KaiApiCandleStream,
                fetch_candles,
            )
        except Exception as e:
            self._log_error("kai-api module import failed", e)
            return

        sym_upper = symbol.upper()

        # 1) Historical bootstrap (off the event loop)
        try:
            hist = await asyncio.to_thread(fetch_candles, sym_upper, interval, 200)
        except RuntimeError as e:
            self._nats_log(
                f"[red]kai-api requires an API key — drop AGENT-KAI-API-KEY.txt "
                f"at the project root or set AGENT_KAI_API_KEY: {e}[/]"
            )
            return
        except Exception as e:
            self._log_error("kai-api REST fetch failed", e)
            return

        # Populate the chart with the historical window
        try:
            chart = self.query_one("#chart-panel", ChartPanel)
            chart.set_data(sym_upper, interval, hist)
        except Exception as e:
            self._log_error("chart set_data failed (kai-api path)", e)
            return

        self._chart_symbol = sym_upper
        try:
            self._current_tf_idx = self.TIMEFRAMES.index(interval)
        except ValueError:
            pass
        self._save_chart_state(interval)

        self._kai_api_last_refresh = time.time()

        # 2) Live WebSocket consumer
        self._kai_api_stream = KaiApiCandleStream(sym_upper, interval)
        self._kai_api_task = asyncio.create_task(
            self._run_kai_api_consumer(sym_upper, interval)
        )

        # 3) Periodic REST refresh — safety net for the WS path.
        # The WebSocket gives sub-second updates when it's working,
        # but it can stall for several reasons: connection drops
        # silently mid-session, the cloud only emits on bar-close,
        # network blips that make reconnect-with-backoff slow,
        # auth-token expiry mid-connection. The periodic refetch
        # guarantees the chart never drifts more than ~20s from
        # the backend's truth regardless of WS health. Re-bases
        # via chart.set_data(), which clears + replaces all bars
        # — any in-progress current candle the WS painted gets
        # rewritten on the next WS tick so nothing is permanently
        # lost. Cheap: ~16KB per refetch, every 20s, against the
        # already-bearer-authed REST endpoint.
        self._kai_api_refresh_task = asyncio.create_task(
            self._run_kai_api_periodic_refresh(sym_upper, interval)
        )

        self._nats_log(
            f"[bold cyan]kai-api WS[/] {sym_upper} {interval} live "
            f"({len(hist)} bars seeded, REST refresh every 20s)"
        )

    async def _stop_kai_api_feed(self) -> None:
        """Cleanly stop any running kai-api feed task + WS stream + refresh."""
        if self._kai_api_stream is not None:
            try:
                self._kai_api_stream.stop()
            except Exception:
                pass
        task = self._kai_api_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Tear down the periodic REST refresh task too
        refresh = self._kai_api_refresh_task
        if refresh is not None and not refresh.done():
            refresh.cancel()
            try:
                await refresh
            except (asyncio.CancelledError, Exception):
                pass
        self._kai_api_stream = None
        self._kai_api_task = None
        self._kai_api_refresh_task = None

    async def _run_kai_api_consumer(self, symbol: str, interval: str) -> None:
        """Consume the kai-api WebSocket stream, updating the chart live.

        Each event bar carries an ``is_closed`` flag — True means the
        candle has finalized, False means it's the still-forming
        current candle. We use ``update_last_bar`` for both because
        the chart panel merges by ts: live updates overwrite the
        in-progress bar, and the next ts boundary creates a new one
        automatically.

        Updates ``self._kai_api_last_refresh`` on every received bar
        so the periodic-refresh task can tell the WS is healthy.

        Surfaces WS state in the NATS log panel for in-TUI debug:
          - first frame received -> "WS first frame"
          - frames dropped by symbol/interval filter -> count, summary every 30s
          - 30s+ silence -> "WS idle (Ns since last frame)" warning
          - errors -> red error message
        """
        ws_frames_received = 0
        ws_frames_filtered_symbol = 0
        ws_frames_filtered_interval = 0
        ws_frames_kept = 0
        first_frame_logged = False
        last_status_log = time.time()
        last_frame_at = time.time()

        try:
            chart = self.query_one("#chart-panel", ChartPanel)
            async for bar in self._kai_api_stream:
                ws_frames_received += 1
                last_frame_at = time.time()

                if not first_frame_logged:
                    self._nats_log(
                        f"[bold green]kai-api WS first frame[/] {symbol} {interval}"
                    )
                    first_frame_logged = True

                # Filter to the symbol/interval we asked for. Track
                # drops separately so we can tell whether the WS is
                # delivering events but filtering them out — that
                # would explain a "WS connected, no chart updates"
                # symptom.
                bar_symbol = bar.get("symbol")
                if bar_symbol and bar_symbol != symbol:
                    ws_frames_filtered_symbol += 1
                    continue
                bar_interval = bar.get("interval")
                if bar_interval and bar_interval != interval:
                    ws_frames_filtered_interval += 1
                    continue

                # Strip the metadata fields the chart panel doesn't need
                clean = {
                    "ts": bar["ts"],
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                }
                chart.update_last_bar(clean)
                ws_frames_kept += 1
                self._kai_api_last_refresh = time.time()

                # Periodic stats line every 30 seconds — gives the user
                # a live view of WS health from inside the TUI.
                now = time.time()
                if now - last_status_log >= 30.0:
                    self._nats_log(
                        f"[dim]kai-api WS stats: {ws_frames_received} frames "
                        f"({ws_frames_kept} kept, "
                        f"{ws_frames_filtered_symbol} sym-filtered, "
                        f"{ws_frames_filtered_interval} int-filtered) "
                        f"last={int(now - last_frame_at)}s ago[/]"
                    )
                    last_status_log = now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error("kai-api WS consumer error", e)
            self._nats_log(f"[bold red]kai-api WS consumer crashed:[/] {e}")

    # Periodic REST refresh interval. Re-fetches historical bars from
    # the cloud kai-api endpoint and rebases the chart even when the
    # WebSocket is broken / throttled / silent. The interval is short
    # enough that the chart never drifts more than ~20 seconds from
    # backend truth, but long enough that we're not hammering the
    # endpoint or burning bandwidth.
    KAI_API_REFRESH_INTERVAL_S = 20.0

    async def _run_kai_api_periodic_refresh(
        self, symbol: str, interval: str
    ) -> None:
        """Periodically re-fetch historical bars and rebase the chart.

        Runs alongside the WebSocket consumer as a safety net. The
        WS gives sub-second updates when it's healthy, but it can
        stall for several reasons (silent disconnect, backend only
        emits on bar-close, slow reconnect backoff, expired auth).
        This task guarantees the chart stays close to fresh
        regardless of WS state.

        Cancels cleanly via ``asyncio.CancelledError`` when the
        feed is stopped (chart source switched, symbol/interval
        changed, TUI quitting).
        """
        from agent.data_sources.kai_api import fetch_candles

        # Initial sleep so we don't immediately re-fetch on top of
        # the bootstrap that just ran in _start_kai_api_feed.
        try:
            await asyncio.sleep(self.KAI_API_REFRESH_INTERVAL_S)
        except asyncio.CancelledError:
            raise

        consecutive_failures = 0
        refresh_count = 0
        last_visible_status = time.time()
        while True:
            try:
                hist = await asyncio.to_thread(
                    fetch_candles, symbol, interval, 200
                )
                if hist:
                    try:
                        chart = self.query_one("#chart-panel", ChartPanel)
                        chart.set_data(symbol, interval, hist)
                        self._kai_api_last_refresh = time.time()
                        refresh_count += 1
                        # If we just recovered from a failure burst,
                        # surface that in the NATS log so the user
                        # can see the chart is fresh again.
                        if consecutive_failures > 0:
                            self._nats_log(
                                f"[bold green]kai-api REST refresh recovered[/] "
                                f"after {consecutive_failures} failures"
                            )
                        consecutive_failures = 0
                        # Periodic visible status — once every 5 minutes,
                        # confirm in the NATS log that the safety net is
                        # firing so the user has running confirmation.
                        # 5 minutes = 15 refresh ticks at the default 20s
                        # interval.
                        now = time.time()
                        if now - last_visible_status >= 300.0:
                            last = hist[-1] if hist else {}
                            self._nats_log(
                                f"[dim]kai-api REST safety net: "
                                f"{refresh_count} refreshes, "
                                f"latest bar={last.get('close', '?')}[/]"
                            )
                            last_visible_status = now
                    except Exception as exc:
                        self.logger.warning(
                            "kai-api periodic refresh: chart update failed: %s", exc
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                # Log the first few failures, then go quiet so a
                # persistent backend outage doesn't spam the log.
                # Surface the first failure in the NATS log too so
                # the user immediately knows the safety net is down.
                if consecutive_failures == 1:
                    self._nats_log(
                        f"[bold red]kai-api REST refresh failed:[/] {exc}"
                    )
                if consecutive_failures <= 3:
                    self.logger.warning(
                        "kai-api periodic refresh failed (#%d): %s",
                        consecutive_failures, exc,
                    )
                elif consecutive_failures == 4:
                    self.logger.warning(
                        "kai-api periodic refresh: silencing further failures "
                        "until recovery"
                    )
                    self._nats_log(
                        "[dim red]kai-api REST refresh: silencing log spam "
                        "(will surface on recovery)[/]"
                    )

            try:
                await asyncio.sleep(self.KAI_API_REFRESH_INTERVAL_S)
            except asyncio.CancelledError:
                raise

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
            self._log_error("Coinbase module import failed", e)
            return

        product_id = normalize_product_id(symbol)

        # 1) Historical bootstrap via REST (off the event loop)
        try:
            hist = await asyncio.to_thread(
                fetch_candles, product_id, interval, 120
            )
        except Exception as e:
            self._log_error(f"Coinbase historical fetch failed ({product_id} {interval})", e)
            return

        # Populate the chart with the historical window
        try:
            chart = self.query_one("#chart-panel", ChartPanel)
            chart.set_data(product_id, interval, hist)
        except Exception as e:
            self._log_error("chart set_data failed (coinbase path)", e)
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
            self._log_error("Coinbase WS consumer error", e)

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
                    self._log_error("Coinbase poll error", e)
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
        # Multi-line paste handling: if the input has a buffered
        # paste, the visible value is just a "[paste: N lines]"
        # summary — the full text lives in HistoryInput's
        # _pasted_buffer. take_pasted_buffer() returns and clears
        # it. Falls through to event.value for plain typed input
        # and single-line pastes.
        pasted = None
        take = getattr(event.input, "take_pasted_buffer", None)
        if callable(take):
            pasted = take()
        if pasted is not None:
            # Strip only leading/trailing whitespace; preserve
            # internal newlines so the agent sees the original
            # structure (stack traces, code blocks, etc).
            text = pasted.strip()
        else:
            text = event.value.strip()
        if not text:
            return
        # Record in shell-style history before clearing the input
        # so the user can recall it with Up arrow on the next prompt.
        # Multi-line pastes are NOT added to history — they're
        # content the user copied from somewhere else, not a typed
        # command worth replaying via Up arrow.
        # Cast guard: every Input we mount in compose() is a
        # HistoryInput, but a stray plain Input would still hit this
        # handler, so we feature-detect rather than isinstance-check
        # to avoid an extra import in this hot path.
        if pasted is None:
            remember = getattr(event.input, "remember", None)
            if callable(remember):
                remember(text)
        event.input.value = ""

        # Busy → queue instead of dropping. The queue is drained
        # FIFO from every busy-task finally block by
        # _drain_input_queue, so the user can stack up commands
        # while the agent is mid-task and they'll execute one after
        # another in submission order — same UX as typing ahead in
        # bash while a long-running command is in flight.
        #
        # Each queued item also gets a clickable row in chat with an
        # [X] button so the user can drop individual items without
        # waiting for them to come up (or use /queue clear to nuke
        # the whole queue at once).
        if self._agent_working:
            self._queue_item(text)
            return

        await self._dispatch_input(text)

    def _queue_item(self, text: str) -> None:
        """Append text to _input_queue AND mount its [X]-clickable row.

        Keeps ``_input_queue`` and ``_queue_widgets`` in lockstep —
        same length, same order. Both lists must be mutated together
        from this method (and ``_drain_input_queue`` /
        ``_drop_queue_item`` for removal) so the row index always
        maps to the matching string.

        Enforces ``MAX_INPUT_QUEUE`` as a hard cap. Inputs beyond the
        cap are rejected with a chat message rather than silently
        accepted (which would lead to runaway memory + endless
        replays). The user can ``/queue clear`` to flush and retry.
        """
        if len(self._input_queue) >= MAX_INPUT_QUEUE:
            preview = text[:60].replace("\n", " ")
            self._chat_msg(
                f"[bold red]queue full ({MAX_INPUT_QUEUE} max) — dropped:[/] "
                f"[dim]{preview}[/]"
            )
            self._chat_msg(
                "[dim]Use /queue clear to flush, or /queue drop N to remove a specific item.[/]"
            )
            return
        self._input_queue.append(text)
        position = len(self._input_queue)
        row = QueuedInputRow(text, position)
        self._queue_widgets.append(row)
        chat = self.query_one("#chat-panel", ChatPanel)
        chat.mount(row)
        chat.scroll_end(animate=False)

    async def _dispatch_input(self, text: str) -> None:
        """Run a single user input through the chat → slash → agent path.

        Shared between fresh submissions from ``on_input_submitted``
        and queue drains from ``_drain_input_queue`` so both code
        paths produce identical UX (user-msg widget appears, slash
        commands route, the main agent gets the prompt if nothing
        claimed it).

        Caller's responsibility: only call this when ``_agent_working``
        is False. The fresh-submission path checks busy before
        calling; the drain path enforces the same invariant.

        End-of-turn drain: if the dispatched command was a non-busy
        slash command (like ``/chart`` or ``/think``) that returned
        without setting ``_agent_working``, no worker finally block
        will fire to pull the next queued item. We drain inline at
        the end to keep back-to-back synchronous commands flushing
        immediately. Busy-setting commands rely on the worker's
        finally block to drain when their work completes.
        """
        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message(f"[bold green]> {text}[/]", "user-msg")

        # Parse slash commands
        routed = await self._handle_slash_command(text)
        if routed:
            # Synchronous slash commands (everything that does NOT
            # set _agent_working before returning) need an explicit
            # drain here — there's no worker finally to do it for
            # them. Busy-setting commands (/buy, /sell, /analyze,
            # /scan, /risk, /learn, the default agent path) all
            # spawn workers whose finally calls drain.
            if not self._agent_working:
                self._drain_input_queue()
            return

        # Default: send to main agent
        self._agent_working = True
        self._set_status("thinking...")
        self.run_worker(self._process_agent(text), thread=False)

    def _drain_input_queue(self) -> None:
        """Pop one queued input and dispatch it on a fresh worker.

        Called from every code path that resets ``_agent_working``
        to False — the finally blocks of ``_process_agent``,
        ``_run_agent_task``, and ``_run_learn_command``, plus the
        synchronous-slash-command tail in ``_dispatch_input``.

        Race-safety: this method is intentionally synchronous (no
        ``await``) so it cannot be interleaved with another
        coroutine. Between the caller's ``self._agent_working = False``
        and this pop+dispatch, no other coroutine can run, which
        means a user input arriving in the same tick lands in the
        queue (because we'll have set busy=True via the new worker
        before the user's submission is processed) instead of
        racing to grab the slot.

        If for any reason ``_agent_working`` is already True when
        this is called (e.g. someone else grabbed the slot first),
        we leave the queue alone and return — the next finally
        block to fire will pick up where we left off.
        """
        if self._agent_working:
            return
        if not self._input_queue:
            return
        next_text = self._input_queue.pop(0)
        # Pop the matching widget and remove it from chat — the
        # "running" message below replaces it visually.
        next_row = self._queue_widgets.pop(0) if self._queue_widgets else None
        if next_row is not None:
            try:
                next_row.remove()
            except Exception:
                pass
        # Renumber the rest of the queue so (#N) labels stay accurate.
        self._renumber_queue_widgets()

        remaining = len(self._input_queue)
        chat = self.query_one("#chat-panel", ChatPanel)
        suffix = f" ({remaining} more queued)" if remaining else ""
        chat.append_message(
            f"[dim italic]→ running queued: {next_text[:60]}{suffix}[/]"
        )
        # Schedule on a fresh worker so the calling finally block
        # can complete cleanly without nesting agent loops.
        self.run_worker(self._dispatch_input(next_text), thread=False)

    def _renumber_queue_widgets(self) -> None:
        """Sync the (#N) labels on every queued row with their FIFO index.

        Call after any queue mutation (drain, X click, /queue clear)
        so a queue of [a, b, c] always renders as #1 #2 #3 even
        after #2 was removed.
        """
        for i, row in enumerate(self._queue_widgets):
            row.set_position(i + 1)

    def _drop_queue_item(self, row: "QueuedInputRow") -> None:
        """Remove a single queued item by its widget reference.

        Triggered by an [X] click on the row (via the
        ``QueuedInputRow.Removed`` message routed through
        ``on_queued_input_row_removed``) and also reused by
        ``/queue clear`` to flush the whole list one item at a time.
        Synchronous and safe to call from anywhere — no await,
        no race with the queue mutators.
        """
        try:
            idx = self._queue_widgets.index(row)
        except ValueError:
            # Already gone — perhaps a drain raced with the click.
            # Make sure the widget is removed from the DOM either way.
            try:
                row.remove()
            except Exception:
                pass
            return
        self._queue_widgets.pop(idx)
        if 0 <= idx < len(self._input_queue):
            self._input_queue.pop(idx)
        try:
            row.remove()
        except Exception:
            pass
        self._renumber_queue_widgets()

    def on_queued_input_row_removed(self, message: "QueuedInputRow.Removed") -> None:
        """Handle the user clicking [X] on a queued row.

        Textual auto-dispatches ``QueuedInputRow.Removed`` to this
        snake_case-named method on any ancestor that defines it.
        We delegate to ``_drop_queue_item`` so the same code path
        is shared with ``/queue clear``.
        """
        self._drop_queue_item(message.row)

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
                        "[dim](available: kai-api, coinbase)[/]"
                    )
                    return True
                source_name = parts[2].lower()
                if source_name not in ("kai-api", "coinbase"):
                    self._chat_msg(
                        f"[red]Unknown source '{source_name}'. Use: kai-api, coinbase[/]"
                    )
                    return True
                if source_name == self._chart_source:
                    self._chat_msg(f"[dim]Already on {source_name} source[/]")
                    return True
                # Stop any in-flight feeds before switching — both
                # coinbase and kai-api own background tasks that need
                # to be cancelled or they'll keep updating the chart
                # with stale data from the previous source.
                await self._stop_coinbase_feed()
                await self._stop_kai_api_feed()
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

        elif cmd == "/login":
            # /login codex — open browser, run OAuth flow, save tokens
            provider = parts[1].lower() if len(parts) > 1 else ""
            if provider != "codex":
                self._chat_msg(
                    "[red]Usage: /login codex[/]  "
                    "[dim](authenticates against your ChatGPT subscription)[/]"
                )
                return True
            self._chat_msg("[dim]Starting Codex OAuth flow — a browser window will open...[/]")
            self.run_worker(self._run_codex_login(), thread=False)
            return True

        elif cmd == "/model":
            # /model                  — show current model for every agent
            # /model AGENT            — show that agent's current model
            # /model AGENT EP/MODEL   — switch the agent to a new (endpoint, model) pair
            return await self._handle_model_command(parts)

        elif cmd == "/think":
            # /think                  — show kai's current thinking level
            # /think LEVEL            — set kai's thinking level
            # /think AGENT            — show AGENT's current thinking level
            # /think AGENT LEVEL      — set AGENT's thinking level
            return await self._handle_think_command(parts)

        elif cmd == "/queue":
            # /queue                  — show how many items are pending
            # /queue clear            — drop everything in the queue
            return self._handle_queue_command(parts)

        elif cmd == "/remember":
            # /remember [hint]        — ask kai to summarize the recent
            #                           discussion and save it as a skill
            #                           (or memory entry) in its library
            return await self._handle_remember_command(parts)

        elif cmd == "/react":
            # /react [hint]           — manual trigger: ask kai to scan
            #                           the recent signal feed and react
            #                           (the in-the-loop manual companion
            #                           to the configured signal_handlers)
            return await self._handle_react_command(parts)

        elif cmd == "/autotrade":
            # /autotrade              — show current state
            # /autotrade on|off       — toggle the autotrade gate that
            #                           gates trader sub-agent dispatches
            #                           from signal_handlers
            return self._handle_autotrade_command(parts)

        elif cmd in ("/exit", "/quit"):
            # /exit or /quit          — save chat history, then quit
            self._save_chat_history()
            self._chat_msg("[dim]Chat saved. Goodbye.[/]")
            self.exit()
            return True

        elif cmd == "/reset":
            # /reset                  — wipe chat history (visible AND
            #                           the agent's chat_history list AND
            #                           the persisted save file)
            self.action_clear_chat()
            try:
                if self._chat_history_path.exists():
                    self._chat_history_path.unlink()
            except Exception as exc:
                self.logger.warning("delete chat_history.json failed: %s", exc)
            self._chat_msg("[dim]Chat history reset — fresh session.[/]")
            return True

        return False

    async def _handle_remember_command(self, parts: list[str]) -> bool:
        """Ask kai to summarize the recent discussion and save it.

        Builds a prompt template from the user's hint (if any) and
        dispatches it to the main kai agent as a regular chat turn.
        Kai's LLM uses its existing ``memory`` and ``skill_manage``
        tools to perform the actual save — no new tools are required.

        The hint is optional. With no hint, kai picks the most
        recent meaningful discussion thread and summarizes that.
        With a hint, kai uses the hint as a topic anchor for
        scanning recent context.
        """
        hint = " ".join(parts[1:]).strip()
        prompt = self._build_remember_prompt(hint)
        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message(
            f"[dim]Saving discovery{f' about {hint!r}' if hint else ''}...[/]"
        )
        self._agent_working = True
        self._set_status("saving discovery...")
        self.run_worker(self._process_agent(prompt), thread=False)
        return True

    @staticmethod
    def _build_remember_prompt(hint: str) -> str:
        """Compose the /remember prompt template that gets sent to kai."""
        if hint:
            topic_clause = f' about "{hint}"'
        else:
            topic_clause = ""
        return (
            f"The user just ran /remember. Look back at the most recent "
            f"meaningful discussion in our chat history{topic_clause}. "
            f"Summarize what we discovered, then save it for future sessions:\n"
            f"\n"
            f"- If it is a REUSABLE PROCEDURE (a trading strategy with entry "
            f"conditions, exit conditions, indicator parameters, pitfalls, and "
            f"a verification checklist), use the skill_manage tool to create "
            f"a new skill in your library. Use kebab-case for the name "
            f"(e.g. 'ema-cross-with-volume-confirm'). Follow the standard "
            f"skill template with these sections: 'When to use', 'Steps', "
            f"'Pitfalls', 'Verification'. Required frontmatter keys: name, "
            f"description, category (one of analysis / execution / risk), tags.\n"
            f"\n"
            f"- If it is a FACT or PREFERENCE (e.g. 'user prefers 1h analyses', "
            f"'the local 6h endpoint sometimes errors and Coinbase 6h is the "
            f"reliable fallback'), use the memory tool with action='add' to "
            f"add it to your memory store. Use target='memory' for personal "
            f"notes or target='user' for cross-agent user preferences.\n"
            f"\n"
            f"Capture EVERYTHING needed to recreate this discovery from "
            f"scratch. Do not assume future-you remembers anything from "
            f"this session. Be specific about numbers, timeframes, indicator "
            f"settings, market regimes, and any caveats or failure modes "
            f"we found.\n"
            f"\n"
            f"After saving, confirm in your reply: what you saved (skill or "
            f"memory), the name (for skills) or content (for memory entries), "
            f"and a one-line summary of what's now persisted."
        )

    async def _handle_react_command(self, parts: list[str]) -> bool:
        """Manual signal-reaction trigger.

        The configured ``signal_handlers`` block runs automatically on
        every incoming signal. ``/react`` is the in-the-loop manual
        companion: it tells kai to walk the recent ring buffer via
        ``get_signals``, decide which (if any) signals are actionable
        right now, and react — dispatch to the analyst for validation,
        ask the risk-manager for sizing, escalate to the trader if
        ``/autotrade`` is on, or do nothing.

        Forms:
            /react              react to whatever is in the buffer
            /react BTC          filter to BTC signals
            /react clucmay02    filter to a specific strategy

        The hint is just appended to the prompt — kai parses it.
        """
        hint = " ".join(parts[1:]).strip()
        prompt = self._build_react_prompt(hint)
        chat = self.query_one("#chat-panel", ChatPanel)
        chat.append_message(
            f"[dim]Reacting to recent signals{f' ({hint})' if hint else ''}...[/]"
        )
        self._agent_working = True
        self._set_status("reacting to signals...")
        self.run_worker(self._process_agent(prompt), thread=False)
        return True

    def _build_react_prompt(self, hint: str) -> str:
        """Compose the prompt template kai receives from /react."""
        autotrade_state = "ON" if self._autotrade_enabled else "OFF"
        if hint:
            filter_clause = (
                f"\n\nThe user passed this hint: {hint!r}. Use it to filter the "
                f"signals you scan (interpret as a symbol, strategy, or signal_type "
                f"depending on what makes sense)."
            )
        else:
            filter_clause = ""
        return (
            "The user just ran /react. Walk through the recent signal feed and "
            "decide which signals (if any) are actionable RIGHT NOW. Process:\n"
            "\n"
            "1. Call get_signals() with no filters first, limit=10. Read what's "
            "in the buffer.\n"
            "2. For each signal that looks interesting, decide which sub-agent "
            "should validate or act on it:\n"
            "   - Use nats_request('analyst', task) to get an independent "
            "multi-timeframe technical read on the symbol\n"
            "   - Use nats_request('risk-manager', task) to size a position "
            "assuming the signal is real, given current portfolio exposure\n"
            "   - Use nats_request('trader', task) to actually place an order — "
            f"but ONLY if autotrade is enabled (currently autotrade is {autotrade_state})\n"
            "3. Synthesize the sub-agent replies into a single recommendation per "
            "actionable signal. Be honest if the signals are noise — saying "
            "'nothing actionable right now' is a perfectly valid output.\n"
            "4. If autotrade is OFF and you found a high-confidence signal that "
            "would warrant a trade, tell the user explicitly: 'autotrade is off — "
            "if you turn it on with /autotrade on, I would dispatch the trader'.\n"
            f"{filter_clause}\n"
            "\n"
            "Format your final reply as:\n"
            "  - Signals scanned: N\n"
            "  - Actionable: M (with the symbol + strategy + your verdict for each)\n"
            "  - Actions taken: which sub-agents you dispatched and what they "
            "returned\n"
            "  - Recommendations: any signals that would be tradeable if "
            "autotrade were on\n"
        )

    def _handle_autotrade_command(self, parts: list[str]) -> bool:
        """Toggle the autotrade gate that gates trader sub-agent dispatches.

        Forms:
            /autotrade           show current state + the safety story
            /autotrade on        enable (with confirmation message)
            /autotrade off       disable
            /autotrade status    same as /autotrade

        State is persisted to workspaces/terminal/state.json so it
        survives restarts. Default is OFF — accidentally leaving
        autotrade enabled across a restart shouldn't be possible
        without explicit user opt-in.
        """
        sub = parts[1].lower() if len(parts) > 1 else "status"

        if sub in ("", "status"):
            state = "[bold green]ON[/]" if self._autotrade_enabled else "[bold red]OFF[/]"
            self._chat_msg(f"[dim]autotrade: {state}[/]")
            self._chat_msg(
                "[dim]When ON, signal_handlers configured to dispatch to the "
                "trader sub-agent (or any agent in AUTOTRADE_GATED_AGENTS) "
                "are allowed to fire automatically. When OFF, those handlers "
                "log a 'gated' message in chat and skip.[/]"
            )
            self._chat_msg("[dim]/autotrade on  |  /autotrade off[/]")
            return True

        if sub in ("on", "enable", "true", "yes"):
            if self._autotrade_enabled:
                self._chat_msg("[dim]autotrade already ON[/]")
                return True
            self._autotrade_enabled = True
            self._persist_autotrade_state()
            self._chat_msg(
                "[bold yellow]⚠ AUTOTRADE ENABLED[/] [dim]— signal_handlers that "
                "dispatch to the trader sub-agent will now fire automatically on "
                "matching signals. Run /autotrade off to disable.[/]"
            )
            return True

        if sub in ("off", "disable", "false", "no"):
            if not self._autotrade_enabled:
                self._chat_msg("[dim]autotrade already OFF[/]")
                return True
            self._autotrade_enabled = False
            self._persist_autotrade_state()
            self._chat_msg("[bold green]autotrade disabled[/] [dim]— trader handlers will be gated[/]")
            return True

        self._chat_msg(
            "[red]Usage:[/] [dim]/autotrade  |  /autotrade on  |  /autotrade off[/]"
        )
        return True

    def _persist_autotrade_state(self) -> None:
        """Save the current autotrade flag to workspaces/terminal/state.json.

        Reads the existing state, updates the autotrade_enabled key,
        writes back atomically. Failures are swallowed — autotrade
        state is a UX convenience, not load-bearing.
        """
        try:
            state = _load_terminal_state()
            state["autotrade_enabled"] = bool(self._autotrade_enabled)
            _save_terminal_state(state)
        except Exception as exc:
            self.logger.warning("persist autotrade state failed: %s", exc)

    def _handle_queue_command(self, parts: list[str]) -> bool:
        """Inspect or modify the type-ahead input queue.

        Forms:
            /queue              show pending count + (#N): preview list
            /queue clear        drop all queued items at once
            /queue drop N       drop the item at position N (1-indexed)

        Individual items can also be removed by clicking the [X]
        button on each queued row in chat — that path goes through
        ``on_queued_input_row_removed`` -> ``_drop_queue_item``.
        ``/queue drop N`` is the keyboard equivalent: same code path,
        different trigger. ``/queue clear`` flushes the entire queue
        in one shot for the case where the user has 10 things stacked
        up and doesn't want to click each one or count positions.

        ``flush`` and ``wipe`` are aliases for ``clear``. ``drop``
        with no N is rejected with a usage error (it would be
        ambiguous: drop everything? drop the first? Better to make
        the user be explicit).
        """
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub in ("", "list", "show"):
            n = len(self._input_queue)
            if n == 0:
                self._chat_msg("[dim]queue empty[/]")
                return True
            self._chat_msg(
                f"[dim]{n} item{'s' if n != 1 else ''} queued (max {MAX_INPUT_QUEUE}):[/]"
            )
            for i, item in enumerate(self._input_queue, start=1):
                preview = item[:60].replace("\n", " ")
                if len(item) > 60:
                    preview += "…"
                self._chat_msg(f"[dim]  #{i}: {preview}[/]")
            self._chat_msg(
                "[dim]Click [X] on a row, or /queue drop N, or /queue clear[/]"
            )
            return True

        if sub in ("clear", "flush", "wipe"):
            n = len(self._input_queue)
            if n == 0:
                self._chat_msg("[dim]queue already empty[/]")
                return True
            # Drop in reverse so the renumber pass after each
            # removal stays cheap (we always pop the tail). Each
            # _drop_queue_item already handles renumbering.
            for row in list(reversed(self._queue_widgets)):
                self._drop_queue_item(row)
            self._chat_msg(
                f"[dim]queue cleared ({n} item{'s' if n != 1 else ''} dropped)[/]"
            )
            return True

        if sub == "drop":
            # /queue drop N — remove item at 1-indexed position N
            if len(parts) < 3:
                self._chat_msg(
                    "[red]Usage:[/] [dim]/queue drop N[/]  "
                    "[dim](1-indexed position; use /queue to see positions)[/]"
                )
                return True
            try:
                pos = int(parts[2])
            except ValueError:
                self._chat_msg(
                    f"[red]Invalid position '{parts[2]}' — must be a number[/]"
                )
                return True
            n = len(self._input_queue)
            if n == 0:
                self._chat_msg("[dim]queue empty — nothing to drop[/]")
                return True
            if pos < 1 or pos > n:
                self._chat_msg(
                    f"[red]Position {pos} out of range — queue has "
                    f"{n} item{'s' if n != 1 else ''} (1..{n})[/]"
                )
                return True
            # Convert 1-indexed user position to 0-indexed list slot
            target_row = self._queue_widgets[pos - 1]
            dropped_text = self._input_queue[pos - 1]
            preview = dropped_text[:60].replace("\n", " ")
            self._drop_queue_item(target_row)
            self._chat_msg(
                f"[dim]dropped #{pos}: {preview}[/]"
            )
            return True

        self._chat_msg(
            "[red]Usage:[/] [dim]/queue  |  /queue clear  |  /queue drop N[/]"
        )
        return True

    async def _run_codex_login(self) -> None:
        """Run the Codex OAuth login flow off the UI thread."""
        from agent.codex_auth import login as codex_login
        try:
            creds = await asyncio.to_thread(codex_login)
            self._chat_msg(
                f"[bold green]Logged in to ChatGPT[/] "
                f"[dim](account_id={creds.account_id[:8]}…, expires in "
                f"{(creds.expires_at - int(__import__('time').time())) // 3600}h)[/]"
            )
            self._chat_msg("[dim]Codex endpoint is now usable. "
                           "Restart agents that should pick it up.[/]")
        except Exception as e:
            self._chat_msg(f"[red]Codex login failed: {e}[/]")

    async def _handle_model_command(self, parts: list[str]) -> bool:
        """Inspect or change the model an agent uses (runtime override).

        At runtime we can't rebuild a sub-agent's executor without
        respawning it, so /model with an override calls
        ``mgr.stop()`` then ``mgr.spawn()`` for the target agent
        after temporarily mutating its in-memory config.
        """
        from config import AGENTS, list_endpoint_models, ENDPOINTS

        if len(parts) == 1:
            # List every agent's currently-configured endpoint+model
            lines = ["[dim]Current models per agent:[/]"]
            for name, cfg in AGENTS.items():
                ep = cfg.get("endpoint", "(default)")
                model = cfg.get("model", "(default)")
                lines.append(f"  [bold]{name}[/]: {ep}/{model}")
            for line in lines:
                self._chat_msg(line)
            return True

        agent_name = parts[1]
        if agent_name not in AGENTS:
            self._chat_msg(f"[red]Unknown agent '{agent_name}'[/]")
            return True

        if len(parts) == 2:
            cfg = AGENTS[agent_name]
            self._chat_msg(
                f"[dim]{agent_name}: endpoint={cfg.get('endpoint')} "
                f"model={cfg.get('model', '(default)')}[/]"
            )
            available = []
            for ep_name in ENDPOINTS:
                for m in list_endpoint_models(ep_name):
                    available.append(f"{ep_name}/{m}")
            self._chat_msg(f"[dim]Available: {', '.join(available)}[/]")
            return True

        # /model AGENT EP/MODEL — apply override
        spec = parts[2]
        if "/" not in spec:
            self._chat_msg("[red]Spec must be ENDPOINT/MODEL (e.g. codex-cli/gpt-5.4)[/]")
            return True
        ep_name, model_name = spec.split("/", 1)
        if ep_name not in ENDPOINTS:
            self._chat_msg(f"[red]Unknown endpoint '{ep_name}'[/]")
            return True
        if model_name not in list_endpoint_models(ep_name):
            self._chat_msg(
                f"[red]Model '{model_name}' not on endpoint '{ep_name}'. "
                f"Available: {list_endpoint_models(ep_name)}[/]"
            )
            return True

        # Mutate the in-memory config first so any subsequent
        # rebuild reads the new endpoint/model.
        AGENTS[agent_name]["endpoint"] = ep_name
        AGENTS[agent_name]["model"] = model_name
        self._chat_msg(f"[dim]{agent_name} → {spec}[/]")

        # Two rebuild paths depending on whether the target is the
        # main agent or a sub-agent. Both end up with the new LLM
        # active without losing chat history.
        if agent_name == self.agent_runner.agent_name:
            # Main agent (kai) — call AgentRunner.reload_llm() which
            # rebuilds the primary executor + fallback chain in
            # place. Tools, memory, skills, prompt, and chat_history
            # are all preserved.
            try:
                summary = self.agent_runner.reload_llm()
            except Exception as exc:
                self._chat_msg(f"[red]reload_llm failed: {exc}[/]")
                self.logger.warning("reload_llm failed for %s: %s", agent_name, exc)
                return True
            self._chat_msg(
                f"[bold green]{agent_name} now on {summary['provider']}/{summary['model']}[/] "
                f"[dim](+{summary['fallback_count']} fallback{'s' if summary['fallback_count'] != 1 else ''})[/]"
            )
            return True

        mgr = getattr(self, "_sub_agent_manager", None)
        if mgr and agent_name in mgr.agents:
            self._chat_msg(f"[dim]Respawning {agent_name} with new model...[/]")
            await mgr.stop(agent_name)
            await mgr.spawn(agent_name)
            self._chat_msg(f"[bold green]{agent_name} restarted on {spec}[/]")
        else:
            # Sub-agent isn't running yet — the mutation will take
            # effect next time it spawns.
            self._chat_msg(
                f"[dim]{agent_name} not running — new model will take effect on next spawn[/]"
            )
        return True

    async def _handle_think_command(self, parts: list[str]) -> bool:
        """Inspect or change an agent's reasoning effort (thinking level).

        Reasoning effort is the ``reasoning.effort`` parameter on
        the OpenAI Responses API for gpt-5.x / o-series models. It
        controls how much hidden chain-of-thought the model burns
        before answering — higher = better answers on hard problems
        but slower and more expensive (the hidden tokens are billed).

        Forms:
            /think                — show kai's current thinking level
            /think LEVEL          — set kai's thinking level
            /think AGENT          — show AGENT's current level
            /think AGENT LEVEL    — set AGENT's thinking level

        Valid levels (case-insensitive, with aliases):
            none       (off)
            minimal    (min)
            low
            medium     (default)
            high
            xhigh      (x-high, extreme, max, extra)

        Per-agent overrides are runtime-only — mirroring /model,
        we mutate the in-memory AGENTS dict and rebuild the executor
        in place via ``reload_llm()`` (main agent) or stop+spawn
        (sub-agent). Restart the TUI to revert to the on-disk
        defaults from agent-config.json.

        Endpoint compatibility: not every endpoint honors the
        reasoning_effort parameter. Codex CLI / Responses API
        (codex-cli/gpt-5.x) DOES. The cloud kai-smart endpoint
        passes through to vLLM running qwen3, which does not.
        We set the override regardless and warn the user when the
        currently-active endpoint won't act on it — the override
        becomes load-bearing as soon as they /model swap to a
        thinking-capable endpoint.
        """
        from config import (
            AGENTS,
            VALID_REASONING_EFFORTS,
            normalize_reasoning_effort,
            set_agent_reasoning_effort,
        )

        # Endpoints whose model family honors reasoning_effort. Used
        # to warn the user when /think is set on an agent whose
        # current endpoint will silently ignore the new level.
        thinking_endpoints = {"codex-cli", "openai-direct"}

        # Form 1: no args — show kai's current effort + the menu of valid levels
        if len(parts) == 1:
            kai_cfg = AGENTS.get(self.agent_runner.agent_name, {})
            current = kai_cfg.get("reasoning_effort") or "(default: medium)"
            self._chat_msg(
                f"[dim]{self.agent_runner.agent_name} thinking level: "
                f"[bold]{current}[/][/]"
            )
            self._chat_msg(
                f"[dim]Valid: {', '.join(VALID_REASONING_EFFORTS)} "
                f"(aliases: x-high, extreme, max, min, off)[/]"
            )
            self._chat_msg(
                "[dim]Usage: /think LEVEL  |  /think AGENT LEVEL[/]"
            )
            return True

        arg1 = parts[1]

        # Form 2: /think LEVEL — set kai's level (the most common case)
        if len(parts) == 2 and normalize_reasoning_effort(arg1) is not None:
            return await self._apply_thinking_level(
                self.agent_runner.agent_name, arg1, thinking_endpoints
            )

        # Form 3: /think AGENT — inspect a specific agent
        if len(parts) == 2:
            agent_name = arg1
            if agent_name not in AGENTS:
                self._chat_msg(
                    f"[red]Unknown agent or invalid level '{arg1}'[/]  "
                    f"[dim]Valid levels: {', '.join(VALID_REASONING_EFFORTS)}[/]"
                )
                return True
            cfg = AGENTS[agent_name]
            current = cfg.get("reasoning_effort") or "(default: medium)"
            ep = cfg.get("endpoint", "(unset)")
            ep_name = ep if isinstance(ep, str) else (ep.get("endpoint") or "(unset)")
            warn = ""
            if ep_name not in thinking_endpoints:
                warn = (
                    f" [yellow](note: endpoint '{ep_name}' may not honor "
                    "reasoning_effort)[/]"
                )
            self._chat_msg(
                f"[dim]{agent_name} thinking level: [bold]{current}[/]"
                f" on endpoint {ep_name}{warn}[/]"
            )
            return True

        # Form 4: /think AGENT LEVEL — set a specific agent's level
        agent_name = arg1
        level = parts[2]
        if agent_name not in AGENTS:
            self._chat_msg(f"[red]Unknown agent '{agent_name}'[/]")
            return True
        return await self._apply_thinking_level(agent_name, level, thinking_endpoints)

    async def _apply_thinking_level(
        self,
        agent_name: str,
        level: str,
        thinking_endpoints: set[str],
    ) -> bool:
        """Validate, apply, and rebuild for a thinking-level change.

        Shared between the /think LEVEL form (kai) and the
        /think AGENT LEVEL form (any agent). Mirrors the rebuild
        logic in _handle_model_command — main agent uses
        ``reload_llm()``, sub-agents use stop+spawn.
        """
        from config import AGENTS, set_agent_reasoning_effort

        try:
            canonical = set_agent_reasoning_effort(agent_name, level)
        except ValueError as exc:
            self._chat_msg(f"[red]{exc}[/]")
            return True

        # Warn when the currently-active endpoint doesn't honor the
        # parameter — the override is set in memory but the user
        # won't see any behavior change until they /model swap to a
        # thinking-capable endpoint.
        cfg = AGENTS[agent_name]
        ep = cfg.get("endpoint")
        ep_name = ep if isinstance(ep, str) else (
            ep.get("endpoint") if isinstance(ep, dict) else None
        )
        suppressed = ep_name not in thinking_endpoints
        if suppressed:
            self._chat_msg(
                f"[yellow]warning: {agent_name} is on '{ep_name}' which may not "
                f"honor reasoning_effort. Override stored — will take effect "
                f"after /model swap to a thinking-capable endpoint.[/]"
            )

        self._chat_msg(f"[dim]{agent_name} thinking → [bold]{canonical}[/][/]")

        # Rebuild the executor so the new effort hits the next request.
        if agent_name == self.agent_runner.agent_name:
            try:
                summary = self.agent_runner.reload_llm()
            except Exception as exc:
                self._chat_msg(f"[red]reload_llm failed: {exc}[/]")
                self.logger.warning(
                    "reload_llm failed for %s after /think: %s", agent_name, exc
                )
                return True
            self._chat_msg(
                f"[bold green]{agent_name} now thinking at {canonical}[/] "
                f"[dim](on {summary['provider']}/{summary['model']})[/]"
            )
            return True

        mgr = getattr(self, "_sub_agent_manager", None)
        if mgr and agent_name in mgr.agents:
            self._chat_msg(f"[dim]Respawning {agent_name} with thinking={canonical}...[/]")
            await mgr.stop(agent_name)
            await mgr.spawn(agent_name)
            self._chat_msg(
                f"[bold green]{agent_name} restarted thinking at {canonical}[/]"
            )
        else:
            self._chat_msg(
                f"[dim]{agent_name} not running — thinking={canonical} will "
                "take effect on next spawn[/]"
            )
        return True

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

                    # Wall-clock cap for sub-agent dispatches from the
                    # TUI. 8 hours (28800s) matches
                    # NATS_REQUEST_DEFAULT_TIMEOUT in agent/tools.py —
                    # both paths (TUI direct + LLM via nats_request
                    # tool) use the same generous default so
                    # substantive analyst / trader / risk-manager
                    # work doesn't time out before the sub-agent
                    # finishes. Routine queries return in seconds;
                    # the cap is for deep multi-step reasoning chains
                    # that legitimately take hours.
                    reply = await self.bus.request(
                        f"agent.{agent_name}.request",
                        {"task": task, "from": self.bus.agent_name},
                        timeout=28800,
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
            self._drain_input_queue()

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

            # Same 8-hour wall-clock cap as the regular sub-agent
            # dispatch path. Mentor reflections on dense sessions
            # (long tool-call lists, large response bodies, multiple
            # candidate skills) can take many minutes — the previous
            # 180s default would cut off the mentor mid-draft and
            # corrupt the structured reply parser.
            reply = await self.bus.request(
                "agent.mentor.request",
                {"task": reflection_prompt, "from": self.bus.agent_name},
                timeout=28800,
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
            self._drain_input_queue()

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
            # Persist the updated chat_history to disk so the next
            # session can resume where we left off, even if the TUI
            # crashes before on_unmount runs. The save is cheap and
            # idempotent — overwrite-and-rename of one small JSON file.
            self._save_chat_history()
            self._drain_input_queue()

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
        """Route a live signal from the SignalConsumer to the AlertsPanel
        AND fan it out to the SignalHandlerRunner for declarative reactions.

        Called synchronously from the consumer's ``_ingest`` method.
        Uses ``call_from_thread`` to be safe if the NATS callback
        fires from a background thread.

        Two responsibilities:
          1. Display — always fires, posts to alerts panel + nats log
          2. React — runs the configured signal_handlers (analyst
             dispatch, risk-manager check, autotrade pipeline, etc).
             Each handler runs through cooldown + autotrade gating
             before firing. The display side never blocks on the
             reaction side.
        """
        # Display side
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

        # Reaction side
        runner = getattr(self, "_signal_handler_runner", None)
        if runner is not None:
            try:
                runner.run(sig)
            except Exception as exc:
                self.logger.warning("signal handler runner failed: %s", exc)

    # ── Signal handler runner construction + dispatchers ──────

    def _build_signal_handler_runner(self) -> None:
        """Construct the SignalHandlerRunner with action dispatchers
        bound to the live bus / chat / agent_runner instances.

        Called from ``on_mount`` after the bus is connected and the
        chat panel is mounted. Idempotent — safe to call again on
        config reload (the new runner replaces the old one).
        """
        from agent.signal_handlers import (
            ACTION_CHAT_MESSAGE,
            ACTION_DISPATCH_AGENT,
            ACTION_DISPATCH_KAI,
            ACTION_PUBLISH,
            ACTION_WEBHOOK,
            SignalHandlerRunner,
            load_handlers_from_config,
            render_template,
        )
        from config import _config as raw_config

        handlers = load_handlers_from_config(raw_config)
        if not handlers:
            self._signal_handler_runner = None
            self.logger.info("no signal_handlers configured — passive mode")
            return

        # ── Dispatchers ──────────────────────────────────────
        # Each dispatcher is an async callable that takes
        # (handler, flat_signal_dict) and performs the side effect.
        # They close over `self` so they can use the bus / chat /
        # agent_runner / sub_agent_manager.

        async def dispatch_agent(handler, flat):
            """Spawn the named sub-agent if needed and nats_request the task."""
            agent_name = handler.agent or ""
            if not agent_name:
                self._chat_msg(
                    f"[red][handler:{handler.name}] dispatch_agent missing 'agent' field[/]"
                )
                return
            mgr = getattr(self, "_sub_agent_manager", None)
            if not mgr or not self.bus:
                self._chat_msg(
                    f"[red][handler:{handler.name}] no sub-agent manager available[/]"
                )
                return
            try:
                if agent_name not in mgr.agents:
                    self._chat_msg(
                        f"[dim][handler:{handler.name}] spawning {agent_name}...[/]"
                    )
                    await mgr.spawn(agent_name)
                task = render_template(handler.task_template, flat)
                if not task:
                    task = (
                        f"A {flat.get('strategy','signal')} {flat.get('signal_type','?')} "
                        f"signal arrived for {flat.get('symbol','?')} at "
                        f"${flat.get('price', 0)}. React appropriately."
                    )
                reply = await self.bus.request(
                    f"agent.{agent_name}.request",
                    {"task": task, "from": self.bus.agent_name},
                    timeout=28800,
                )
                response = reply.get("response", str(reply))
                self._chat_msg(
                    f"[bold cyan][{agent_name} ← handler:{handler.name}][/] {response}"
                )
            except Exception as exc:
                self._chat_msg(
                    f"[red][handler:{handler.name}] dispatch_agent failed: {exc}[/]"
                )

        async def dispatch_kai(handler, flat):
            """Send the rendered task to the main agent as a queued chat turn.

            Uses the existing input queue path so a busy main agent
            queues the dispatch instead of running concurrently with
            whatever it's already doing.
            """
            task = render_template(handler.task_template, flat)
            if not task:
                task = (
                    f"A {flat.get('strategy','signal')} {flat.get('signal_type','?')} "
                    f"signal arrived for {flat.get('symbol','?')} at "
                    f"${flat.get('price', 0)}. Decide what to do — read the live "
                    "signal feed via get_signals if you need more context, then react."
                )
            if self._agent_working:
                self._queue_item(task)
            else:
                await self._dispatch_input(task)

        async def chat_message(handler, flat):
            """Just post a styled message in chat — no LLM call, no cost."""
            text = render_template(handler.template, flat)
            if not text:
                text = (
                    f"[handler:{handler.name}] {flat.get('strategy','?')} "
                    f"{flat.get('signal_type','?')} {flat.get('symbol','?')} "
                    f"@ ${flat.get('price', 0)}"
                )
            self._chat_msg(text)

        async def publish_action(handler, flat):
            """Republish the (rendered) signal to a different NATS topic."""
            if not self.bus:
                return
            subject = render_template(handler.subject, flat) or "signals.handled"
            try:
                await self.bus.publish(subject, dict(flat))
            except Exception as exc:
                self._chat_msg(
                    f"[red][handler:{handler.name}] publish failed: {exc}[/]"
                )

        async def webhook_action(handler, flat):
            """POST the signal payload to an external URL."""
            if not handler.url:
                return
            try:
                import json as _json
                import urllib.request
                data = _json.dumps(flat, default=str).encode("utf-8")
                req = urllib.request.Request(
                    handler.url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                # Run in a worker thread so the dispatcher doesn't
                # block the event loop on a slow webhook target.
                await asyncio.to_thread(
                    lambda: urllib.request.urlopen(req, timeout=10).read()
                )
            except Exception as exc:
                self._chat_msg(
                    f"[red][handler:{handler.name}] webhook failed: {exc}[/]"
                )

        action_dispatchers = {
            ACTION_DISPATCH_AGENT: dispatch_agent,
            ACTION_DISPATCH_KAI: dispatch_kai,
            ACTION_CHAT_MESSAGE: chat_message,
            ACTION_PUBLISH: publish_action,
            ACTION_WEBHOOK: webhook_action,
        }

        # Schedule async dispatchers as Textual workers so the
        # SignalConsumer's _ingest callback (which is sync) can
        # fire-and-forget without awaiting them.
        def schedule(coro):
            self.run_worker(coro, thread=False)

        self._signal_handler_runner = SignalHandlerRunner(
            handlers=handlers,
            action_dispatchers=action_dispatchers,
            autotrade_enabled=lambda: self._autotrade_enabled,
            chat_log=self._chat_msg,
            run_async=schedule,
        )
        self.logger.info(
            "signal_handler_runner built with %d handler(s), autotrade=%s",
            len(handlers),
            "ON" if self._autotrade_enabled else "OFF",
        )

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

    def _log_error(self, msg: str, exc: Exception | None = None) -> None:
        """Log an error to BOTH the on-screen panel and the file log.

        Use this everywhere a chart / signal / feed / network error
        happens. Without the file log half, errors disappear when the
        TUI closes and there's no way to post-mortem the next morning.
        """
        if exc is not None:
            self.logger.error("%s: %s", msg, exc)
        else:
            self.logger.error(msg)
        try:
            self._nats_log(f"[red]{msg}[/]")
        except Exception:
            pass

    # ── Chat history persistence ──────────────────────────────

    def _save_chat_history(self) -> None:
        """Persist the agent's chat_history to disk.

        Format: a JSON array of ``{"role": "human"|"ai", "content": "..."}``
        dicts, one per LangChain message in ``agent_runner.chat_history``.
        Tool-call IDs and other run-scoped metadata are intentionally
        dropped — they reference per-session run_ids that won't be
        valid after a restart anyway. The conversational thread (which
        is what the LLM uses for context on the next turn) is preserved
        verbatim.

        Saved after every successful turn (in ``_process_agent``'s
        finally block) AND on app teardown (``on_unmount``), so a
        crash, kill -9, terminal close, or clean exit all preserve
        the most recent conversation.

        Failures are logged but never raised — chat persistence is a
        convenience, not load-bearing, and a corrupt save file should
        never block the agent from running.
        """
        try:
            history = list(getattr(self.agent_runner, "chat_history", []) or [])
            if not history:
                # Don't write an empty file — just remove any stale one
                # so a fresh session doesn't surface yesterday's empty
                # restore message.
                if self._chat_history_path.exists():
                    self._chat_history_path.unlink()
                return

            entries: list[dict] = []
            for msg in history:
                role = self._role_for_message(msg)
                content = getattr(msg, "content", "")
                if isinstance(content, list):
                    # Some LLMs return content as a list of structured
                    # blocks. Flatten to plain text for the on-disk
                    # format — the LLM only needs the text on reload.
                    parts: list[str] = []
                    for block in content:
                        if isinstance(block, dict):
                            t = block.get("text") or block.get("content") or ""
                            if isinstance(t, str):
                                parts.append(t)
                        elif isinstance(block, str):
                            parts.append(block)
                    content = "".join(parts)
                if not isinstance(content, str):
                    content = str(content)
                entries.append({"role": role, "content": content})

            self._chat_history_path.parent.mkdir(parents=True, exist_ok=True)
            self._chat_history_path.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.warning("save chat history failed: %s", exc)

    def _load_chat_history(self) -> int:
        """Restore chat_history from disk on TUI start. Returns count restored.

        Reconstructs LangChain ``HumanMessage`` / ``AIMessage`` objects
        from the saved JSON and assigns them to
        ``agent_runner.chat_history``. Also re-mounts each message in
        the chat panel so the user sees the previous conversation
        immediately on relaunch.

        No-op (returns 0) if the save file doesn't exist or is empty.
        Logs and returns 0 on read errors — chat restore is a
        convenience.
        """
        from langchain_core.messages import AIMessage, HumanMessage

        path = self._chat_history_path
        if not path.exists():
            return 0
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception as exc:
            self.logger.warning("read chat history failed: %s", exc)
            return 0
        if not raw.strip():
            return 0
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.logger.warning("parse chat history failed: %s", exc)
            return 0
        if not isinstance(entries, list):
            return 0

        restored: list = []
        chat = self.query_one("#chat-panel", ChatPanel)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            role = entry.get("role", "")
            content = entry.get("content", "")
            if not isinstance(content, str) or not content:
                continue
            if role == "human":
                restored.append(HumanMessage(content=content))
                chat.append_message(f"[bold green]> {content}[/]", "user-msg")
            elif role == "ai":
                restored.append(AIMessage(content=content))
                # Drop the rich-markup styling — we don't have the
                # original tool_start / tool_end events to recreate
                # the inline tool log lines. Just show the answer.
                chat.append_message(content, "agent-msg")

        self.agent_runner.chat_history = restored
        return len(restored)

    @staticmethod
    def _role_for_message(msg) -> str:
        """Return ``human`` / ``ai`` / ``system`` for a LangChain message.

        Looks at the message type name. Conservative on unknown types
        — defaults to ``"ai"`` so anything that's not clearly a user
        prompt is treated as agent output (the LLM rarely needs to
        re-see system messages on reload, and the chat panel
        doesn't render them anyway).
        """
        cls = type(msg).__name__.lower()
        if "human" in cls:
            return "human"
        if "system" in cls:
            return "system"
        return "ai"

    async def on_unmount(self) -> None:
        """Save chat history one more time on app teardown.

        Covers the path where Ctrl+C / app crash bypasses the
        per-turn save in ``_process_agent``. Idempotent with the
        per-turn save — same content gets written twice in the
        common case, no harm done.
        """
        try:
            self._save_chat_history()
        except Exception as exc:
            try:
                self.logger.warning("on_unmount save failed: %s", exc)
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

    def _detect_clipboard_backend(self) -> str:
        """Pick the best system clipboard backend available on this host.

        Returns one of ``"wl-copy"``, ``"xclip"``, ``"xsel"``, ``"osc52"``.
        Cached on ``self._clipboard_backend`` so we only run
        ``shutil.which`` once per session.

        Why we do not just rely on Textual's ``copy_to_clipboard``:
        that method emits an OSC 52 escape sequence and trusts the
        host terminal to honor it. VTE-based terminals on Linux
        (gnome-terminal, Tilix, Terminator, Konsole, etc.) disable
        OSC 52 clipboard writes by default for security, so the
        sequence reaches the terminal and gets silently dropped —
        the chat says "Copied" but the system clipboard never
        changes. The CLI tools (wl-copy, xclip, xsel) bypass the
        terminal entirely and talk to the Wayland / X11 selection
        owner directly, which is the only reliable path on Linux.
        OSC 52 is still the right fallback for SSH and for terminals
        that DO honor it (kitty, alacritty, wezterm, iTerm2,
        Windows Terminal).
        """
        if self._clipboard_backend is not None:
            return self._clipboard_backend

        import os
        import shutil

        on_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        on_x11 = bool(os.environ.get("DISPLAY"))

        # Probe order: native session backend first, then the other
        # display server's tools (XWayland is common, so xclip on a
        # Wayland session can still work for X-aware apps), then OSC 52.
        candidates: list[str] = []
        if on_wayland:
            candidates.append("wl-copy")
        if on_x11:
            candidates.extend(["xclip", "xsel"])
        if not on_wayland and shutil.which("wl-copy"):
            candidates.append("wl-copy")

        for tool in candidates:
            if shutil.which(tool):
                self._clipboard_backend = tool
                self.logger.info("clipboard backend detected: %s", tool)
                return tool

        self._clipboard_backend = "osc52"
        self.logger.warning(
            "no system clipboard tool found (wl-copy / xclip / xsel) — "
            "falling back to OSC 52 which is silently dropped by VTE-based "
            "terminals (gnome-terminal, Tilix, etc). Install wl-clipboard "
            "(`sudo apt install wl-clipboard`) for Wayland or xclip "
            "(`sudo apt install xclip`) for X11."
        )
        return "osc52"

    def _set_system_clipboard(self, text: str) -> str:
        """Write text to the system clipboard via the best backend.

        Returns the backend name so callers can surface it in the
        chat confirmation message — useful for debugging "I clicked
        copy but my paste shows old data" since the user immediately
        sees whether we used wl-copy (reliable) or osc52 (terminal
        roulette). Raises on subprocess failure so the caller can
        show an error.
        """
        import subprocess

        backend = self._detect_clipboard_backend()

        if backend == "wl-copy":
            subprocess.run(
                ["wl-copy"],
                input=text.encode("utf-8"),
                check=True,
                timeout=2,
            )
            return "wl-copy"

        if backend == "xclip":
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode("utf-8"),
                check=True,
                timeout=2,
            )
            return "xclip"

        if backend == "xsel":
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=text.encode("utf-8"),
                check=True,
                timeout=2,
            )
            return "xsel"

        # Final fallback: OSC 52 via Textual.
        self.copy_to_clipboard(text)
        return "osc52"

    def action_copy_selection(self):
        """Copy the currently mouse-selected text to the system clipboard.

        Bound to ``Ctrl+Shift+C``. Reads ``Screen.get_selected_text()``
        which Textual maintains automatically as the user click-drags
        within any widget that has ``ALLOW_SELECT = True`` (the default
        for ``Static``, ``RichLog``, etc — i.e. everything in our TUI).

        This is the keyboard shortcut version of the auto-copy-on-mouse
        -up handler below: same call into ``_set_system_clipboard``,
        different trigger. Use the keyboard one when you want explicit
        confirmation in chat or when the auto-copy didn't fire.
        """
        try:
            text = self.screen.get_selected_text()
        except Exception as exc:
            self.logger.warning("get_selected_text failed: %s", exc)
            self._chat_msg(f"[red]Copy failed: {exc}[/]")
            return

        if not text:
            self._chat_msg(
                "[dim]No selection. Click and drag inside any panel first, "
                "then hit Ctrl+Shift+C.[/]"
            )
            return

        try:
            backend = self._set_system_clipboard(text)
        except Exception as exc:
            self.logger.warning("set_system_clipboard (selection) failed: %s", exc)
            self._chat_msg(f"[red]Copy failed: {exc}[/]")
            return

        n = len(text)
        preview = text[:50].replace("\n", " ")
        if len(text) > 50:
            preview += "…"
        self._chat_msg(
            f"[dim]Copied {n} chars from selection via {backend} — {preview}[/]"
        )
        self.logger.info(
            "copied %d chars from mouse selection via Ctrl+Shift+C (%s)",
            n,
            backend,
        )
        self._last_auto_copied = text  # so the next auto-copy handler doesn't echo

    def on_text_selected(self, event) -> None:
        """Auto-copy the current text selection on mouse-up.

        Textual's ``TextSelected`` event bubbles up from the screen
        whenever a text selection is updated. The event itself carries
        no payload — we have to call ``screen.get_selected_text()`` to
        actually grab the highlighted text. We then push it through
        ``_set_system_clipboard`` which prefers wl-copy / xclip / xsel
        over OSC 52 because OSC 52 is silently dropped by VTE-based
        terminals (gnome-terminal, Tilix, etc).

        Guarded against duplicate emissions: if the selection text
        hasn't changed since the last copy, we skip. That's the
        per-tick spam guard for the case where TextSelected fires
        multiple times during a single drag.

        Deliberately does NOT post a chat message — auto-copy should
        be invisible. The user knows they highlighted something; the
        proof is the paste working in the destination app. We do log
        an INFO line so a post-mortem can verify the fire happened
        and which backend handled it.
        """
        try:
            text = self.screen.get_selected_text()
        except Exception as exc:
            self.logger.debug("on_text_selected get_selected_text failed: %s", exc)
            return
        if not text or text == self._last_auto_copied:
            return
        try:
            backend = self._set_system_clipboard(text)
            self._last_auto_copied = text
            self.logger.info(
                "auto-copied %d chars from text selection (%s)",
                len(text),
                backend,
            )
        except Exception as exc:
            self.logger.warning("auto-copy set_system_clipboard failed: %s", exc)

    def action_copy_last_response(self):
        """Copy the most recent agent response to the system clipboard.

        Routes through ``_set_system_clipboard`` which prefers the
        platform's native clipboard CLI (wl-copy on Wayland, xclip
        or xsel on X11) and only falls back to OSC 52 if none of
        those tools are installed. The CLI path is the only reliable
        one on Linux because VTE-based terminals (gnome-terminal,
        Tilix, Terminator, Konsole) silently drop OSC 52 clipboard
        writes by default for security.

        Selection logic: walks the chat panel children newest-first
        and grabs the first widget tagged ``agent-msg`` (the CSS
        class our streaming response widgets carry). If nothing
        agent-tagged exists yet, falls back to the most recent
        non-user, non-error message so a freshly-started session
        can still copy welcome banners or status messages.

        Posts a brief confirmation to the chat panel showing how
        many characters were copied AND which backend handled it,
        so the user can immediately see whether they got a reliable
        wl-copy/xclip path or the unreliable osc52 fallback.
        """
        try:
            chat = self.query_one("#chat-panel", ChatPanel)
        except Exception:
            return

        text = self._extract_last_agent_text(chat)
        if not text:
            self._chat_msg("[dim]Nothing to copy yet[/]")
            return

        try:
            backend = self._set_system_clipboard(text)
        except Exception as exc:
            self.logger.warning("set_system_clipboard failed: %s", exc)
            self._chat_msg(f"[red]Copy failed: {exc}[/]")
            return

        n = len(text)
        preview = text[:40].replace("\n", " ")
        if len(text) > 40:
            preview += "…"
        self._chat_msg(
            f"[dim]Copied {n} chars to clipboard via {backend} — {preview}[/]"
        )
        self.logger.info("copied %d chars to clipboard via %s", n, backend)

    @staticmethod
    def _extract_last_agent_text(chat: "ChatPanel") -> str:
        """Walk the chat panel and return the most recent agent text.

        Pulls the markup string off each Static child via
        ``widget.content`` (NOT ``widget.renderable`` — that attribute
        does not exist on Textual's Static and the previous version
        of this method was silently returning the empty default for
        every widget, which is why Ctrl+Y kept reporting "Nothing to
        copy yet"). Strips Rich markup tags so the clipboard contents
        are pure text the user can paste into anything.

        Walks in reverse so the very last response wins — that's
        what the user almost always wants when they hit Ctrl+Y after
        the agent finishes a turn.
        """
        from rich.text import Text

        # Tags whose content is conversational output worth copying.
        # Skip user-msg (the user already typed it) and tool-msg /
        # error-msg (those are usually status noise the user does
        # not want pasted).
        agent_tags = {"agent-msg"}
        fallback_tags = {"agent-msg", "tool-msg"}  # 2nd pass if no agent-msg yet

        def _plain(widget) -> str:
            # Textual Static stores its markup on `.content`. As a
            # final fallback we render the widget — render() returns
            # a Rich Content object whose str() is the plain text
            # already, no markup stripping needed.
            content = getattr(widget, "content", None)
            if isinstance(content, str):
                return Text.from_markup(content).plain
            if content is not None and hasattr(content, "plain"):
                return content.plain
            try:
                return str(widget.render())
            except Exception:
                return ""

        # First pass: prefer the most recent agent message
        for widget in reversed(list(chat.children)):
            classes = getattr(widget, "classes", set()) or set()
            if agent_tags.intersection(classes):
                t = _plain(widget).strip()
                if t:
                    return t

        # Second pass: fall back to any non-user, non-error message
        for widget in reversed(list(chat.children)):
            classes = getattr(widget, "classes", set()) or set()
            if "user-msg" in classes or "error-msg" in classes:
                continue
            if fallback_tags.intersection(classes) or not classes:
                t = _plain(widget).strip()
                if t:
                    return t

        return ""

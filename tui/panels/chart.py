"""ASCII candlestick chart panel with pluggable color schemes."""

from __future__ import annotations

from dataclasses import dataclass
from textual.widget import Widget
from textual.app import RenderResult
from rich.text import Text


# ── Color schemes ───────────────────────────────────────────
#
# Each scheme defines Rich style strings for every visual element of
# the chart. Adding a new scheme = adding a new instance to SCHEMES.


@dataclass(frozen=True)
class ChartColorScheme:
    """Color mapping for the candlestick chart."""

    name: str
    # Candle body
    bar_up: str     # bullish body
    bar_down: str   # bearish body
    # Wick
    wick: str
    # Doji / neutral bar
    doji: str
    # Header text
    header_up: str
    header_down: str
    header_symbol: str
    header_dim: str
    # Price axis + frame
    axis: str


SCHEMES: dict[str, ChartColorScheme] = {
    # TradingView-style chart colors. The teal-green and soft-red
    # below are the exact RGB triplets TradingView uses for its
    # default candle palette and what most professional charts ship
    # with — readable on dark terminals, not eye-stabbing, doesn't
    # look like an ANSI demo from 1992. This is the default scheme.
    "classic": ChartColorScheme(
        name="classic",
        bar_up="rgb(38,166,154)",
        bar_down="rgb(239,83,80)",
        wick="grey62",
        doji="grey78",
        header_up="bold rgb(38,166,154)",
        header_down="bold rgb(239,83,80)",
        header_symbol="bold white",
        header_dim="grey58",
        axis="grey46",
    ),
    # Loud bright-ANSI green/red. Kept for users who want the
    # high-contrast vintage terminal look.
    "neon": ChartColorScheme(
        name="neon",
        bar_up="bold bright_green",
        bar_down="bold bright_red",
        wick="grey70",
        doji="yellow",
        header_up="bold bright_green",
        header_down="bold bright_red",
        header_symbol="bold white",
        header_dim="grey62",
        axis="grey50",
    ),
    # Plain ANSI 8-color green/red. Works on the cheapest terminal,
    # falls back gracefully when the user has SSH'd in over a dumb
    # tty that doesn't speak truecolor.
    "ansi": ChartColorScheme(
        name="ansi",
        bar_up="green",
        bar_down="red",
        wick="dim",
        doji="dim yellow",
        header_up="bold green",
        header_down="bold red",
        header_symbol="bold",
        header_dim="dim",
        axis="dim",
    ),
    "mono": ChartColorScheme(
        name="mono",
        bar_up="bold white",
        bar_down="dim white",
        wick="grey50",
        doji="grey62",
        header_up="bold white",
        header_down="dim white",
        header_symbol="bold",
        header_dim="dim",
        axis="grey42",
    ),
    "ocean": ChartColorScheme(
        name="ocean",
        bar_up="bold cyan",
        bar_down="bold magenta",
        wick="blue",
        doji="bright_blue",
        header_up="bold cyan",
        header_down="bold magenta",
        header_symbol="bold bright_cyan",
        header_dim="dark_cyan",
        axis="dark_blue",
    ),
    "ember": ChartColorScheme(
        name="ember",
        bar_up="bold bright_yellow",
        bar_down="bold bright_red",
        wick="dark_orange",
        doji="yellow",
        header_up="bold bright_yellow",
        header_down="bold bright_red",
        header_symbol="bold bright_yellow",
        header_dim="dark_orange",
        axis="rgb(80,40,0)",
    ),
}

# `classic` is the default — it's the TradingView-style red/green
# every trader is already calibrated to read.
DEFAULT_SCHEME = "classic"


class ChartPanel(Widget):
    """Renders ASCII candlestick charts using Unicode block characters."""

    DEFAULT_CSS = """
    ChartPanel {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.symbol = "BTC"
        self.interval = "1m"
        self.bars: list[dict] = []
        self._scheme: ChartColorScheme = SCHEMES[DEFAULT_SCHEME]
        self._visible = True

    # ── Color scheme management ─────────────────────────────

    @property
    def color_scheme(self) -> str:
        return self._scheme.name

    def set_color_scheme(self, name: str) -> bool:
        """Switch to a named color scheme. Returns True if found."""
        scheme = SCHEMES.get(name.lower())
        if scheme is None:
            return False
        self._scheme = scheme
        self.refresh()
        return True

    @staticmethod
    def available_schemes() -> list[str]:
        return list(SCHEMES.keys())

    # ── Visibility toggle ───────────────────────────────────

    def toggle_visible(self, visible: bool | None = None) -> bool:
        """Toggle or explicitly set chart visibility. Returns new state."""
        if visible is None:
            self._visible = not self._visible
        else:
            self._visible = visible
        self.display = self._visible
        return self._visible

    def set_data(self, symbol: str, interval: str, bars: list[dict]):
        """Set chart data and refresh."""
        self.symbol = symbol
        self.interval = interval
        self.bars = bars
        self.refresh()

    def append_bar(self, bar: dict):
        """Append a new bar and refresh."""
        self.bars.append(bar)
        # Keep last 200 bars max
        if len(self.bars) > 200:
            self.bars = self.bars[-200:]
        self.refresh()

    def update_last_bar(self, bar: dict):
        """Update the most recent bar (live candle)."""
        if self.bars and self.bars[-1].get("ts") == bar.get("ts"):
            self.bars[-1] = bar
        else:
            self.append_bar(bar)
        self.refresh()

    def render(self) -> RenderResult:
        if not self._visible:
            return Text("  Chart hidden — /chart on to restore", style="dim")

        if not self.bars:
            return Text(f"  {self.symbol}/{self.interval} — No data", style=self._scheme.header_dim)

        s = self._scheme  # shorthand
        width = self.size.width - 2
        height = self.size.height - 3  # Reserve for header + price axis
        if width < 10 or height < 5:
            return Text("Panel too small", style=s.header_dim)

        # Use as many bars as fit
        visible_bars = self.bars[-width:]
        if not visible_bars:
            return Text("No bars", style=s.header_dim)

        # Calculate price range
        all_highs = [b["high"] for b in visible_bars]
        all_lows = [b["low"] for b in visible_bars]
        price_max = max(all_highs)
        price_min = min(all_lows)
        price_range = price_max - price_min
        if price_range == 0:
            price_range = 1

        last = visible_bars[-1]
        change = last["close"] - visible_bars[0]["open"]
        change_pct = (change / visible_bars[0]["open"]) * 100 if visible_bars[0]["open"] else 0
        is_up = change >= 0
        arrow = "▲" if is_up else "▼"
        hdr_color = s.header_up if is_up else s.header_down

        # Header
        text = Text()
        text.append(f"  {self.symbol} ", style=s.header_symbol)
        text.append(f"{self.interval} ", style=s.header_dim)
        text.append(f"{arrow} ${last['close']:,.2f} ", style=hdr_color)
        text.append(f"({change_pct:+.2f}%) ", style=hdr_color)
        text.append(f"H:{price_max:,.2f} L:{price_min:,.2f} ", style=s.header_dim)
        text.append(f"[{s.name}]", style=s.header_dim)
        text.append("\n")

        # Build chart grid
        grid = [[" " for _ in range(len(visible_bars))] for _ in range(height)]

        def price_to_row(price):
            row = int((price_max - price) / price_range * (height - 1))
            return max(0, min(height - 1, row))

        for col, bar in enumerate(visible_bars):
            o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

            wick_top = price_to_row(h)
            wick_bot = price_to_row(l)
            body_top = price_to_row(max(o, c))
            body_bot = price_to_row(min(o, c))

            # Draw wick
            for row in range(wick_top, wick_bot + 1):
                grid[row][col] = "│"

            # Draw body (overwrites wick)
            if body_top == body_bot:
                grid[body_top][col] = "─"
            else:
                bar_up = c >= o
                for row in range(body_top, body_bot + 1):
                    grid[row][col] = "█" if bar_up else "▓"

        # Render grid with scheme colors
        for row_idx, row in enumerate(grid):
            price_at_row = price_max - (row_idx / max(height - 1, 1)) * price_range
            if row_idx == 0 or row_idx == height - 1 or row_idx == height // 2:
                text.append(f"{price_at_row:>9,.1f}│", style=s.axis)
            else:
                text.append(f"{'':>9}│", style=s.axis)

            for col_idx, char in enumerate(row):
                bar = visible_bars[col_idx] if col_idx < len(visible_bars) else None
                if bar and char in ("█", "▓"):
                    style = s.bar_up if bar["close"] >= bar["open"] else s.bar_down
                elif bar and char == "─":
                    style = s.doji
                elif char == "│":
                    style = s.wick
                else:
                    style = ""
                text.append(char, style=style)
            text.append("\n")

        # Time axis
        text.append(f"{'':>9}└{'─' * len(visible_bars)}", style=s.axis)

        return text

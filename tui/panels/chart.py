"""ASCII candlestick chart panel."""

from textual.widget import Widget
from textual.app import RenderResult
from rich.text import Text


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
        if not self.bars:
            return Text(f"  {self.symbol}/{self.interval} — No data", style="dim")

        width = self.size.width - 2
        height = self.size.height - 3  # Reserve for header + price axis
        if width < 10 or height < 5:
            return Text("Panel too small", style="dim")

        # Use as many bars as fit
        visible_bars = self.bars[-width:]
        if not visible_bars:
            return Text("No bars", style="dim")

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
        arrow = "▲" if change >= 0 else "▼"
        color = "green" if change >= 0 else "red"

        # Header
        text = Text()
        text.append(f"  {self.symbol} ", style="bold")
        text.append(f"{self.interval} ", style="dim")
        text.append(f"{arrow} ${last['close']:,.2f} ", style=f"bold {color}")
        text.append(f"({change_pct:+.2f}%) ", style=color)
        text.append(f"H:{price_max:,.2f} L:{price_min:,.2f}", style="dim")
        text.append("\n")

        # Build chart grid
        grid = [[" " for _ in range(len(visible_bars))] for _ in range(height)]

        def price_to_row(price):
            row = int((price_max - price) / price_range * (height - 1))
            return max(0, min(height - 1, row))

        for col, bar in enumerate(visible_bars):
            o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
            is_green = c >= o

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
                for row in range(body_top, body_bot + 1):
                    grid[row][col] = "█" if is_green else "▓"

        # Render grid with colors
        for row_idx, row in enumerate(grid):
            # Price label on left
            price_at_row = price_max - (row_idx / max(height - 1, 1)) * price_range
            if row_idx == 0 or row_idx == height - 1 or row_idx == height // 2:
                text.append(f"{price_at_row:>9,.1f}│", style="dim")
            else:
                text.append(f"{'':>9}│", style="dim")

            for col_idx, char in enumerate(row):
                bar = visible_bars[col_idx] if col_idx < len(visible_bars) else None
                if bar and char in ("█", "▓", "─"):
                    style = "green" if bar["close"] >= bar["open"] else "red"
                elif char == "│":
                    style = "dim"
                else:
                    style = ""
                text.append(char, style=style)
            text.append("\n")

        # Time axis
        text.append(f"{'':>9}└{'─' * len(visible_bars)}", style="dim")

        return text

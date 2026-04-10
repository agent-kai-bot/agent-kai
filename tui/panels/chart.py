"""Viewport-aware terminal candlestick chart panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rich.text import Text
from textual.app import RenderResult
from textual.binding import Binding
from textual.events import Click
from textual.widget import Widget


@dataclass(frozen=True)
class ChartColorScheme:
    """Color mapping for the candlestick chart."""

    name: str
    bar_up: str
    bar_down: str
    wick: str
    doji: str
    header_up: str
    header_down: str
    header_symbol: str
    header_dim: str
    axis: str
    overlay: str
    volume_up: str
    volume_down: str


SCHEMES: dict[str, ChartColorScheme] = {
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
        overlay="grey35",
        volume_up="rgb(24,117,106)",
        volume_down="rgb(164,61,58)",
    ),
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
        overlay="grey37",
        volume_up="green",
        volume_down="red",
    ),
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
        overlay="dim",
        volume_up="green",
        volume_down="red",
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
        overlay="grey35",
        volume_up="white",
        volume_down="grey50",
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
        overlay="blue",
        volume_up="cyan",
        volume_down="magenta",
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
        overlay="rgb(120,70,20)",
        volume_up="yellow",
        volume_down="red",
    ),
}

DEFAULT_SCHEME = "classic"
DEFAULT_HISTORY_LIMIT = 600


@dataclass(frozen=True)
class ChartZoomLevel:
    """Viewport zoom profile for the chart.

    Attributes:
        name: Stable profile name.
        label: Compact user-facing label shown in the HUD.
        aggregation: Number of raw bars merged into one rendered candle.
        candle_width: Width of the candle body in terminal cells.
        gap_width: Horizontal gap between rendered candles.
    """

    name: str
    label: str
    aggregation: int
    candle_width: int
    gap_width: int


ZOOM_LEVELS: list[ChartZoomLevel] = [
    ChartZoomLevel("macro", "8x", aggregation=8, candle_width=1, gap_width=0),
    ChartZoomLevel("wide", "4x", aggregation=4, candle_width=1, gap_width=0),
    ChartZoomLevel("swing", "2x", aggregation=2, candle_width=1, gap_width=0),
    ChartZoomLevel("detail", "1x", aggregation=1, candle_width=1, gap_width=0),
    ChartZoomLevel("close", "2col", aggregation=1, candle_width=2, gap_width=0),
    ChartZoomLevel("tape", "2+1", aggregation=1, candle_width=2, gap_width=1),
]
DEFAULT_ZOOM_INDEX = 3


@dataclass
class ChartViewportState:
    """Interactive viewport state for the chart panel."""

    zoom_index: int = DEFAULT_ZOOM_INDEX
    right_offset: int = 0
    show_volume: bool = True
    show_price_line: bool = True


@dataclass(frozen=True)
class ChartViewWindow:
    """Visible chart slice after zoom and pan are applied."""

    bars: list[dict[str, Any]]
    capacity: int
    aggregated_count: int
    hidden_left: int
    hidden_right: int
    zoom_level: ChartZoomLevel


def _coerce_timestamp(value: Any) -> datetime:
    """Return a timezone-aware UTC datetime for a bar timestamp."""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    raise TypeError(f"unsupported timestamp type: {type(value)!r}")


def normalize_bar(bar: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw bar dict into the chart's internal shape."""

    return {
        "ts": _coerce_timestamp(bar["ts"]),
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": float(bar["close"]),
        "volume": float(bar.get("volume", 0.0)),
    }


def aggregate_view_bars(
    bars: list[dict[str, Any]], factor: int
) -> list[dict[str, Any]]:
    """Aggregate raw bars into coarser viewport candles.

    Args:
        bars: Oldest-first raw bars.
        factor: Number of consecutive raw bars merged into one view bar.

    Returns:
        Aggregated bars preserving OHLCV semantics.
    """

    if factor <= 1:
        return [normalize_bar(bar) for bar in bars]

    normalized = [normalize_bar(bar) for bar in bars]
    grouped: list[dict[str, Any]] = []
    for start in range(0, len(normalized), factor):
        chunk = normalized[start : start + factor]
        if not chunk:
            continue
        grouped.append(
            {
                "ts": chunk[0]["ts"],
                "open": chunk[0]["open"],
                "high": max(bar["high"] for bar in chunk),
                "low": min(bar["low"] for bar in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(bar.get("volume", 0.0) for bar in chunk),
            }
        )
    return grouped


def build_view_window(
    bars: list[dict[str, Any]],
    plot_width: int,
    viewport: ChartViewportState,
) -> ChartViewWindow:
    """Select the visible chart window for the current viewport.

    Args:
        bars: Raw bars in oldest-first order.
        plot_width: Horizontal plot width in terminal cells.
        viewport: Active zoom and pan state.

    Returns:
        A ``ChartViewWindow`` describing the rendered slice.
    """

    zoom = ZOOM_LEVELS[max(0, min(viewport.zoom_index, len(ZOOM_LEVELS) - 1))]
    unit_width = max(1, zoom.candle_width + zoom.gap_width)
    capacity = max(1, plot_width // unit_width)
    aggregated = aggregate_view_bars(bars, zoom.aggregation)
    if not aggregated:
        return ChartViewWindow(
            bars=[],
            capacity=capacity,
            aggregated_count=0,
            hidden_left=0,
            hidden_right=0,
            zoom_level=zoom,
        )

    max_right_offset = max(0, len(aggregated) - capacity)
    right_offset = max(0, min(viewport.right_offset, max_right_offset))
    end_index = len(aggregated) - right_offset
    start_index = max(0, end_index - capacity)

    return ChartViewWindow(
        bars=aggregated[start_index:end_index],
        capacity=capacity,
        aggregated_count=len(aggregated),
        hidden_left=start_index,
        hidden_right=len(aggregated) - end_index,
        zoom_level=zoom,
    )


def _format_price(value: float) -> str:
    """Render a price value for the left axis."""

    if abs(value) >= 1000:
        return f"{value:>9,.1f}"
    if abs(value) >= 10:
        return f"{value:>9,.2f}"
    return f"{value:>9,.4f}"


def _format_short_time(value: datetime) -> str:
    """Render a compact timestamp label for the footer."""

    return value.strftime("%m-%d %H:%M")


def _place_label(buffer: list[str], start_index: int, label: str) -> None:
    """Copy a label into a char buffer, clipped to fit."""

    if not label or start_index >= len(buffer):
        return
    index = max(0, start_index)
    for char in label:
        if index >= len(buffer):
            break
        buffer[index] = char
        index += 1


def _make_grid(height: int, width: int) -> tuple[list[list[str]], list[list[str]]]:
    """Create a char grid and matching style grid."""

    chars = [[" " for _ in range(width)] for _ in range(height)]
    styles = [["" for _ in range(width)] for _ in range(height)]
    return chars, styles


class ChartPanel(Widget):
    """Render a serious terminal chart with viewport controls and modes."""

    can_focus = True

    BINDINGS = [
        Binding("left", "pan_left", "Pan Left", show=False),
        Binding("right", "pan_right", "Pan Right", show=False),
        Binding("up", "zoom_in", "Zoom In", show=False),
        Binding("down", "zoom_out", "Zoom Out", show=False),
        Binding("home", "pan_to_latest", "Latest", show=False),
        Binding("end", "pan_to_latest", "Latest", show=False),
        Binding("v", "toggle_volume", "Volume", show=False),
    ]

    DEFAULT_CSS = """
    ChartPanel {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.symbol = "BTC"
        self.interval = "1m"
        self.source = "kai-api"
        self.layout_mode = "dashboard"
        self.bars: list[dict[str, Any]] = []
        self._scheme: ChartColorScheme = SCHEMES[DEFAULT_SCHEME]
        self._visible = True
        self._viewport = ChartViewportState()

    @property
    def color_scheme(self) -> str:
        """Return the active color scheme name."""

        return self._scheme.name

    def set_color_scheme(self, name: str) -> bool:
        """Switch to a named color scheme."""

        scheme = SCHEMES.get(name.lower())
        if scheme is None:
            return False
        self._scheme = scheme
        self.refresh()
        return True

    @staticmethod
    def available_schemes() -> list[str]:
        """Return the available chart color schemes."""

        return list(SCHEMES.keys())

    def set_source(self, name: str) -> None:
        """Set the data source label shown in the chart HUD."""

        self.source = name
        self.refresh()

    def set_layout_mode(self, mode: str) -> None:
        """Set the active workspace layout mode label."""

        self.layout_mode = mode
        self.refresh()

    def get_view_state(self, plot_width: int | None = None) -> dict[str, Any]:
        """Return a summary of the current viewport state."""

        width = plot_width or max(10, self.size.width - 14)
        view = build_view_window(self.bars, width, self._viewport)
        return {
            "zoom": view.zoom_level.label,
            "layout_mode": self.layout_mode,
            "right_offset": min(
                self._viewport.right_offset, max(0, view.aggregated_count - view.capacity)
            ),
            "visible_bars": len(view.bars),
            "total_bars": view.aggregated_count,
            "show_volume": self._viewport.show_volume,
            "show_price_line": self._viewport.show_price_line,
        }

    def zoom_in(self) -> dict[str, Any]:
        """Zoom the chart in one step and refresh."""

        self._viewport.zoom_index = min(
            len(ZOOM_LEVELS) - 1, self._viewport.zoom_index + 1
        )
        self.refresh()
        return self.get_view_state()

    def zoom_out(self) -> dict[str, Any]:
        """Zoom the chart out one step and refresh."""

        self._viewport.zoom_index = max(0, self._viewport.zoom_index - 1)
        self.refresh()
        return self.get_view_state()

    def reset_view(self) -> dict[str, Any]:
        """Reset the viewport to the default live-following state."""

        self._viewport = ChartViewportState()
        self.refresh()
        return self.get_view_state()

    def pan_left(self, steps: int = 6) -> dict[str, Any]:
        """Pan left into older history."""

        self._viewport.right_offset += max(1, int(steps))
        self.refresh()
        return self.get_view_state()

    def pan_right(self, steps: int = 6) -> dict[str, Any]:
        """Pan right toward the latest candle."""

        self._viewport.right_offset = max(
            0, self._viewport.right_offset - max(1, int(steps))
        )
        self.refresh()
        return self.get_view_state()

    def pan_to_latest(self) -> dict[str, Any]:
        """Jump back to the live edge."""

        self._viewport.right_offset = 0
        self.refresh()
        return self.get_view_state()

    def toggle_volume(self, visible: bool | None = None) -> dict[str, Any]:
        """Toggle or explicitly set the volume pane visibility."""

        if visible is None:
            self._viewport.show_volume = not self._viewport.show_volume
        else:
            self._viewport.show_volume = bool(visible)
        self.refresh()
        return self.get_view_state()

    def action_pan_left(self) -> None:
        """Pan the viewport toward older history."""

        self.pan_left()

    def action_pan_right(self) -> None:
        """Pan the viewport toward the latest candles."""

        self.pan_right()

    def action_zoom_in(self) -> None:
        """Zoom the chart in."""

        self.zoom_in()

    def action_zoom_out(self) -> None:
        """Zoom the chart out."""

        self.zoom_out()

    def action_pan_to_latest(self) -> None:
        """Jump back to the live edge."""

        self.pan_to_latest()

    def action_toggle_volume(self) -> None:
        """Toggle the volume pane."""

        self.toggle_volume()

    def on_click(self, _event: Click) -> None:
        """Focus the chart when it is clicked."""

        self.focus()

    def toggle_visible(self, visible: bool | None = None) -> bool:
        """Toggle or explicitly set chart visibility."""

        if visible is None:
            self._visible = not self._visible
        else:
            self._visible = visible
        self.visible = self._visible
        return self._visible

    def set_data(self, symbol: str, interval: str, bars: list[dict[str, Any]]) -> None:
        """Replace the chart history and redraw."""

        self.symbol = symbol
        self.interval = interval
        self.bars = [normalize_bar(bar) for bar in bars][-DEFAULT_HISTORY_LIMIT:]
        self.refresh()

    def append_bar(self, bar: dict[str, Any]) -> None:
        """Append a new bar to the chart history."""

        self.bars.append(normalize_bar(bar))
        if len(self.bars) > DEFAULT_HISTORY_LIMIT:
            self.bars = self.bars[-DEFAULT_HISTORY_LIMIT:]
        self.refresh()

    def update_last_bar(self, bar: dict[str, Any]) -> None:
        """Update the latest candle or append a new one."""

        normalized = normalize_bar(bar)
        if self.bars and self.bars[-1]["ts"] == normalized["ts"]:
            self.bars[-1] = normalized
        else:
            self.append_bar(normalized)
            return
        self.refresh()

    def render(self) -> RenderResult:
        """Render the chart as Rich text."""

        if not self._visible:
            return Text("  Chart hidden - /chart on to restore", style="dim")

        if not self.bars:
            return Text(
                f"  {self.symbol}/{self.interval} - No data",
                style=self._scheme.header_dim,
            )

        inner_width = max(1, self.size.width - 2)
        if inner_width < 28 or self.size.height < 10:
            return Text("Panel too small", style=self._scheme.header_dim)

        axis_width = 10
        plot_width = inner_width - axis_width - 1
        if plot_width < 10:
            return Text("Panel too small", style=self._scheme.header_dim)

        view = build_view_window(self.bars, plot_width, self._viewport)
        if not view.bars:
            return Text("No bars", style=self._scheme.header_dim)

        return self._render_view(view, axis_width=axis_width, plot_width=plot_width)

    def _render_view(
        self, view: ChartViewWindow, axis_width: int, plot_width: int
    ) -> Text:
        """Render a prepared viewport window."""

        scheme = self._scheme
        bars = view.bars
        last = bars[-1]
        first = bars[0]
        change = last["close"] - first["open"]
        change_pct = (change / first["open"]) * 100 if first["open"] else 0.0
        is_up = change >= 0
        arrow = "▲" if is_up else "▼"
        header_style = scheme.header_up if is_up else scheme.header_down

        total_header_lines = 3
        total_footer_lines = 2
        available_height = self.size.height - total_header_lines - total_footer_lines
        if available_height < 5:
            return Text("Panel too small", style=scheme.header_dim)

        volume_height = 0
        if self._viewport.show_volume and available_height >= 12:
            volume_height = max(3, min(6, available_height // 4))
        price_height = available_height - volume_height - (1 if volume_height else 0)

        highs = [bar["high"] for bar in bars]
        lows = [bar["low"] for bar in bars]
        price_max = max(highs)
        price_min = min(lows)
        raw_range = max(1e-9, price_max - price_min)
        padding = max(raw_range * 0.06, last["close"] * 0.0015)
        scale_max = price_max + padding
        scale_min = price_min - padding
        scale_range = max(1e-9, scale_max - scale_min)

        unit_width = view.zoom_level.candle_width + view.zoom_level.gap_width
        price_chars, price_styles = _make_grid(price_height, plot_width)

        def price_to_row(value: float) -> int:
            row = int((scale_max - value) / scale_range * max(price_height - 1, 1))
            return max(0, min(price_height - 1, row))

        if self._viewport.show_price_line:
            price_line_row = price_to_row(last["close"])
            for column in range(plot_width):
                price_chars[price_line_row][column] = "┈"
                price_styles[price_line_row][column] = scheme.overlay

        for index, bar in enumerate(bars):
            x0 = index * unit_width
            x_mid = x0 + (view.zoom_level.candle_width - 1) // 2
            if x0 >= plot_width:
                break
            wick_top = price_to_row(bar["high"])
            wick_bottom = price_to_row(bar["low"])
            body_top = price_to_row(max(bar["open"], bar["close"]))
            body_bottom = price_to_row(min(bar["open"], bar["close"]))

            for row in range(wick_top, wick_bottom + 1):
                price_chars[row][x_mid] = "│"
                price_styles[row][x_mid] = scheme.wick

            body_style = (
                scheme.bar_up if bar["close"] >= bar["open"] else scheme.bar_down
            )
            if body_top == body_bottom:
                for column in range(
                    x0, min(plot_width, x0 + view.zoom_level.candle_width)
                ):
                    price_chars[body_top][column] = "─"
                    price_styles[body_top][column] = scheme.doji
            else:
                fill = "█" if bar["close"] >= bar["open"] else "▓"
                for row in range(body_top, body_bottom + 1):
                    for column in range(
                        x0, min(plot_width, x0 + view.zoom_level.candle_width)
                    ):
                        price_chars[row][column] = fill
                        price_styles[row][column] = body_style

        volume_chars: list[list[str]] = []
        volume_styles: list[list[str]] = []
        max_volume = max((bar["volume"] for bar in bars), default=0.0)
        if volume_height:
            volume_chars, volume_styles = _make_grid(volume_height, plot_width)
            if max_volume <= 0:
                max_volume = 1.0
            for index, bar in enumerate(bars):
                x0 = index * unit_width
                filled_rows = int((bar["volume"] / max_volume) * volume_height)
                filled_rows = max(1 if bar["volume"] > 0 else 0, filled_rows)
                style = (
                    scheme.volume_up
                    if bar["close"] >= bar["open"]
                    else scheme.volume_down
                )
                for row in range(volume_height - 1, volume_height - 1 - filled_rows, -1):
                    if row < 0:
                        break
                    for column in range(
                        x0, min(plot_width, x0 + view.zoom_level.candle_width)
                    ):
                        volume_chars[row][column] = "▄"
                        volume_styles[row][column] = style

        text = Text()
        text.append(f"  {self.symbol} ", style=scheme.header_symbol)
        text.append(f"{self.interval} ", style=scheme.header_dim)
        text.append(f"{arrow} ${last['close']:,.2f} ", style=header_style)
        text.append(f"({change_pct:+.2f}%) ", style=header_style)
        text.append(f"O:{last['open']:,.2f} ", style=scheme.header_dim)
        text.append(f"H:{last['high']:,.2f} ", style=scheme.header_dim)
        text.append(f"L:{last['low']:,.2f} ", style=scheme.header_dim)
        text.append(f"C:{last['close']:,.2f}", style=scheme.header_dim)
        text.append("\n")

        text.append("  ", style=scheme.header_dim)
        text.append(
            f"mode {self.layout_mode}  ",
            style=scheme.header_dim,
        )
        text.append(
            f"zoom {view.zoom_level.label}  ",
            style=scheme.header_dim,
        )
        text.append(
            f"view {len(bars)}/{view.aggregated_count} bars  ",
            style=scheme.header_dim,
        )
        if view.hidden_right:
            text.append(
                f"offset {view.hidden_right}  ",
                style=scheme.header_down,
            )
        else:
            text.append("live  ", style=scheme.header_up)
        text.append(
            f"volume {'on' if volume_height else 'off'}  ",
            style=scheme.header_dim,
        )
        text.append(f"source {self.source}", style=scheme.header_dim)
        text.append("\n")

        text.append("  ", style=scheme.header_dim)
        text.append(
            f"range {_format_short_time(first['ts'])} -> {_format_short_time(last['ts'])}",
            style=scheme.header_dim,
        )
        text.append(f"  vol {last['volume']:,.1f}", style=scheme.header_dim)
        text.append("\n")

        label_rows = {0, price_height // 3, (price_height * 2) // 3, price_height - 1}
        for row_index in range(price_height):
            price_at_row = scale_max - (
                row_index / max(price_height - 1, 1)
            ) * scale_range
            if row_index in label_rows:
                text.append(_format_price(price_at_row), style=scheme.axis)
            else:
                text.append(" " * axis_width, style=scheme.axis)
            text.append("│", style=scheme.axis)
            for column in range(plot_width):
                text.append(price_chars[row_index][column], style=price_styles[row_index][column])
            text.append("\n")

        if volume_height:
            text.append(" " * axis_width, style=scheme.axis)
            text.append("┝", style=scheme.axis)
            text.append("━" * plot_width, style=scheme.axis)
            text.append("\n")

            for row_index in range(volume_height):
                if row_index == 0:
                    label = f"{max_volume:>9,.0f}"
                elif row_index == volume_height - 1:
                    label = f"{'vol':>9}"
                else:
                    label = " " * axis_width
                text.append(label, style=scheme.axis)
                text.append("│", style=scheme.axis)
                for column in range(plot_width):
                    text.append(
                        volume_chars[row_index][column],
                        style=volume_styles[row_index][column],
                    )
                text.append("\n")

        footer = [" " for _ in range(plot_width)]
        if bars:
            positions = [
                (0, _format_short_time(bars[0]["ts"])),
                (max(0, plot_width // 2 - 5), _format_short_time(bars[len(bars) // 2]["ts"])),
                (max(0, plot_width - 11), _format_short_time(bars[-1]["ts"])),
            ]
            for position, label in positions:
                _place_label(footer, position, label)

        text.append(" " * axis_width, style=scheme.axis)
        text.append("└", style=scheme.axis)
        text.append("".join("─" if char == " " else char for char in footer), style=scheme.axis)
        text.append("\n")

        text.append(" " * axis_width, style=scheme.axis)
        text.append(" ", style=scheme.axis)
        hud = (
            "Tab/click focus  <- -> pan  Up/Down zoom  Home latest  "
            "V volume  Ctrl+G mode"
        )
        text.append(hud[:plot_width], style=scheme.header_dim)
        return text

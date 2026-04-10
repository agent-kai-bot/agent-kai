"""Chart workspace layout modes for the trading terminal."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChartLayoutMode:
    """Describe a chart-focused terminal layout mode.

    Attributes:
        name: Stable mode identifier.
        screen_class: CSS class applied to the root screen.
        description: User-facing summary of the layout.
    """

    name: str
    screen_class: str
    description: str


CHART_LAYOUT_MODES: dict[str, ChartLayoutMode] = {
    "dashboard": ChartLayoutMode(
        name="dashboard",
        screen_class="chart-mode-dashboard",
        description="Balanced layout with watchlist, chart, chat, alerts, and logs visible.",
    ),
    "inspect": ChartLayoutMode(
        name="inspect",
        screen_class="chart-mode-inspect",
        description="Chart-dominant layout with narrow side rails and a reduced chat row.",
    ),
    "focus": ChartLayoutMode(
        name="focus",
        screen_class="chart-mode-focus",
        description="Full-screen chart workspace with all side panels hidden.",
    ),
    "chat": ChartLayoutMode(
        name="chat",
        screen_class="chart-mode-chat",
        description="Chat-dominant layout that keeps the chart visible but secondary.",
    ),
}


CHART_LAYOUT_ALIASES: dict[str, str] = {
    "default": "dashboard",
    "full": "dashboard",
    "detail": "inspect",
    "analysis": "inspect",
    "half": "chat",
    "halfsize": "chat",
    "small": "chat",
    "chatty": "chat",
}


def normalize_chart_layout_mode(value: str | None) -> str:
    """Normalize a user or persisted value to a known chart layout mode.

    Args:
        value: Raw layout mode from a slash command or persisted state.

    Returns:
        A valid key from ``CHART_LAYOUT_MODES``. Unknown or empty values
        fall back to ``dashboard``.
    """

    if not value:
        return "dashboard"
    normalized = value.strip().lower()
    normalized = CHART_LAYOUT_ALIASES.get(normalized, normalized)
    if normalized in CHART_LAYOUT_MODES:
        return normalized
    return "dashboard"


def chart_layout_choices() -> list[str]:
    """Return the stable order of chart layout choices."""

    return list(CHART_LAYOUT_MODES.keys())


def cycle_chart_layout_mode(current: str, step: int = 1) -> str:
    """Return the next layout mode in the configured cycle order.

    Args:
        current: Current raw or normalized mode.
        step: Number of positions to rotate by.

    Returns:
        The normalized next layout mode.
    """

    choices = chart_layout_choices()
    current_mode = normalize_chart_layout_mode(current)
    current_index = choices.index(current_mode)
    return choices[(current_index + step) % len(choices)]

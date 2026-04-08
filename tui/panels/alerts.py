"""Alerts panel — signals and scanner alerts."""

from datetime import datetime
from textual.widgets import RichLog


class AlertsPanel(RichLog):
    """Displays trading signals and scanner alerts from agents."""

    DEFAULT_CSS = """
    AlertsPanel {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(markup=True, wrap=True, **kwargs)

    def add_signal(self, source: str, symbol: str, direction: str, message: str):
        """Add a trading signal."""
        ts = datetime.now().strftime("%H:%M:%S")
        color = "green" if direction.lower() in ("long", "buy", "bullish") else "red"
        self.write(f"[dim]{ts}[/] [bold {color}]{direction.upper()}[/] [bold]{symbol}[/] [{source}]")
        self.write(f"  {message[:120]}")

    def add_alert(self, alert_type: str, message: str):
        """Add a scanner alert."""
        ts = datetime.now().strftime("%H:%M:%S")
        color = "yellow" if alert_type == "pump" else "cyan"
        self.write(f"[dim]{ts}[/] [bold {color}][{alert_type}][/] {message[:150]}")

    def add_risk_warning(self, message: str):
        """Add a risk warning."""
        ts = datetime.now().strftime("%H:%M:%S")
        self.write(f"[dim]{ts}[/] [bold red]⚠ RISK[/] {message[:150]}")

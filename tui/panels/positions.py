"""Positions panel — open positions and P&L."""

from textual.widgets import DataTable


class PositionsPanel(DataTable):
    """Shows open paper trading positions with P&L."""

    DEFAULT_CSS = """
    PositionsPanel {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cursor_type = "row"

    def on_mount(self):
        self.add_columns("Symbol", "Side", "Qty", "Entry", "Price", "P&L", "P&L%")

    def update_positions(self, positions: list[dict], pnl: dict = None):
        """Refresh the positions table."""
        self.clear()
        for p in positions:
            pnl_val = p.get("unrealized_pnl", 0)
            pnl_pct = p.get("pnl_pct", 0)
            self.add_row(
                p["symbol"],
                p["side"].upper(),
                f"{p['quantity']:.4f}",
                f"${p['entry_price']:,.2f}",
                f"${p['current_price']:,.2f}",
                f"${pnl_val:+,.2f}",
                f"{pnl_pct:+.1f}%",
            )

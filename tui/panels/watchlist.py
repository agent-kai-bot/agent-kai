"""Watchlist panel — live prices for tracked symbols."""

import json
from textual.widgets import DataTable
from textual.widget import Widget


class WatchlistPanel(DataTable):
    """Shows live prices for tracked symbols."""

    DEFAULT_CSS = """
    WatchlistPanel {
        height: 1fr;
    }
    """

    def __init__(self, tracked_symbols=None, **kwargs):
        super().__init__(**kwargs)
        self.tracked_symbols = tracked_symbols or ["BTC", "ETH", "SOL"]
        self._prices: dict[str, dict] = {}
        self.cursor_type = "row"

    def on_mount(self):
        self.add_columns("Symbol", "Price", "Vol")
        for sym in self.tracked_symbols:
            self.add_row(sym, "---", "---", key=sym)

    def update_price(self, symbol: str, price: float, volume: float = None):
        """Update a symbol's price in the table."""
        symbol = symbol.upper()
        if symbol not in self.tracked_symbols:
            self.tracked_symbols.append(symbol)
            self.add_row(symbol, f"${price:,.2f}", f"{volume or 0:.1f}", key=symbol)
        else:
            price_str = f"${price:,.2f}"
            vol_str = f"{volume:.1f}" if volume else "---"
            try:
                row_key = self.get_row(symbol)
                self.update_cell(symbol, "Price", price_str)
                self.update_cell(symbol, "Vol", vol_str)
            except Exception:
                pass

        old_price = self._prices.get(symbol, {}).get("price", 0)
        self._prices[symbol] = {"price": price, "volume": volume}

    def get_selected_symbol(self) -> str | None:
        """Get the currently selected symbol."""
        if self.cursor_row is not None and self.cursor_row < len(self.tracked_symbols):
            return self.tracked_symbols[self.cursor_row]
        return None

    def add_symbol(self, symbol: str):
        """Add a symbol to the watchlist."""
        symbol = symbol.upper()
        if symbol not in self.tracked_symbols:
            self.tracked_symbols.append(symbol)
            self.add_row(symbol, "---", "---", key=symbol)

    def remove_symbol(self, symbol: str):
        """Remove a symbol from the watchlist."""
        symbol = symbol.upper()
        if symbol in self.tracked_symbols:
            self.tracked_symbols.remove(symbol)
            try:
                self.remove_row(symbol)
            except Exception:
                pass

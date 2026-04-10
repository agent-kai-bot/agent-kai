"""Watchlist panel — live prices for tracked symbols."""

import logging

from textual.widgets import DataTable

logger = logging.getLogger(__name__)


class WatchlistPanel(DataTable):
    """Shows live prices for tracked symbols.

    Built on Textual's ``DataTable``. The schema is three columns
    (Symbol / Price / Vol) with explicit string keys so
    ``update_cell(row_key, column_key, ...)`` actually matches.

    The previous version used ``add_columns("Symbol", "Price", "Vol")``
    without keys, which made Textual auto-generate column keys like
    ``ColumnKey('column_1')``. Subsequent ``update_cell(symbol, "Price", ...)``
    calls raised ``CellDoesNotExist`` (because "Price" wasn't the actual
    column key) and the bare ``except Exception: pass`` silently
    swallowed the error. The seed would run, try to update, fail
    silently, and the initial "---" placeholders stayed on screen
    forever. That's why the user saw symbols but "--- ---" for
    price and volume. Fixed by using ``add_column`` (singular) with
    explicit ``key=`` arguments.
    """

    DEFAULT_CSS = """
    WatchlistPanel {
        height: 1fr;
    }
    """

    # Column keys — used in update_cell calls. Must match what
    # on_mount passes to add_column.
    COL_SYMBOL = "symbol"
    COL_PRICE = "price"
    COL_CHANGE = "change_24h"
    COL_VOL = "vol"

    def __init__(self, tracked_symbols=None, **kwargs):
        super().__init__(**kwargs)
        self.tracked_symbols = tracked_symbols or ["BTC", "ETH", "SOL"]
        # Live price + volume state, keyed by symbol. Kept alongside
        # the DataTable so consumers can query the latest value
        # without parsing rendered cell text.
        self._prices: dict[str, dict] = {}
        self.cursor_type = "row"

    def on_mount(self):
        # add_column (singular) lets us set explicit string keys so
        # update_cell(row_key, column_key, value) works against a
        # stable identifier instead of auto-generated ColumnKey
        # objects. See the class docstring for the full history.
        self.add_column("Symbol", key=self.COL_SYMBOL)
        self.add_column("Price", key=self.COL_PRICE)
        self.add_column("24h%", key=self.COL_CHANGE)
        self.add_column("Vol", key=self.COL_VOL)
        for sym in self.tracked_symbols:
            self.add_row(sym, "---", "---", "---", key=sym)

    @staticmethod
    def _format_change(change: float | None) -> str:
        """Render a 24h percent change with sign + color markup.

        Returns rich-markup wrapped output: green for positive,
        red for negative, dim for zero / missing. Caller passes
        the result straight to update_cell which renders the
        markup inline.
        """
        if change is None:
            return "[dim]---[/]"
        if change > 0:
            return f"[bold green]+{change:.2f}%[/]"
        if change < 0:
            return f"[bold red]{change:.2f}%[/]"
        return "[dim]0.00%[/]"

    def update_price(
        self,
        symbol: str,
        price: float,
        volume: float | None = None,
        change_24h_pct: float | None = None,
    ):
        """Update a symbol's price + 24h change + volume in the table.

        Adds the symbol as a new row if it's not already tracked.
        Otherwise updates the Price, 24h%, and Vol cells on the
        existing row via the column keys declared at mount time.

        ``change_24h_pct`` is optional — pass None when the data
        source doesn't provide it (the cell renders as ``---``).
        """
        symbol = symbol.upper()
        price_str = f"${price:,.2f}" if price is not None else "---"
        vol_str = f"{volume:,.1f}" if volume else "---"
        change_str = self._format_change(change_24h_pct)

        if symbol not in self.tracked_symbols:
            self.tracked_symbols.append(symbol)
            try:
                self.add_row(symbol, price_str, change_str, vol_str, key=symbol)
            except Exception as exc:
                logger.warning(
                    "watchlist add_row failed for %s: %s", symbol, exc
                )
        else:
            # Update the three live cells via column key. No silent
            # swallow — log any failure so a future regression is
            # visible instead of showing "--- ---" forever.
            try:
                self.update_cell(symbol, self.COL_PRICE, price_str)
            except Exception as exc:
                logger.warning(
                    "watchlist update_cell price failed for %s: %s",
                    symbol, exc,
                )
            try:
                self.update_cell(symbol, self.COL_CHANGE, change_str)
            except Exception as exc:
                logger.warning(
                    "watchlist update_cell change failed for %s: %s",
                    symbol, exc,
                )
            try:
                self.update_cell(symbol, self.COL_VOL, vol_str)
            except Exception as exc:
                logger.warning(
                    "watchlist update_cell vol failed for %s: %s",
                    symbol, exc,
                )

        self._prices[symbol] = {
            "price": price,
            "volume": volume,
            "change_24h_pct": change_24h_pct,
        }

    def get_selected_symbol(self) -> str | None:
        """Get the currently selected symbol."""
        if self.cursor_row is not None and self.cursor_row < len(self.tracked_symbols):
            return self.tracked_symbols[self.cursor_row]
        return None

    def add_symbol(self, symbol: str):
        """Add a symbol to the watchlist (empty price/change/vol until populated)."""
        symbol = symbol.upper()
        if symbol not in self.tracked_symbols:
            self.tracked_symbols.append(symbol)
            try:
                self.add_row(symbol, "---", "---", "---", key=symbol)
            except Exception as exc:
                logger.warning(
                    "watchlist add_symbol failed for %s: %s", symbol, exc
                )

    def remove_symbol(self, symbol: str):
        """Remove a symbol from the watchlist."""
        symbol = symbol.upper()
        if symbol in self.tracked_symbols:
            self.tracked_symbols.remove(symbol)
            self._prices.pop(symbol, None)
            try:
                self.remove_row(symbol)
            except Exception as exc:
                logger.warning(
                    "watchlist remove_row failed for %s: %s", symbol, exc
                )

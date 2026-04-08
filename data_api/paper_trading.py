"""In-memory paper trading engine with NATS position updates."""

import json
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict

import requests

from data_api.config import API_PORT

API_BASE = f"http://localhost:{API_PORT}/api/v1"
PERSIST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "workspaces", "trader", "portfolio.json"
)


@dataclass
class Position:
    symbol: str
    side: str  # "long" or "short"
    quantity: float
    entry_price: float
    current_price: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    opened_at: str = ""
    unrealized_pnl: float = 0.0
    pnl_pct: float = 0.0

    def update_pnl(self, price: float):
        self.current_price = price
        if self.side == "long":
            self.unrealized_pnl = (price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - price) * self.quantity
        if self.entry_price > 0:
            self.pnl_pct = (self.unrealized_pnl / (self.entry_price * self.quantity)) * 100


@dataclass
class OrderResult:
    success: bool
    message: str
    position: Position | None = None
    fill_price: float = 0.0


class PaperPortfolio:
    """Paper trading portfolio with position tracking."""

    def __init__(self, starting_balance: float = 100_000.0):
        self.starting_balance = starting_balance
        self.cash = starting_balance
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[dict] = []
        self._load()

    def _get_price(self, symbol: str) -> float | None:
        try:
            resp = requests.get(f"{API_BASE}/price/{symbol}", timeout=5)
            data = resp.json()
            return data.get("price")
        except Exception:
            return None

    def place_order(self, symbol: str, side: str, quantity: float,
                    order_type: str = "market", price: float | None = None,
                    stop_loss: float | None = None,
                    take_profit: float | None = None) -> OrderResult:
        """Place a paper trade."""
        symbol = symbol.upper()
        side = side.lower()
        if side not in ("buy", "sell", "long", "short"):
            return OrderResult(False, f"Invalid side: {side}")

        # Normalize side
        if side == "buy":
            side = "long"
        elif side == "sell":
            side = "short"

        # Get fill price
        if order_type == "market":
            fill_price = self._get_price(symbol)
            if fill_price is None:
                return OrderResult(False, f"Could not get price for {symbol}")
        elif order_type == "limit":
            if price is None:
                return OrderResult(False, "Limit order requires a price")
            fill_price = price
        else:
            return OrderResult(False, f"Invalid order type: {order_type}")

        cost = fill_price * quantity

        # Check if closing an existing position
        if symbol in self.positions:
            pos = self.positions[symbol]
            if (pos.side == "long" and side == "short") or (pos.side == "short" and side == "long"):
                return self._close_position(symbol, fill_price, quantity)

        # Check cash for new long positions
        if side == "long" and cost > self.cash:
            return OrderResult(False, f"Insufficient cash: need ${cost:.2f}, have ${self.cash:.2f}")

        # Open new position or add to existing
        if symbol in self.positions:
            pos = self.positions[symbol]
            # Average in
            total_qty = pos.quantity + quantity
            pos.entry_price = ((pos.entry_price * pos.quantity) + (fill_price * quantity)) / total_qty
            pos.quantity = total_qty
            if stop_loss is not None:
                pos.stop_loss = stop_loss
            if take_profit is not None:
                pos.take_profit = take_profit
            pos.update_pnl(fill_price)
        else:
            pos = Position(
                symbol=symbol,
                side=side,
                quantity=quantity,
                entry_price=fill_price,
                current_price=fill_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                opened_at=datetime.now(timezone.utc).isoformat(),
            )
            self.positions[symbol] = pos

        if side == "long":
            self.cash -= cost

        self._save()
        return OrderResult(
            True,
            f"{'Bought' if side == 'long' else 'Shorted'} {quantity} {symbol} @ ${fill_price:.2f} (cost: ${cost:.2f})",
            position=pos,
            fill_price=fill_price,
        )

    def _close_position(self, symbol: str, close_price: float,
                        quantity: float | None = None) -> OrderResult:
        """Close (or reduce) a position."""
        if symbol not in self.positions:
            return OrderResult(False, f"No position in {symbol}")

        pos = self.positions[symbol]
        close_qty = min(quantity, pos.quantity) if quantity else pos.quantity
        pos.update_pnl(close_price)

        realized_pnl = (pos.unrealized_pnl / pos.quantity) * close_qty
        proceeds = close_price * close_qty

        self.closed_trades.append({
            "symbol": symbol,
            "side": pos.side,
            "quantity": close_qty,
            "entry_price": pos.entry_price,
            "exit_price": close_price,
            "pnl": realized_pnl,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        })

        if pos.side == "long":
            self.cash += proceeds

        if close_qty >= pos.quantity:
            del self.positions[symbol]
            msg = f"Closed {symbol} {pos.side} {close_qty} @ ${close_price:.2f}, P&L: ${realized_pnl:+.2f}"
        else:
            pos.quantity -= close_qty
            pos.update_pnl(close_price)
            msg = f"Reduced {symbol} by {close_qty}, remaining: {pos.quantity}, P&L on closed: ${realized_pnl:+.2f}"

        self._save()
        return OrderResult(True, msg, fill_price=close_price)

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        """Get current positions, optionally filtered by symbol."""
        # Update prices
        for sym, pos in self.positions.items():
            price = self._get_price(sym)
            if price:
                pos.update_pnl(price)

        positions = self.positions.values()
        if symbol:
            positions = [p for p in positions if p.symbol == symbol.upper()]
        return [asdict(p) for p in positions]

    def get_pnl(self) -> dict:
        """Get portfolio P&L summary."""
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        total_realized = sum(t["pnl"] for t in self.closed_trades)
        position_value = sum(p.current_price * p.quantity for p in self.positions.values())

        return {
            "cash": round(self.cash, 2),
            "position_value": round(position_value, 2),
            "total_value": round(self.cash + position_value, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "realized_pnl": round(total_realized, 2),
            "total_pnl": round(total_unrealized + total_realized, 2),
            "total_pnl_pct": round(((self.cash + position_value - self.starting_balance) / self.starting_balance) * 100, 2),
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed_trades),
        }

    def _save(self):
        try:
            os.makedirs(os.path.dirname(PERSIST_PATH), exist_ok=True)
            data = {
                "cash": self.cash,
                "starting_balance": self.starting_balance,
                "positions": {k: asdict(v) for k, v in self.positions.items()},
                "closed_trades": self.closed_trades,
            }
            with open(PERSIST_PATH, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception:
            pass

    def _load(self):
        try:
            if os.path.isfile(PERSIST_PATH):
                with open(PERSIST_PATH) as f:
                    data = json.load(f)
                self.cash = data.get("cash", self.starting_balance)
                self.starting_balance = data.get("starting_balance", self.starting_balance)
                self.closed_trades = data.get("closed_trades", [])
                for k, v in data.get("positions", {}).items():
                    self.positions[k] = Position(**v)
        except Exception:
            pass


# Singleton
portfolio = PaperPortfolio()

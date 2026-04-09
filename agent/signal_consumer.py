"""Signal consumer — receives real-time trading signals from the vpn-stack
signal scanners via NATS and makes them available to agent tools and the TUI.

Signals arrive on NATS subjects matching ``signals.{strategy}.{symbol}``
(e.g. ``signals.clucmay02.BTC``, ``signals.double_top.ETH``). The consumer
keeps a bounded ring buffer of the last N signals so the agent can query
recent history via the ``get_signals`` tool without needing to be online
at the moment the signal fired.

The AI token analyzer also publishes events on ``ai.analysis.completed``
which we consume and normalize into the same buffer.

Usage::

    consumer = SignalConsumer(max_signals=200)

    # In the TUI's on_mount:
    await consumer.subscribe(bus)

    # From an agent tool:
    recent = consumer.query(symbol="BTC", limit=5)

    # The TUI also registers a callback for live display:
    consumer.on_signal = lambda sig: alerts_panel.add_signal(...)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """Normalized signal record stored in the ring buffer."""

    source: str  # "signal-scanner", "ai-token-analyzer", "manual"
    strategy: str  # "clucmay02", "double_top", "ai_daily", ...
    symbol: str  # "BTC", "ETH", "SOL"
    signal_type: str  # "BUY", "SELL", "ANALYSIS", ...
    price: float = 0.0
    timestamp: str = ""  # ISO 8601
    received_at: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Flatten details into top-level for cleaner tool output
        extra = d.pop("details", {})
        d.update(extra)
        return d

    def summary(self) -> str:
        """One-line summary for the TUI alerts panel."""
        direction = self.signal_type.upper()
        return (
            f"[{self.strategy}] {direction} {self.symbol} @ ${self.price:,.2f}"
        )


class SignalConsumer:
    """Bounded ring buffer of trading signals with NATS subscription support."""

    def __init__(self, max_signals: int = 200):
        self._buffer: deque[Signal] = deque(maxlen=max_signals)
        self._subscribed = False
        # Optional callback fired for every incoming signal.
        # The TUI sets this to route signals to the AlertsPanel.
        self.on_signal: Optional[Callable[[Signal], None]] = None

    # ── NATS integration ────────────────────────────────────

    async def subscribe(self, bus: Any) -> None:
        """Subscribe to signal subjects on the NATS bus.

        Call this from the TUI's ``on_mount`` after the bus is connected.
        Subscribes to:
        - ``signals.>``            — signal scanner events
        - ``ai.analysis.completed`` — AI token analyzer events
        """
        if self._subscribed or bus is None:
            return
        try:
            await bus.subscribe("signals.>", self._handle_signal)
            await bus.subscribe("ai.analysis.completed", self._handle_ai_analysis)
            self._subscribed = True
            logger.info("SignalConsumer subscribed to signals.> and ai.analysis.completed")
        except Exception as exc:
            logger.warning("SignalConsumer subscribe failed: %s", exc)

    async def _handle_signal(self, subject: str, payload: dict) -> None:
        """Handle a signal scanner event from NATS."""
        parts = subject.split(".")
        strategy = parts[1] if len(parts) > 1 else "unknown"
        symbol = parts[2] if len(parts) > 2 else payload.get("symbol", "?")

        sig = Signal(
            source=payload.get("source", "signal-scanner"),
            strategy=payload.get("strategy", strategy),
            symbol=payload.get("symbol", symbol).upper(),
            signal_type=payload.get("signal_type", "unknown"),
            price=float(payload.get("price", 0)),
            timestamp=payload.get("timestamp", ""),
            details={
                k: v
                for k, v in payload.items()
                if k not in ("source", "strategy", "symbol", "signal_type", "price", "timestamp")
            },
        )
        self._ingest(sig)

    async def _handle_ai_analysis(self, subject: str, payload: dict) -> None:
        """Handle an AI token analyzer completion event from NATS."""
        sig = Signal(
            source="ai-token-analyzer",
            strategy=payload.get("use_case", "ai_daily"),
            symbol=payload.get("symbol", "?").upper(),
            signal_type="ANALYSIS",
            timestamp=payload.get("timestamp", ""),
            details={"result_id": payload.get("result_id")},
        )
        self._ingest(sig)

    # ── Ingestion ───────────────────────────────────────────

    def _ingest(self, sig: Signal) -> None:
        """Add a signal to the buffer and fire the callback."""
        self._buffer.append(sig)
        logger.info("Signal received: %s", sig.summary())
        if self.on_signal:
            try:
                self.on_signal(sig)
            except Exception as exc:
                logger.warning("on_signal callback error: %s", exc)

    def add_manual(
        self,
        strategy: str,
        symbol: str,
        signal_type: str,
        price: float = 0.0,
        **details: Any,
    ) -> Signal:
        """Programmatically add a signal (for tests or manual injection)."""
        sig = Signal(
            source="manual",
            strategy=strategy,
            symbol=symbol.upper(),
            signal_type=signal_type,
            price=price,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            details=details,
        )
        self._ingest(sig)
        return sig

    # ── Query interface (used by agent tools) ───────────────

    def query(
        self,
        symbol: str | None = None,
        strategy: str | None = None,
        signal_type: str | None = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Return recent signals matching the optional filters.

        Results are ordered newest-first. Filters are AND-combined:
        ``query(symbol="BTC", strategy="clucmay02")`` returns only
        signals that match both.
        """
        results: list[Signal] = []
        for sig in reversed(self._buffer):
            if symbol and sig.symbol != symbol.upper():
                continue
            if strategy and sig.strategy.lower() != strategy.lower():
                continue
            if signal_type and sig.signal_type.lower() != signal_type.lower():
                continue
            results.append(sig)
            if len(results) >= limit:
                break
        return [s.to_dict() for s in results]

    @property
    def count(self) -> int:
        return len(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()

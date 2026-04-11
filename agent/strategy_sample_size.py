"""Canonical ASO v1 sample-size rule."""

from __future__ import annotations

MIN_TRADES_V1 = 50
MIN_TRADES_CLUSTERED_V1 = 100
MIN_TRADES_SHADOW_V1 = 10
OVERLAP_THRESHOLD = 0.5


def check_sample_size(total_trades, avg_bars_held, total_bars, stage):
    """Apply the single v1 sample-size gate."""
    if stage == "shadow":
        if total_trades < MIN_TRADES_SHADOW_V1:
            return False, f"Shadow needs ≥{MIN_TRADES_SHADOW_V1} trades, got {total_trades}"
        return True, "ok"

    min_required = MIN_TRADES_V1
    if total_trades > 0:
        avg_trade_spacing = total_bars / total_trades
        if avg_bars_held > avg_trade_spacing * OVERLAP_THRESHOLD:
            min_required = MIN_TRADES_CLUSTERED_V1

    if total_trades < min_required:
        return False, f"Need ≥{min_required} trades (overlap-adjusted), got {total_trades}"
    return True, "ok"

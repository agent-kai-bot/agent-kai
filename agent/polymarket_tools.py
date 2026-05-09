"""Polymarket-specific tools for the kai agent.

Wraps `~/git/OPS/vpn-stack/scripts/lib/local_first.polymarket.best_bid_ask_async`
so the agent can fetch the live order-book best bid/ask for a Polymarket token
without shelling out to python_exec each time.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

_LOCAL_FIRST_PATH = "/home/atc/git/OPS/vpn-stack/scripts/lib"


class PolymarketBboInput(BaseModel):
    token_id: str = Field(
        ...,
        description=(
            "Polymarket CLOB token id (the long numeric string). NOT the market "
            "address or slug. Required."
        ),
    )
    allow_rest_fallback: bool = Field(
        default=True,
        description=(
            "If True (default), fall back to the gamma REST endpoint when no "
            "live NATS BBO is available. The result will have stale=True."
        ),
    )


def _polymarket_bbo(token_id: str, allow_rest_fallback: bool = True) -> dict[str, Any]:
    """Fetch live best bid/ask for a Polymarket token.

    Returns a dict like:
      {"ok": true, "source": "nats|rest", "bid": "0.42", "ask": "0.43",
       "ts_event": "2026-05-09T19:30:00Z", "stale": false, "fetched_at": "..."}
    Or on failure:
      {"ok": false, "error": "...", "fetched_at": "..."}

    The `fetched_at` timestamp is set right when this tool returned, so the
    agent can verify freshness against the alarm's own ts.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    if not token_id:
        return {"ok": False, "error": "token_id required", "fetched_at": fetched_at}
    try:
        if _LOCAL_FIRST_PATH not in sys.path:
            sys.path.insert(0, _LOCAL_FIRST_PATH)
        from local_first import polymarket  # type: ignore

        result = asyncio.run(
            polymarket.best_bid_ask_async(
                str(token_id), allow_rest_fallback=allow_rest_fallback
            )
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "fetched_at": fetched_at,
        }
    if not isinstance(result, dict):
        return {"ok": False, "error": f"unexpected_result_type:{type(result).__name__}", "fetched_at": fetched_at}
    out = {
        "ok": bool(result.get("ok")),
        "source": str(result.get("source", "")),
        "bid": str(result.get("bid", "")),
        "ask": str(result.get("ask", "")),
        "ts_event": str(result.get("ts_event", "")),
        "stale": bool(result.get("stale", False)),
        "fetched_at": fetched_at,
    }
    if not out["ok"]:
        out["error"] = str(result.get("error") or result.get("detail") or "unknown")
        out["fallback_reason"] = str(result.get("fallback_reason", ""))
    return out


def create_polymarket_tools() -> list[StructuredTool]:
    """Tools the kai agent uses for Polymarket-specific lookups."""
    return [
        StructuredTool.from_function(
            func=_polymarket_bbo,
            name="polymarket_bbo",
            description=(
                "Fetch the LIVE Polymarket order-book best bid/ask for one CLOB "
                "token. Use this whenever you need the current price on a token "
                "(e.g. validating a polymarket alarm before acting). Returns "
                "{ok, source: nats|rest, bid, ask, ts_event, stale, fetched_at}. "
                "Always check the `stale` and `source` fields — `nats` source is "
                "live, `rest` source is the gamma fallback (slower, can lag). "
                "DO NOT guess prices; always call this tool."
            ),
            args_schema=PolymarketBboInput,
        ),
    ]

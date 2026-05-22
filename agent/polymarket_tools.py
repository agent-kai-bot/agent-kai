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
_AGENT_KAI_SHARED_PATH = "/home/atc/git/OPS/agent-kai-shared"
_DISCORD_USER_AGENT = "Kai-Alert-Response (https://agent-k.ai, 1.0)"


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
      {"ok": true, "source": "redis|nats|rest", "bid": "0.42", "ask": "0.43",
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
        "ts_ingest": str(result.get("ts_ingest", "")),
        "age_s": str(result.get("age_s", "")),
        "stale": bool(result.get("stale", False)),
        "fetched_at": fetched_at,
    }
    if not out["ok"]:
        out["error"] = str(result.get("error") or result.get("detail") or "unknown")
        out["fallback_reason"] = str(result.get("fallback_reason", ""))
        out["redis_error"] = str(result.get("redis_error", ""))
    return out


class DiscordAlertInput(BaseModel):
    title: str = Field(..., description="Embed title — should be a short human-readable summary (e.g. '🟡 strategy_b_burst — HOU vs CIN — Astros').")
    description: str = Field(default="", description="Embed body. Markdown-supported. Keep ~4 lines.")
    color: int = Field(default=3066993, description="Embed color (decimal RGB). 3066993=green/p0, 16776960=yellow/p1 sentinel, 15158332=red/p2 emergency.")
    url: str = Field(default="", description="Optional click-through URL (e.g. https://polymarket.com/event/<slug>).")
    rule_id: str = Field(default="", description="Alarm rule_id, surfaced in the embed footer.")
    sentinel_match: bool = Field(default=False, description="Whether this token is in today's edge sentinel.")
    timestamp: str = Field(default="", description="ISO 8601 timestamp of the alarm/event (used as Discord embed timestamp).")
    webhook_url: str = Field(default="", description="Optional override; if empty, reads $KAI_DISCORD_WEBHOOK_URL.")


def _discord_alert_send(
    title: str,
    description: str = "",
    color: int = 3066993,
    url: str = "",
    rule_id: str = "",
    sentinel_match: bool = False,
    timestamp: str = "",
    webhook_url: str = "",
) -> dict[str, Any]:
    """POST a single alert as a Discord embed via the operator-configured webhook.

    Handles Discord's Cloudflare WAF correctly: sends a non-default User-Agent
    so we don't get blocked with HTTP 403 'error code: 1010'. Returns
    {ok, http_status, response_body, fetched_at, webhook_id_redacted}.
    Always returns a dict; never raises.
    """
    import os
    import re
    fetched_at = datetime.now(timezone.utc).isoformat()
    hook = webhook_url or os.environ.get("KAI_DISCORD_WEBHOOK_URL", "").strip()
    if not hook:
        return {"ok": False, "error": "KAI_DISCORD_WEBHOOK_URL not set", "fetched_at": fetched_at}
    redacted_id = "?"
    m = re.match(r"https://discord\.com/api/webhooks/(\d+)/", hook)
    if m:
        redacted_id = m.group(1)
    embed = {"title": title, "description": description, "color": int(color)}
    if url:
        embed["url"] = url
    if timestamp:
        embed["timestamp"] = timestamp
    footer_bits = []
    if rule_id:
        footer_bits.append(f"rule {rule_id}")
    footer_bits.append(f"sentinel={'yes' if sentinel_match else 'no'}")
    embed["footer"] = {"text": " · ".join(footer_bits)}
    payload = {"username": "Kai", "embeds": [embed]}
    try:
        import requests  # type: ignore
        resp = requests.post(
            hook,
            json=payload,
            timeout=8,
            headers={"User-Agent": _DISCORD_USER_AGENT},
        )
        ok = 200 <= resp.status_code < 300
        body = ""
        if not ok:
            try:
                body = resp.text[:300]
            except Exception:
                body = "<unreadable>"
        return {
            "ok": ok,
            "http_status": resp.status_code,
            "response_body": body,
            "webhook_id": redacted_id,
            "fetched_at": fetched_at,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "webhook_id": redacted_id,
            "fetched_at": fetched_at,
        }


class TokenResolveInput(BaseModel):
    token_id: str = Field(
        ...,
        description=(
            "Polymarket CLOB token id (long numeric string). NOT the market "
            "address or slug. Required."
        ),
    )


def _polymarket_token_resolve(token_id: str) -> dict[str, Any]:
    """Resolve a Polymarket token_id to {slug, title, outcome, category}.

    Two-tier cache: in-process LRU + on-disk 24h TTL (zero-network for
    repeats). Falls back: today's sentinel JSON -> Polymarket gamma API ->
    safe placeholder. Always returns a dict; never raises.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    if not token_id:
        return {"ok": False, "error": "token_id required", "fetched_at": fetched_at}
    try:
        if _AGENT_KAI_SHARED_PATH not in sys.path:
            sys.path.insert(0, _AGENT_KAI_SHARED_PATH)
        from token_resolver import resolve_token  # type: ignore

        info = resolve_token(str(token_id))
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "fetched_at": fetched_at,
        }
    if not isinstance(info, dict):
        return {"ok": False, "error": f"unexpected:{type(info).__name__}", "fetched_at": fetched_at}
    return {
        "ok": True,
        "slug": str(info.get("slug", "")),
        "title": str(info.get("title", "")),
        "outcome": str(info.get("outcome", "")),
        "category": str(info.get("category", "")),
        "summary": (
            f"[{info.get('category','?')}] {info.get('title','?')} "
            f":: {info.get('outcome','?')}"
        ),
        "fetched_at": fetched_at,
    }


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
                "{ok, source: redis|nats|rest, bid, ask, ts_event, stale, fetched_at}. "
                "Always check the `stale` and `source` fields — `redis`/`nats` sources are "
                "local live feed/cache, `rest` source is the public CLOB fallback (slower, can lag). "
                "DO NOT guess prices; always call this tool."
            ),
            args_schema=PolymarketBboInput,
        ),
        StructuredTool.from_function(
            func=_polymarket_token_resolve,
            name="polymarket_token_resolve",
            description=(
                "Resolve a Polymarket CLOB token_id to a human-readable "
                "{slug, title, outcome, category, summary}. Two-tier cached "
                "(in-process LRU + on-disk 24h TTL), so repeat calls are free. "
                "Falls back: today's sentinel JSON -> Polymarket gamma API -> "
                "safe placeholder. Use to label alarms, embed titles, or audit "
                "rows with the actual market name (e.g. 'MLB: HOU vs CIN :: "
                "Houston Astros') instead of the raw token id. Returns "
                "{ok, slug, title, outcome, category, summary, fetched_at}."
            ),
            args_schema=TokenResolveInput,
        ),
        StructuredTool.from_function(
            func=_discord_alert_send,
            name="discord_alert_send",
            description=(
                "POST one alert as a Discord embed via the operator-configured "
                "webhook (KAI_DISCORD_WEBHOOK_URL env var). Use this for ALL "
                "alert delivery — DO NOT call requests/curl/urllib yourself for "
                "Discord; this tool handles the User-Agent header that Discord's "
                "Cloudflare WAF requires (default Python urllib gets HTTP 403 "
                "with 'error code 1010'). Color: 3066993=green standard, "
                "16776960=yellow sentinel-match, 15158332=red emergency. "
                "Returns {ok, http_status, response_body, webhook_id, fetched_at}. "
                "On HTTP 204 success, http_status=204 and response_body is empty."
            ),
            args_schema=DiscordAlertInput,
        ),
    ]

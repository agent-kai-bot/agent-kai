"""agent-k.ai-backed data access helpers."""

from __future__ import annotations

from data_api.agent_kai_client import AgentKaiMarketClient
from data_api.config import AGENT_KAI_API_KEY, TRACKED_SYMBOLS

_agent_kai_client: AgentKaiMarketClient | None = None


def _require_api_key() -> str:
    """Return the configured API key or raise a clear startup error."""
    if not AGENT_KAI_API_KEY:
        raise RuntimeError("AGENT_KAI_API_KEY is required for market data access")
    return AGENT_KAI_API_KEY


def _get_agent_kai_client() -> AgentKaiMarketClient:
    """Return the shared agent-kai client instance."""
    global _agent_kai_client
    if _agent_kai_client is None:
        _agent_kai_client = AgentKaiMarketClient(api_key=_require_api_key())
    return _agent_kai_client


async def create_pool() -> None:
    """Initialize the shared upstream client."""
    client = _get_agent_kai_client()
    await client.warmup()


async def close_pool() -> None:
    """Close the shared upstream client."""
    global _agent_kai_client
    if _agent_kai_client is not None:
        await _agent_kai_client.close()
        _agent_kai_client = None


async def get_backend_health() -> dict:
    """Return health information for the upstream market-data provider."""
    try:
        return await _get_agent_kai_client().healthcheck()
    except Exception as exc:
        return {"status": "error", "provider": "agent-kai", "detail": str(exc)}


async def fetch_ohlcv(
    symbol: str,
    interval: str = "1m",
    limit: int = 500,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Fetch OHLCV bars from agent-k.ai."""
    return await _get_agent_kai_client().fetch_ohlcv(symbol, interval, limit, start, end)


async def fetch_symbols() -> list[dict]:
    """Return tracked symbols with their latest prices."""
    return await _get_agent_kai_client().fetch_symbols(TRACKED_SYMBOLS)


async def fetch_latest_price(symbol: str) -> dict | None:
    """Fetch the latest price update for a symbol."""
    return await _get_agent_kai_client().fetch_latest_price(symbol)

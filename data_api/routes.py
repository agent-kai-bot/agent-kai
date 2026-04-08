"""FastAPI REST endpoints for market data."""

from fastapi import APIRouter, Query
from data_api.db import (
    fetch_latest_price,
    fetch_ohlcv,
    fetch_symbols,
    get_backend_health,
)
from data_api.models import OHLCVBar, PriceUpdate, SymbolInfo

router = APIRouter(prefix="/api/v1")


@router.get("/health")
async def health():
    """Return health information for the active market data provider."""
    return await get_backend_health()


@router.get("/symbols", response_model=list[SymbolInfo])
async def list_symbols():
    rows = await fetch_symbols()
    return [SymbolInfo(**r) for r in rows]


@router.get("/ohlcv/{symbol}", response_model=list[OHLCVBar])
async def get_ohlcv(
    symbol: str,
    interval: str = Query("1m", description="Timeframe: 1m, 5m, 15m, 1h, 6h, 1d"),
    limit: int = Query(500, ge=1, le=5000),
    start: str = Query(None, description="Start datetime (ISO)"),
    end: str = Query(None, description="End datetime (ISO)"),
):
    rows = await fetch_ohlcv(symbol, interval, limit, start, end)
    return [OHLCVBar(**r) for r in rows]


@router.get("/price/{symbol}", response_model=PriceUpdate)
async def get_price(symbol: str):
    row = await fetch_latest_price(symbol)
    if not row:
        return PriceUpdate(symbol=symbol.upper(), price=0.0,
                           ts="1970-01-01T00:00:00Z")
    return PriceUpdate(**row)

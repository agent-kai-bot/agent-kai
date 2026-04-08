"""Pydantic models for the data API."""

from datetime import datetime
from pydantic import BaseModel


class OHLCVBar(BaseModel):
    symbol: str
    interval: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class PriceUpdate(BaseModel):
    symbol: str
    price: float
    ts: datetime
    volume: float | None = None


class SymbolInfo(BaseModel):
    symbol: str
    latest_price: float | None = None
    latest_ts: datetime | None = None

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from .evm import normalize_address


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    return int(value)


def _csv_addresses(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    values = []
    for item in value.split(","):
        if not item.strip():
            continue
        values.append(normalize_address(item))
    return tuple(dict.fromkeys(values))


@dataclass(slots=True)
class Settings:
    service_name: str
    log_level: str
    database_url: str
    redis_url: str
    rpc_gateway_url: str
    polygon_rpc_http: str
    polygon_rpc_ws: str
    alchemy_rpc_url: str | None
    reorg_depth: int
    backfill_days: int
    tracked_tokens: tuple[str, ...]
    host: str = "0.0.0.0"
    port: int = 8000
    request_timeout_seconds: float = 20.0
    log_range_limit: int = 1_000
    whale_threshold_usd: float = 10_000.0

    @property
    def has_alchemy(self) -> bool:
        return bool(self.alchemy_rpc_url)


def load_settings(service_name: str) -> Settings:
    default_gateway = "http://localhost:8000"
    if os.getenv("RPC_GATEWAY_URL") is None and service_name != "rpc_gateway":
        default_gateway = "http://rpc-gateway:8000"

    port = 8000
    if service_name == "analytics":
        port = _env_int("PORT", 8000)
    elif service_name == "rpc_gateway":
        port = _env_int("PORT", 8000)

    return Settings(
        service_name=service_name,
        log_level=_env("LOG_LEVEL", "INFO") or "INFO",
        database_url=_env("DATABASE_URL", "postgresql+asyncpg://indexer:indexer_dev@localhost:5433/polygon_indexer")
        or "",
        redis_url=_env("REDIS_URL", "redis://localhost:6380") or "",
        rpc_gateway_url=_env("RPC_GATEWAY_URL", default_gateway) or default_gateway,
        polygon_rpc_http=_env("POLYGON_RPC_HTTP", "http://localhost:8545") or "",
        polygon_rpc_ws=_env("POLYGON_RPC_WS", "ws://localhost:8546") or "",
        alchemy_rpc_url=_env("ALCHEMY_RPC_URL"),
        reorg_depth=_env_int("REORG_DEPTH", 64),
        backfill_days=_env_int("BACKFILL_DAYS", 30),
        tracked_tokens=_csv_addresses(_env("TRACKED_TOKENS")),
        port=port,
    )


def merge_addresses(*groups: Iterable[str]) -> tuple[str, ...]:
    values: list[str] = []
    for group in groups:
        for address in group:
            values.append(normalize_address(address))
    return tuple(dict.fromkeys(values))


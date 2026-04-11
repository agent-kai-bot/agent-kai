from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import count
from typing import Any

import httpx
import websockets

from .evm import to_hex_quantity

LOGGER = logging.getLogger(__name__)


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "JsonRpcError":
        return cls(payload.get("code", -1), payload.get("message", "unknown json-rpc error"), payload.get("data"))


def is_method_missing(error: Exception) -> bool:
    if not isinstance(error, JsonRpcError):
        return False
    return error.code in {-32601, -32004}


class JsonRpcHttpClient:
    def __init__(self, url: str, *, timeout: float = 20.0, name: str = "rpc"):
        self.url = url
        self.name = name
        self.client = httpx.AsyncClient(timeout=timeout)
        self._ids = count(1)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, params: list[Any] | None = None, *, timeout: float | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or [],
        }
        response = await self.client.post(self.url, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise JsonRpcError.from_payload(data["error"])
        return data["result"]


class RpcGatewayClient:
    def __init__(self, base_url: str, *, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        response = await self.client.post(
            f"{self.base_url}/rpc",
            json={"method": method, "params": params or []},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok", True):
            error = payload.get("error", {})
            raise JsonRpcError(error.get("code", -1), error.get("message", "rpc gateway error"), error.get("data"))
        return payload["result"]

    async def health(self) -> dict[str, Any]:
        response = await self.client.get(f"{self.base_url}/health")
        response.raise_for_status()
        return response.json()

    async def subscribe_heads(self) -> AsyncIterator[dict[str, Any]]:
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/heads"
        while True:
            try:
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as websocket:
                    async for raw_message in websocket:
                        yield json.loads(raw_message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("gateway head subscription disconnected: %s", exc)
                await asyncio.sleep(2)


@dataclass(slots=True)
class EthCall:
    to: str
    data: str
    block: str = "latest"

    def to_params(self) -> list[Any]:
        return [{"to": self.to, "data": self.data}, self.block]


async def fetch_block_number(client: RpcGatewayClient | JsonRpcHttpClient) -> int:
    result = await client.call("eth_blockNumber", [])
    if isinstance(result, int):
        return result
    return int(result, 16)


def build_logs_filter(
    *,
    from_block: int,
    to_block: int,
    addresses: list[str] | None = None,
    topics: list[Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fromBlock": to_hex_quantity(from_block),
        "toBlock": to_hex_quantity(to_block),
    }
    if addresses:
        payload["address"] = addresses if len(addresses) > 1 else addresses[0]
    if topics:
        payload["topics"] = topics
    return payload

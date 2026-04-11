from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from src.shared.config import Settings, load_settings
from src.shared.events import LOCAL_ONLY_METHODS, PROBED_METHODS, SAFE_RPC_METHODS
from src.shared.evm import from_hex_quantity
from src.shared.logging import configure_logging
from src.shared.rpc import JsonRpcError, JsonRpcHttpClient, is_method_missing

LOGGER = logging.getLogger(__name__)

PROBE_PARAMS: dict[str, list[Any]] = {
    "eth_blockNumber": [],
    "eth_getBlockByNumber": ["latest", False],
    "eth_getLogs": [{"fromBlock": "latest", "toBlock": "latest", "topics": []}],
    "eth_getTransactionReceipt": ["0x" + "0" * 64],
    "eth_call": [{"to": "0x0000000000000000000000000000000000001010", "data": "0x313ce567"}, "latest"],
    "eth_feeHistory": ["0x1", "latest", [25, 75]],
    "eth_syncing": [],
    "net_version": [],
    "web3_clientVersion": [],
    "txpool_content": [],
    "txpool_status": [],
}


class RpcRequest(BaseModel):
    method: str
    params: list[Any] = Field(default_factory=list)


class MethodBudget:
    def __init__(self, limit: int):
        self.semaphore = asyncio.Semaphore(limit)

    async def __aenter__(self) -> None:
        await self.semaphore.acquire()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.semaphore.release()


class HeadBroadcaster:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.latest_head: dict[str, Any] | None = None
        self._queues: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._subscription_id: str | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="polygon-head-broadcaster")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def subscribe(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=50)
        self._queues.add(queue)
        try:
            if self.latest_head is not None:
                await queue.put(json.dumps(self.latest_head))
            while True:
                yield await queue.get()
        finally:
            self._queues.discard(queue)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload)
        self.latest_head = payload
        dead: list[asyncio.Queue[str]] = []
        for queue in self._queues:
            try:
                queue.put_nowait(encoded)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self._queues.discard(queue)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "eth_subscribe",
                                "params": ["newHeads"],
                            }
                        )
                    )
                    subscribe_reply = json.loads(await websocket.recv())
                    self._subscription_id = subscribe_reply.get("result")
                    LOGGER.info("subscribed to newHeads via local node websocket")
                    async for raw_message in websocket:
                        payload = json.loads(raw_message)
                        result = payload.get("params", {}).get("result")
                        if result:
                            await self._broadcast(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("head broadcaster reconnecting after websocket error: %s", exc)
                await asyncio.sleep(2)


@dataclass(slots=True)
class RouterResult:
    result: Any
    provider: str


class ProviderRouter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.local = JsonRpcHttpClient(settings.polygon_rpc_http, timeout=settings.request_timeout_seconds, name="local")
        self.alchemy = (
            JsonRpcHttpClient(settings.alchemy_rpc_url, timeout=settings.request_timeout_seconds, name="alchemy")
            if settings.alchemy_rpc_url
            else None
        )
        self.capabilities: dict[str, dict[str, bool]] = {"local": {}, "alchemy": {}}
        self.budgets = {
            "eth_getLogs": MethodBudget(4),
            "eth_call": MethodBudget(24),
            "eth_getTransactionReceipt": MethodBudget(16),
            "txpool_content": MethodBudget(1),
            "txpool_status": MethodBudget(2),
        }
        self.default_budget = MethodBudget(32)

    async def close(self) -> None:
        await self.local.close()
        if self.alchemy:
            await self.alchemy.close()

    async def probe_capabilities(self) -> None:
        for method in PROBED_METHODS:
            self.capabilities["local"][method] = await self._probe_client(self.local, method)
            if self.alchemy and method not in LOCAL_ONLY_METHODS:
                self.capabilities["alchemy"][method] = await self._probe_client(self.alchemy, method)
        LOGGER.info("capability matrix local=%s fallback=%s", self.capabilities["local"], self.capabilities["alchemy"])

    async def _probe_client(self, client: JsonRpcHttpClient, method: str) -> bool:
        try:
            await client.call(method, PROBE_PARAMS[method], timeout=min(self.settings.request_timeout_seconds, 10.0))
            return True
        except JsonRpcError as exc:
            if is_method_missing(exc):
                return False
            return True
        except Exception:
            return False

    async def call(self, method: str, params: list[Any] | None = None) -> RouterResult:
        params = params or []
        self._validate(method, params)
        budget = self.budgets.get(method, self.default_budget)
        async with budget:
            client = self._select_primary(method)
            try:
                result = await client.call(method, params)
                return RouterResult(result=result, provider=client.name)
            except Exception as exc:
                if self._should_fallback(method, exc):
                    LOGGER.warning("falling back to alchemy for %s after local error: %s", method, exc)
                    result = await self.alchemy.call(method, params)
                    return RouterResult(result=result, provider=self.alchemy.name)
                raise

    def _validate(self, method: str, params: list[Any]) -> None:
        if method not in SAFE_RPC_METHODS:
            raise ValueError(f"method not allowed: {method}")
        if method == "eth_getLogs" and params:
            first = params[0]
            if isinstance(first, dict):
                from_block = first.get("fromBlock")
                to_block = first.get("toBlock")
                if all(isinstance(value, str) and value.startswith("0x") for value in (from_block, to_block)):
                    span = from_hex_quantity(to_block) - from_hex_quantity(from_block) + 1
                    if span > self.settings.log_range_limit:
                        raise ValueError(f"eth_getLogs range exceeds limit {self.settings.log_range_limit}")

    def _select_primary(self, method: str) -> JsonRpcHttpClient:
        if method in LOCAL_ONLY_METHODS:
            return self.local
        local_supported = self.capabilities["local"].get(method, True)
        if local_supported:
            return self.local
        if self.alchemy and self.capabilities["alchemy"].get(method, False):
            return self.alchemy
        return self.local

    def _should_fallback(self, method: str, exc: Exception) -> bool:
        if not self.alchemy or method in LOCAL_ONLY_METHODS:
            return False
        if isinstance(exc, ValueError):
            return False
        if isinstance(exc, JsonRpcError):
            return is_method_missing(exc) or exc.code in {-32000, -32005, -32603}
        return True

    async def health(self) -> dict[str, Any]:
        block_hex = await self.local.call("eth_blockNumber", [])
        block_number = from_hex_quantity(block_hex)
        syncing = await self.local.call("eth_syncing", [])
        block = await self.local.call("eth_getBlockByNumber", ["latest", False])
        head_lag_seconds = 0
        if block:
            timestamp = from_hex_quantity(block.get("timestamp"))
            head_lag_seconds = max(0, int(datetime.now(tz=UTC).timestamp()) - timestamp)
        return {
            "ok": True,
            "service": "rpc_gateway",
            "node": {
                "syncing": syncing,
                "block_number": block_number,
                "head_lag_seconds": head_lag_seconds,
            },
            "capabilities": self.capabilities,
            "fallback_enabled": bool(self.alchemy),
        }


class GatewayState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.router = ProviderRouter(settings)
        self.heads = HeadBroadcaster(settings.polygon_rpc_ws)
        self.probe_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        await self.router.probe_capabilities()
        self.heads.start()
        self.probe_task = asyncio.create_task(self._probe_loop(), name="rpc-gateway-probe")

    async def shutdown(self) -> None:
        if self.probe_task:
            self.probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.probe_task
        await self.heads.stop()
        await self.router.close()

    async def _probe_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.router.probe_capabilities()
            except Exception as exc:
                LOGGER.warning("capability refresh failed: %s", exc)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings("rpc_gateway")
    configure_logging(settings.log_level)
    state = GatewayState(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="Polygon RPC Gateway", version="1.0.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return await state.router.health()

    @app.post("/rpc")
    async def rpc(request: RpcRequest) -> dict[str, Any]:
        try:
            routed = await state.router.call(request.method, request.params)
            return {"ok": True, "result": routed.result, "provider": routed.provider}
        except JsonRpcError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": exc.message, "data": exc.data}}
        except ValueError as exc:
            return {"ok": False, "error": {"code": -32050, "message": str(exc)}}
        except Exception as exc:
            LOGGER.exception("gateway rpc proxy failed")
            return {"ok": False, "error": {"code": -32099, "message": str(exc)}}

    @app.websocket("/ws/heads")
    async def websocket_heads(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            async for payload in state.heads.subscribe():
                await websocket.send_text(payload)
        except WebSocketDisconnect:
            return

    return app

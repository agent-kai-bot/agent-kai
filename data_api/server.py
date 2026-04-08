"""FastAPI application — KAI Market Data API."""

import asyncio
import logging

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from data_api.config import API_HOST, API_PORT
from data_api.db import create_pool, close_pool
from data_api.nats_bridge import NatsBridge
from data_api.routes import router
from data_api.websocket import ws_endpoint

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

bridge = NatsBridge()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await create_pool()
    await bridge.connect()
    bridge_task = asyncio.create_task(bridge.start())
    logging.getLogger("kai.server").info(
        "KAI Data API started on :%d provider=agent-kai",
        API_PORT,
    )
    yield
    # Shutdown
    bridge.stop()
    bridge_task.cancel()
    await bridge.disconnect()
    await close_pool()


app = FastAPI(title="KAI Market Data API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.add_api_websocket_route("/ws/{symbol}/{interval}", ws_endpoint)


def main():
    uvicorn.run(
        "data_api.server:app",
        host=API_HOST,
        port=API_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()

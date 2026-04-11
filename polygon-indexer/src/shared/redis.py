from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis


class RedisClient:
    def __init__(self, url: str):
        self.redis = Redis.from_url(url, decode_responses=True)

    async def close(self) -> None:
        await self.redis.close()

    async def publish_json(self, channel: str, payload: dict[str, Any]) -> None:
        await self.redis.publish(channel, json.dumps(payload, default=str))

    async def subscribe(self, *channels: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(*channels)
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                data = message["data"]
                if isinstance(data, str):
                    yield message["channel"], json.loads(data)
        finally:
            await pubsub.unsubscribe(*channels)
            await pubsub.close()


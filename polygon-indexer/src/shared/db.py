from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            future=True,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def connect(self) -> AsyncConnection:
        async with self.engine.connect() as connection:
            yield connection

    @asynccontextmanager
    async def connection(self) -> AsyncConnection:
        async with self.engine.begin() as connection:
            yield connection

    @asynccontextmanager
    async def session(self) -> AsyncSession:
        async with self.session_factory() as session:
            yield session


class StateStore:
    def __init__(self, database: Database):
        self.database = database

    async def get(self, key: str, default: str | None = None) -> str | None:
        async with self.database.connection() as connection:
            result = await connection.execute(
                text("SELECT value FROM polygon_indexer_state WHERE key = :key"),
                {"key": key},
            )
            return result.scalar_one_or_none() or default

    async def set(self, key: str, value: str) -> None:
        async with self.database.connection() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO polygon_indexer_state (key, value, updated_at)
                    VALUES (:key, :value, now())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_at = now()
                    """
                ),
                {"key": key, "value": value},
            )

    async def get_int(self, key: str, default: int = 0) -> int:
        value = await self.get(key)
        if value is None:
            return default
        return int(value)

    async def set_int(self, key: str, value: int) -> None:
        await self.set(key, str(value))

    async def get_bool(self, key: str, default: bool = False) -> bool:
        value = await self.get(key)
        if value is None:
            return default
        return value.lower() == "true"

    async def set_bool(self, key: str, value: bool) -> None:
        await self.set(key, "true" if value else "false")

from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from src.ingest.service import IngestService


class _DummyTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _DummyConnection:
    def begin(self) -> _DummyTransaction:
        return _DummyTransaction()


class _DummyDatabase:
    @asynccontextmanager
    async def connect(self):
        yield _DummyConnection()


class IngestBackfillTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = object.__new__(IngestService)
        self.service.settings = SimpleNamespace(log_range_limit=5, backfill_rpc_timeout_seconds=60.0, backfill_days=30)
        self.service.database = _DummyDatabase()
        self.service.tracked_tokens = ["0x1111111111111111111111111111111111111111"]
        self.service.tracked_pools = []
        self.service.persist_transfer_logs = AsyncMock(return_value=0)
        self.service.persist_swap_logs = AsyncMock(return_value=0)
        self.service._set_state_ints = AsyncMock()

    async def test_backfill_transfers_resumes_from_cursor_and_checkpoints_each_chunk(self) -> None:
        async def get_int(key: str, default: int = 0) -> int:
            if key == "backfill_transfer_cursor":
                return 5
            return default

        self.service.state = SimpleNamespace(get_int=AsyncMock(side_effect=get_int))
        self.service.rpc = SimpleNamespace(call=AsyncMock(side_effect=[[], []]))

        await self.service.backfill_transfers(1, 12)

        calls = self.service.rpc.call.await_args_list
        self.assertEqual(calls[0].args[1][0]["fromBlock"], "0x6")
        self.assertEqual(calls[0].args[1][0]["toBlock"], "0xa")
        self.assertEqual(calls[0].kwargs["timeout"], 60.0)
        self.assertEqual(calls[1].args[1][0]["fromBlock"], "0xb")
        self.assertEqual(calls[1].args[1][0]["toBlock"], "0xc")

        cursor_updates = [call.args[1]["backfill_transfer_cursor"] for call in self.service._set_state_ints.await_args_list]
        self.assertEqual(cursor_updates, [10, 12])

    async def test_backfill_transfers_retries_with_smaller_chunk_after_timeout(self) -> None:
        async def get_int(key: str, default: int = 0) -> int:
            return default

        self.service.settings.log_range_limit = 256
        self.service.state = SimpleNamespace(get_int=AsyncMock(side_effect=get_int))
        self.service.rpc = SimpleNamespace(
            call=AsyncMock(side_effect=[httpx.ReadTimeout("boom"), [], [], []])
        )

        await self.service.backfill_transfers(1, 520)

        calls = self.service.rpc.call.await_args_list
        self.assertEqual(calls[0].args[1][0]["fromBlock"], "0x1")
        self.assertEqual(calls[0].args[1][0]["toBlock"], "0x100")
        self.assertEqual(calls[1].args[1][0]["fromBlock"], "0x1")
        self.assertEqual(calls[1].args[1][0]["toBlock"], "0x80")
        self.assertEqual(calls[2].args[1][0]["fromBlock"], "0x81")
        self.assertEqual(calls[2].args[1][0]["toBlock"], "0x180")
        self.assertEqual(calls[3].args[1][0]["fromBlock"], "0x181")
        self.assertEqual(calls[3].args[1][0]["toBlock"], "0x208")

        cursor_updates = [call.args[1]["backfill_transfer_cursor"] for call in self.service._set_state_ints.await_args_list]
        self.assertEqual(cursor_updates, [128, 384, 520])

    async def test_run_backfill_reuses_saved_start_block(self) -> None:
        async def get_int(key: str, default: int = 0) -> int:
            values = {
                "backfill_start_block": 123,
                "backfill_transfer_cursor": 122,
                "backfill_swap_cursor": 122,
            }
            return values.get(key, default)

        self.service.state = SimpleNamespace(
            get_int=AsyncMock(side_effect=get_int),
            set_int=AsyncMock(),
            set_bool=AsyncMock(),
        )
        self.service.refresh_tracking_sets = AsyncMock()
        self.service.find_block_for_timestamp = AsyncMock(return_value=999)
        self.service.backfill_transfers = AsyncMock()
        self.service.backfill_swaps = AsyncMock()
        self.service.seed_recent_blocks = AsyncMock()

        await self.service.run_backfill(200)

        self.service.find_block_for_timestamp.assert_not_awaited()
        self.service.backfill_transfers.assert_awaited_once_with(123, 200)


if __name__ == "__main__":
    unittest.main()

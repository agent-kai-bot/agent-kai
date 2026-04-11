from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from src.decoder.service import DecoderService
from src.shared.events import OWNERSHIP_TRANSFERRED_TOPIC, PAUSED_TOPIC
from src.shared.evm import pad_topic_address


class DecoderServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_decode_ownership_transferred_log(self) -> None:
        service = object.__new__(DecoderService)
        service.load_block_timestamp = AsyncMock(return_value=datetime(2026, 4, 11, 12, 0, tzinfo=UTC))
        log = {
            "address": "0x1111111111111111111111111111111111111111",
            "blockNumber": hex(50),
            "transactionHash": "0x" + "56" * 32,
            "logIndex": hex(1),
            "topics": [
                OWNERSHIP_TRANSFERRED_TOPIC,
                pad_topic_address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                pad_topic_address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            ],
            "data": "0x",
        }

        decoded = await service.decode_governance_log(log)
        self.assertEqual(decoded["event_type"], "ownership_transferred")
        self.assertIn("new_owner", decoded["event_data"])

    async def test_decode_paused_log(self) -> None:
        service = object.__new__(DecoderService)
        service.load_block_timestamp = AsyncMock(return_value=datetime(2026, 4, 11, 12, 0, tzinfo=UTC))
        log = {
            "address": "0x1111111111111111111111111111111111111111",
            "blockNumber": hex(51),
            "transactionHash": "0x" + "78" * 32,
            "logIndex": hex(2),
            "topics": [PAUSED_TOPIC],
            "data": "0x" + ("00" * 12) + "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".rjust(40, "0"),
        }

        decoded = await service.decode_governance_log(log)
        self.assertEqual(decoded["event_type"], "paused")


if __name__ == "__main__":
    unittest.main()


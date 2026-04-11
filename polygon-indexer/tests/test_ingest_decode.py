from __future__ import annotations

import unittest

from eth_abi import encode

from src.ingest.service import IngestService
from src.shared.events import V2_SWAP_TOPIC, V3_SWAP_TOPIC
from src.shared.evm import pad_topic_address


class IngestDecodeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = object.__new__(IngestService)

    def test_decode_v2_swap_log_net_amounts(self) -> None:
        log = {
            "address": "0x1111111111111111111111111111111111111111",
            "blockNumber": hex(100),
            "transactionHash": "0x" + "12" * 32,
            "logIndex": hex(3),
            "topics": [
                V2_SWAP_TOPIC,
                pad_topic_address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                pad_topic_address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            ],
            "data": "0x" + encode(["uint256", "uint256", "uint256", "uint256"], [10, 0, 0, 25]).hex(),
        }

        decoded = self.service.decode_swap_log(log)
        self.assertEqual(decoded["amount0"], -10)
        self.assertEqual(decoded["amount1"], 25)
        self.assertEqual(decoded["sender"], "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(decoded["recipient"], "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    def test_parse_transfer_value_treats_empty_data_as_zero(self) -> None:
        self.assertEqual(self.service._parse_transfer_value({"data": "0x"}), 0)

    def test_parse_transfer_value_skips_malformed_data(self) -> None:
        self.assertIsNone(self.service._parse_transfer_value({"data": "not-hex"}))

    def test_decode_v3_swap_log_signed_amounts(self) -> None:
        log = {
            "address": "0x2222222222222222222222222222222222222222",
            "blockNumber": hex(101),
            "transactionHash": "0x" + "34" * 32,
            "logIndex": hex(7),
            "topics": [
                V3_SWAP_TOPIC,
                pad_topic_address("0xcccccccccccccccccccccccccccccccccccccccc"),
                pad_topic_address("0xdddddddddddddddddddddddddddddddddddddddd"),
            ],
            "data": "0x" + encode(["int256", "int256", "uint160", "uint128", "int24"], [-5, 15, 22, 33, -4]).hex(),
        }

        decoded = self.service.decode_swap_log(log)
        self.assertEqual(decoded["amount0"], -5)
        self.assertEqual(decoded["amount1"], 15)
        self.assertEqual(decoded["sqrt_price_x96"], 22)
        self.assertEqual(decoded["liquidity"], 33)
        self.assertEqual(decoded["tick"], -4)


if __name__ == "__main__":
    unittest.main()

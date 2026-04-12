from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from eth_abi import decode
from web3 import Web3

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def normalize_address(value: str | None) -> str:
    if not value:
        return ZERO_ADDRESS
    value = value.strip()
    if value.startswith("0x"):
        body = value[2:]
    else:
        body = value
    return "0x" + body.lower().rjust(40, "0")[-40:]


def topic_from_signature(signature: str) -> str:
    raw = Web3.keccak(text=signature).hex()
    return raw if raw.startswith("0x") else "0x" + raw


def to_hex_quantity(value: int) -> str:
    return hex(int(value))


def from_hex_quantity(value: int | str | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(value, 16)


def decode_topic_address(topic: str) -> str:
    return normalize_address("0x" + topic[-40:])


def decode_hex_data(data: str) -> bytes:
    if data.startswith("0x"):
        data = data[2:]
    return bytes.fromhex(data)


def decode_abi(types: list[str], data: str) -> tuple:
    return decode(types, decode_hex_data(data))


def decode_string_result(value: str) -> str | None:
    if value in (None, "", "0x"):
        return None
    payload = decode_hex_data(value)
    try:
        decoded = decode(["string"], payload)[0]
        if isinstance(decoded, str):
            return decoded.strip("\x00") or None
    except Exception:
        pass
    text = payload.rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
    return text or None


def decode_uint_result(value: str) -> int | None:
    if value in (None, "", "0x"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def units_to_decimal(value: int | Decimal, decimals: int | None) -> Decimal:
    if decimals is None:
        decimals = 18
    scale = Decimal(10) ** int(decimals)
    return Decimal(value) / scale


def checksum(address: str) -> str:
    return Web3.to_checksum_address(normalize_address(address))


def is_zero_address(address: str | None) -> bool:
    return normalize_address(address) == ZERO_ADDRESS


def pad_topic_address(address: str) -> str:
    return "0x" + normalize_address(address)[2:].rjust(64, "0")


def unique_addresses(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(normalize_address(value) for value in values))


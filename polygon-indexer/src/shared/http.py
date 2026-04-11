from __future__ import annotations

from typing import Any

from .utils import to_iso8601, utcnow


def envelope(data: Any, *, block: int | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    payload_meta = {"timestamp": to_iso8601(utcnow())}
    if block is not None:
        payload_meta["block"] = block
    if meta:
        payload_meta.update(meta)
    return {"ok": True, "data": data, "meta": payload_meta}


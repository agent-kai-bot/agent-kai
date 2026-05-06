"""Daemon runtime package."""

from __future__ import annotations

from typing import Any

__all__ = ["Session"]


def __getattr__(name: str) -> Any:
    """Lazily expose heavy daemon runtime objects."""

    if name == "Session":
        from daemon.core import Session

        return Session
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
